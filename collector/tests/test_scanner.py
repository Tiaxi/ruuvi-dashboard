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
