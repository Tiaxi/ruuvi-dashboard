import asyncio
import os
import signal
import sys

import pytest
import uvicorn

import main
from scanner import ScannerStatus


class FakeServer(uvicorn.Server):
    """Keeps uvicorn's real serve() wrapper but serves until should_exit."""

    on_serve = None

    async def _serve(self, sockets=None):
        if FakeServer.on_serve is not None:
            FakeServer.on_serve()
        while not self.should_exit:
            await asyncio.sleep(0.01)


class FakeWatcher:
    instances = []

    def __init__(self, config_path, on_change, dispatch=None):
        self.started = False
        self.stopped = False
        FakeWatcher.instances.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


async def _wait_forever():
    await asyncio.Event().wait()


class FakeWriter:
    instances = []
    run_impl = staticmethod(_wait_forever)

    def __init__(self, url, min_write_interval):
        self.closed = False
        FakeWriter.instances.append(self)

    def update_min_interval(self, seconds):
        pass

    def submit(self, reading, name, rssi=None):
        pass

    async def run(self):
        await FakeWriter.run_impl()

    async def close(self):
        self.closed = True


def make_scanner(run_impl):
    class FakeScanner:
        def __init__(self, config, writer, readings):
            self.status = ScannerStatus.STARTING

        def update_config(self, config):
            pass

        async def run(self):
            await run_impl(self)

    return FakeScanner


@pytest.fixture
def collector_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "config.yaml"))
    monkeypatch.setenv("DASHBOARD_PATH", str(tmp_path / "ruuvi.json"))
    monkeypatch.setenv("ADMIN_PORT", "0")
    monkeypatch.setattr(main.uvicorn, "Server", FakeServer)
    monkeypatch.setattr(main, "ConfigWatcher", FakeWatcher)
    monkeypatch.setattr(main, "MetricsWriter", FakeWriter)
    monkeypatch.setattr(FakeServer, "on_serve", None)
    FakeWatcher.instances = []
    FakeWriter.instances = []
    monkeypatch.setattr(FakeWriter, "run_impl", staticmethod(_wait_forever))


def assert_cleaned_up():
    assert FakeWatcher.instances[0].started
    assert FakeWatcher.instances[0].stopped
    assert FakeWriter.instances[0].closed


async def fail_on_start(scanner):
    scanner.status = ScannerStatus.FAILED
    msg = "BLE adapter unavailable"
    raise RuntimeError(msg)


async def fail_while_running(scanner):
    scanner.status = ScannerStatus.RUNNING
    await asyncio.sleep(0.05)
    scanner.status = ScannerStatus.FAILED
    msg = "BLE adapter went away"
    raise RuntimeError(msg)


async def return_early(_scanner):
    await asyncio.sleep(0.05)


async def run_forever(scanner):
    scanner.status = ScannerStatus.RUNNING
    await asyncio.Event().wait()


class TestScannerSupervision:
    @pytest.mark.parametrize(
        "run_impl", [fail_on_start, fail_while_running, return_early]
    )
    async def test_scanner_end_exits_nonzero_and_cleans_up(
        self, collector_env, monkeypatch, caplog, run_impl
    ):
        monkeypatch.setattr(main, "RuuviScanner", make_scanner(run_impl))

        exit_code = await asyncio.wait_for(main.run(), timeout=5)

        assert exit_code == 1
        assert_cleaned_up()
        assert "BLE scanner" in caplog.text

    async def test_shutdown_signal_exits_zero_and_cleans_up(
        self, collector_env, monkeypatch
    ):
        fake_scanner = make_scanner(run_forever)
        monkeypatch.setattr(main, "RuuviScanner", fake_scanner)
        # Signal handlers are installed before the server starts serving.
        monkeypatch.setattr(
            FakeServer, "on_serve", lambda: os.kill(os.getpid(), signal.SIGTERM)
        )

        exit_code = await asyncio.wait_for(main.run(), timeout=5)

        assert exit_code == 0
        assert_cleaned_up()

    async def test_server_exit_is_supervised(self, collector_env, monkeypatch, caplog):
        monkeypatch.setattr(main, "RuuviScanner", make_scanner(run_forever))

        def bind_failure():
            # uvicorn calls sys.exit(1) when it cannot bind the port.
            sys.exit(1)

        monkeypatch.setattr(FakeServer, "on_serve", bind_failure)

        exit_code = await asyncio.wait_for(main.run(), timeout=5)

        assert exit_code == 1
        assert_cleaned_up()
        assert "Admin API server failed" in caplog.text

    async def test_writer_end_exits_nonzero_and_cleans_up(
        self, collector_env, monkeypatch, caplog
    ):
        monkeypatch.setattr(main, "RuuviScanner", make_scanner(run_forever))

        async def writer_dies():
            await asyncio.sleep(0.05)
            msg = "writer worker crashed"
            raise RuntimeError(msg)

        monkeypatch.setattr(FakeWriter, "run_impl", staticmethod(writer_dies))

        exit_code = await asyncio.wait_for(main.run(), timeout=5)

        assert exit_code == 1
        assert_cleaned_up()
        assert "Metrics writer failed" in caplog.text

    def test_main_exits_with_run_status(self, monkeypatch):
        async def fake_run():
            return 1

        monkeypatch.setattr(main, "run", fake_run)

        with pytest.raises(SystemExit) as exc_info:
            main.main()

        assert exc_info.value.code == 1
