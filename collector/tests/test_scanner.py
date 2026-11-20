import asyncio
from unittest.mock import AsyncMock, patch

import pytest

import scanner as scanner_module
from config import AppConfig, ReadingsStore
from scanner import RuuviScanner, ScannerStatus


def make_scanner():
    return RuuviScanner(AppConfig(), writer=None, readings=ReadingsStore())


class TestScannerRun:
    async def test_start_failure_raises_and_marks_failed(self):
        bleak = AsyncMock()
        bleak.start.side_effect = RuntimeError("BLE adapter unavailable")
        scanner = make_scanner()

        with patch.object(scanner_module, "BleakScanner", return_value=bleak):
            with pytest.raises(RuntimeError, match="adapter unavailable"):
                await scanner.run()

        assert scanner.status == ScannerStatus.FAILED
        bleak.stop.assert_not_awaited()

    async def test_running_then_cancelled_stops_scan(self):
        bleak = AsyncMock()
        scanner = make_scanner()
        assert scanner.status == ScannerStatus.STARTING

        with patch.object(scanner_module, "BleakScanner", return_value=bleak):
            task = asyncio.create_task(scanner.run())
            while scanner.status != ScannerStatus.RUNNING:
                await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert scanner.status == ScannerStatus.STOPPED
        bleak.stop.assert_awaited_once()


class TestReadingHandoff:
    async def test_reading_updates_live_store_and_hands_write_to_worker(self):
        from conftest import make_reading

        from config import TagConfig
        from writer import MetricsWriter

        config = AppConfig(tags=[TagConfig(mac="AA:BB:CC:DD:EE:FF", name="Sauna")])
        # No worker runs, as if the database were down: nothing may wait on it.
        writer = MetricsWriter("http://127.0.0.1:9")
        readings = ReadingsStore()
        scanner = RuuviScanner(config, writer=writer, readings=readings)

        for seq in range(50):
            reading = make_reading(measurement_sequence=seq)
            scanner._schedule_reading(reading, -60)  # noqa: SLF001
        for _ in range(5):
            await asyncio.sleep(0)

        assert asyncio.all_tasks() == {asyncio.current_task()}
        latest = await readings.get_by_mac("AA:BB:CC:DD:EE:FF")
        assert latest.reading.measurement_sequence == 49
        assert writer.pending_count == 1
        await writer.close()
