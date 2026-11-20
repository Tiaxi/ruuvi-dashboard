"""BLE scanner that discovers and reads Ruuvi sensor tags."""

from __future__ import annotations

import asyncio
import logging
from enum import StrEnum

from bleak import AdvertisementData, BleakScanner, BLEDevice

from config import AppConfig, ReadingsStore
from decoder import RuuviReading, decode_rawv2
from writer import MetricsWriter

logger = logging.getLogger(__name__)

RUUVI_COMPANY_ID = 0x0499


class ScannerStatus(StrEnum):
    """Lifecycle state of the BLE scanner, reported by /api/health."""

    STARTING = "starting"
    RUNNING = "running"
    STOPPED = "stopped"
    FAILED = "failed"


class RuuviScanner:
    """Scan for Ruuvi BLE advertisements and forward readings."""

    def __init__(
        self,
        config: AppConfig,
        writer: MetricsWriter,
        readings: ReadingsStore,
    ) -> None:
        self._config = config
        self._writer = writer
        self._readings = readings
        self._loop: asyncio.AbstractEventLoop | None = None
        self._seen_macs: set[str] = set()
        self._tasks: set[asyncio.Future[None]] = set()
        self.status = ScannerStatus.STARTING

    def update_config(self, config: AppConfig) -> None:
        """Apply a new configuration (e.g. after tag changes)."""
        self._config = config
        logger.info(
            "Scanner config updated: %d tags enabled",
            len(config.enabled_tags_map),
        )

    def _detection_callback(
        self, device: BLEDevice, advertisement_data: AdvertisementData
    ) -> None:
        raw = advertisement_data.manufacturer_data.get(RUUVI_COMPANY_ID)
        if raw is None:
            return

        reading = decode_rawv2(raw)
        if reading is None:
            return

        rssi = advertisement_data.rssi

        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._schedule_reading, reading, rssi)

    def _schedule_reading(self, reading: RuuviReading, rssi: int) -> None:
        # The task only updates in-memory state and hands the write to the
        # writer worker, so it finishes right away; keep a reference until then.
        task = asyncio.ensure_future(self._process_reading(reading, rssi))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _process_reading(self, reading: RuuviReading, rssi: int) -> None:
        await self._readings.update(reading.mac, reading, rssi)

        if reading.mac not in self._seen_macs:
            self._seen_macs.add(reading.mac)
            logger.info(
                "Discovered Ruuvi tag %s (%.1f\u00b0C, %.0f%% RH, %.2fV, RSSI %d dBm)",
                reading.mac,
                reading.temperature or 0,
                reading.humidity or 0,
                reading.battery_voltage or 0,
                rssi,
            )

        enabled = self._config.enabled_tags_map
        tag = enabled.get(reading.mac)
        if tag is not None:
            self._writer.submit(reading, tag.name, rssi=rssi)

    async def run(self) -> None:
        """Start the BLE scanner and process readings until cancelled.

        Errors are re-raised after marking the scanner failed, so the caller
        can shut the collector down and let the container restart it.
        """
        self._loop = asyncio.get_running_loop()
        self.status = ScannerStatus.STARTING
        logger.info("Starting BLE scan...")
        try:
            scanner = BleakScanner(detection_callback=self._detection_callback)
            await scanner.start()
            self.status = ScannerStatus.RUNNING
            try:
                await asyncio.Event().wait()
            finally:
                logger.info("Stopping BLE scan...")
                try:
                    await scanner.stop()
                except Exception:
                    logger.debug("Error stopping scanner (expected during shutdown)")
        except asyncio.CancelledError:
            self.status = ScannerStatus.STOPPED
            raise
        except Exception:
            self.status = ScannerStatus.FAILED
            raise
