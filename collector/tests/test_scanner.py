import asyncio
import logging
from unittest.mock import AsyncMock, patch

import pytest
from conftest import FakeClock, mac_for, make_reading

import scanner as scanner_module
from config import (
    DISCOVERY_TTL_SECONDS,
    MAX_DISCOVERED_TAGS,
    AppConfig,
    ReadingsStore,
    TagConfig,
)
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


class TestConfigChangeReachesWriter:
    """Issue #46: queued readings followed the tag config at submit time."""

    MAC = "AA:BB:CC:DD:EE:FF"

    async def _queued(self, tags):
        from writer import MetricsWriter

        writer = MetricsWriter("http://127.0.0.1:9")  # no worker: stays queued
        scanner = RuuviScanner(
            AppConfig(tags=tags), writer=writer, readings=ReadingsStore()
        )
        await scanner._process_reading(make_reading(mac=self.MAC), -60)  # noqa: SLF001
        assert writer.pending_count == 1
        return scanner, writer

    @pytest.mark.parametrize(
        "new_tags",
        [
            pytest.param([TagConfig(mac=MAC, name="Sauna", enabled=False)], id="off"),
            pytest.param([], id="deleted"),
        ],
    )
    async def test_disabled_or_deleted_tag_drops_queued_reading(self, new_tags):
        scanner, writer = await self._queued([TagConfig(mac=self.MAC, name="Sauna")])

        scanner.update_config(AppConfig(tags=new_tags))

        assert writer.pending_count == 0
        await writer.close()

    async def test_renamed_tag_queued_reading_uses_new_name(self):
        scanner, writer = await self._queued([TagConfig(mac=self.MAC, name="Sauna")])

        scanner.update_config(AppConfig(tags=[TagConfig(mac=self.MAC, name="Spa")]))

        assert writer._pending[self.MAC][1] == "Spa"  # noqa: SLF001
        await writer.close()


class TestDiscoveryBookkeeping:
    def _discoveries(self, caplog) -> list[str]:
        return [
            r.args[0] for r in caplog.records if r.msg.startswith("Discovered Ruuvi")
        ]

    async def test_tag_back_after_ttl_is_logged_again(self, caplog):
        caplog.set_level(logging.INFO, logger="scanner")
        clock = FakeClock()
        scanner = RuuviScanner(
            AppConfig(), writer=None, readings=ReadingsStore(clock=clock)
        )
        process = scanner._process_reading  # noqa: SLF001 - what the callback runs
        reading = make_reading(mac="11:22:33:44:55:66")

        await process(reading, -70)
        await process(reading, -70)
        assert self._discoveries(caplog) == ["11:22:33:44:55:66"]

        clock.now += DISCOVERY_TTL_SECONDS + 1
        await process(reading, -70)
        assert self._discoveries(caplog) == ["11:22:33:44:55:66"] * 2

    async def test_transient_tags_do_not_evict_configured_tag(self):
        clock = FakeClock()
        store = ReadingsStore(clock=clock)
        config = AppConfig(
            tags=[TagConfig(mac="AA:BB:CC:DD:EE:FF", name="Room", enabled=False)]
        )
        scanner = RuuviScanner(config, writer=None, readings=store)
        process = scanner._process_reading  # noqa: SLF001 - what the callback runs

        await process(make_reading(), -60)
        for i in range(MAX_DISCOVERED_TAGS + 10):
            clock.now += 1
            await process(make_reading(mac=mac_for(i)), -70)
        clock.now += DISCOVERY_TTL_SECONDS + 1
        await process(make_reading(mac="11:22:33:44:55:66"), -70)

        stored = await store.get_all()
        assert set(stored) == {"AA:BB:CC:DD:EE:FF", "11:22:33:44:55:66"}
