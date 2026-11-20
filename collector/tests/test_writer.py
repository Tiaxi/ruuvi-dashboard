import asyncio
import gc
import time
import types

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from aioresponses import aioresponses

import writer as writer_module
from decoder import RuuviReading
from writer import MetricsWriter, format_line_protocol


def _make_reading(**overrides) -> RuuviReading:
    defaults = {
        "mac": "AA:BB:CC:DD:EE:FF",
        "temperature": 22.5,
        "humidity": 45.0,
        "pressure": 1013.25,
        "acceleration_x": 0.010,
        "acceleration_y": -0.020,
        "acceleration_z": 1.000,
        "battery_voltage": 3.0,
        "tx_power": 4,
        "movement_counter": 42,
        "measurement_sequence": 1234,
    }
    defaults.update(overrides)
    return RuuviReading(**defaults)


class TestFormatLineProtocol:
    def test_basic_format(self):
        reading = _make_reading()
        ts_ns = 1700000000_000_000_000
        line = format_line_protocol(reading, "Sauna", ts_ns, rssi=-65)
        assert line.startswith("ruuvi,")
        assert "mac=AA:BB:CC:DD:EE:FF" in line
        assert "name=Sauna" in line
        assert "temperature=22.5" in line
        assert "humidity=45.0" in line
        assert "pressure=1013.25" in line
        assert "battery_voltage=3.0" in line
        assert "tx_power=4i" in line
        assert "movement_counter=42i" in line
        assert "measurement_sequence=1234i" in line
        assert "rssi=-65i" in line
        assert line.endswith(str(ts_ns))

    def test_rssi_omitted_when_none(self):
        reading = _make_reading()
        line = format_line_protocol(reading, "Sauna", 0)
        assert "rssi" not in line

    def test_name_with_spaces_escaped(self):
        reading = _make_reading()
        line = format_line_protocol(reading, "Living Room", 0)
        assert r"name=Living\ Room" in line

    def test_none_fields_omitted(self):
        reading = _make_reading(temperature=None, humidity=None)
        line = format_line_protocol(reading, "Test", 0)
        assert "temperature" not in line
        assert "humidity" not in line
        assert "pressure=1013.25" in line

    def test_all_none_fields_returns_none(self):
        reading = RuuviReading(
            mac="AA:BB:CC:DD:EE:FF",
            temperature=None,
            humidity=None,
            pressure=None,
            acceleration_x=None,
            acceleration_y=None,
            acceleration_z=None,
            battery_voltage=None,
            tx_power=None,
            movement_counter=None,
            measurement_sequence=None,
        )
        assert format_line_protocol(reading, "Test", 0) is None


class TestMetricsWriter:
    async def test_write_posts_to_victoriametrics(self):
        reading = _make_reading()
        with aioresponses() as m:
            m.post("http://localhost:8428/write?precision=ns", status=204)
            writer = MetricsWriter("http://localhost:8428")
            await writer.write(reading, "Sauna")
            await writer.close()

        total_calls = sum(len(v) for v in m.requests.values())
        assert total_calls == 1
        call = next(iter(m.requests.values()))[0]
        body = call.kwargs["data"]
        assert "ruuvi," in body
        assert "temperature=22.5" in body

    async def test_write_throttle_skips_recent(self):
        reading = _make_reading()
        with aioresponses() as m:
            m.post("http://localhost:8428/write?precision=ns", status=204, repeat=True)
            writer = MetricsWriter("http://localhost:8428", min_write_interval=60)
            await writer.write(reading, "Sauna")  # first write goes through
            await writer.write(reading, "Sauna")  # second write throttled
            await writer.close()

        # Only one POST should have been made
        total_calls = sum(len(v) for v in m.requests.values())
        assert total_calls == 1

    async def test_write_no_throttle_by_default(self):
        reading = _make_reading()
        with aioresponses() as m:
            m.post("http://localhost:8428/write?precision=ns", status=204, repeat=True)
            writer = MetricsWriter("http://localhost:8428")
            await writer.write(reading, "Sauna")
            await writer.write(reading, "Sauna")
            await writer.close()

        total_calls = sum(len(v) for v in m.requests.values())
        assert total_calls == 2

    async def test_update_url_sends_next_write_to_new_database(self):
        """After a reload from URL A to B, the next write goes to B (issue #6)."""
        reading = _make_reading()
        with aioresponses() as m:
            m.post("http://db-a:8428/write?precision=ns", status=204, repeat=True)
            m.post("http://db-b:8428/write?precision=ns", status=204, repeat=True)
            writer = MetricsWriter("http://db-a:8428")
            await writer.write(reading, "Sauna")
            writer.update_url("http://db-b:8428")
            await writer.write(reading, "Sauna")
            await writer.close()

        hosts = [
            url.host for (_method, url), calls in m.requests.items() for _ in calls
        ]
        assert sorted(hosts) == ["db-a", "db-b"]

    async def test_write_logs_error_on_failure(self, caplog):
        reading = _make_reading()
        with aioresponses() as m:
            m.post("http://localhost:8428/write?precision=ns", status=500)
            writer = MetricsWriter("http://localhost:8428")
            await writer.write(reading, "Sauna")
            await writer.close()

        assert "Failed to write" in caplog.text or "500" in caplog.text


class FakeVictoriaMetrics:
    """Local HTTP receiver whose responses tests can stall, fail or release."""

    def __init__(self):
        self.bodies: list[str] = []
        self.status = 204
        self.release = asyncio.Event()
        self.release.set()
        self.received = asyncio.Event()
        app = web.Application()
        app.router.add_post("/write", self._handle)
        self.server = TestServer(app, host="127.0.0.1")

    async def _handle(self, request):
        self.bodies.append(await request.text())
        self.received.set()
        await self.release.wait()
        return web.Response(status=self.status)

    @property
    def base_url(self):
        return str(self.server.make_url("")).rstrip("/")

    def stall(self):
        self.release.clear()

    async def wait_for_requests(self, count):
        while len(self.bodies) < count:
            self.received.clear()
            await asyncio.wait_for(self.received.wait(), timeout=5)


@pytest.fixture
async def backend():
    fake = FakeVictoriaMetrics()
    await fake.server.start_server()
    yield fake
    fake.release.set()
    await fake.server.close()


@pytest.fixture
async def unhandled_errors():
    """Collect errors the event loop reports, e.g. unretrieved task exceptions."""
    loop = asyncio.get_running_loop()
    errors = []
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: errors.append(context))
    yield errors
    loop.set_exception_handler(previous)


async def _idle():
    """Let the worker and the HTTP client run until nothing is runnable."""
    for _ in range(20):
        await asyncio.sleep(0)


async def _stop(task):
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


class TestWriterWorker:
    async def test_stalled_backend_keeps_one_pending_reading_per_mac(self, backend):
        macs = ["AA:00:00:00:00:01", "AA:00:00:00:00:02", "AA:00:00:00:00:03"]
        backend.stall()
        writer = MetricsWriter(backend.base_url)
        worker = asyncio.create_task(writer.run())
        writer.submit(_make_reading(mac=macs[0], measurement_sequence=0), "T")
        await backend.wait_for_requests(1)
        tasks_while_stalled = len(asyncio.all_tasks())

        for seq in range(1, 300):
            mac = macs[seq % len(macs)]
            writer.submit(_make_reading(mac=mac, measurement_sequence=seq), "T")
            await asyncio.sleep(0)

        assert writer.pending_count == len(macs)
        assert len(backend.bodies) == 1  # one request in flight, nothing piling up
        assert len(asyncio.all_tasks()) == tasks_while_stalled

        # Recovery: the backend answers again and the latest reading per MAC
        # is written; older pending readings were replaced, not queued.
        backend.release.set()
        await backend.wait_for_requests(1 + len(macs))
        await _idle()
        assert writer.pending_count == 0
        latest = [f"measurement_sequence={seq}i" for seq in (297, 298, 299)]
        for marker in latest:
            assert sum(marker in body for body in backend.bodies) == 1
        assert len(backend.bodies) == 1 + len(macs)

        await _stop(worker)
        await writer.close()

    async def test_reading_keeps_its_receive_time_while_pending(self, backend):
        backend.stall()
        writer = MetricsWriter(backend.base_url)
        worker = asyncio.create_task(writer.run())
        writer.submit(_make_reading(mac="AA:00:00:00:00:01"), "First")
        await backend.wait_for_requests(1)
        writer.submit(_make_reading(mac="AA:00:00:00:00:02"), "Second")
        submitted_ns = writer_module.time.time_ns()
        await asyncio.sleep(0.05)

        backend.release.set()
        await backend.wait_for_requests(2)
        timestamp_ns = int(backend.bodies[1].rsplit(" ", 1)[1])
        assert timestamp_ns <= submitted_ns

        await _stop(worker)
        await writer.close()

    async def test_concurrent_same_mac_writes_send_one_request(self, backend):
        """Issue #13: every write passed the throttle while one was in flight."""
        backend.stall()
        writer = MetricsWriter(backend.base_url, min_write_interval=60)
        reading = _make_reading()

        writes = asyncio.gather(*(writer.write(reading, "Sauna") for _ in range(10)))
        await backend.wait_for_requests(1)
        await asyncio.sleep(0.05)
        backend.release.set()
        await writes

        assert len(backend.bodies) == 1
        await writer.close()

    async def test_submitted_same_mac_readings_send_one_request_per_interval(
        self, backend
    ):
        backend.stall()
        writer = MetricsWriter(backend.base_url, min_write_interval=60)
        worker = asyncio.create_task(writer.run())

        writer.submit(_make_reading(), "Sauna")
        await backend.wait_for_requests(1)
        for _ in range(10):
            writer.submit(_make_reading(), "Sauna")
            await asyncio.sleep(0)
        backend.release.set()
        await _idle()
        await asyncio.sleep(0.05)
        writer.submit(_make_reading(), "Sauna")
        await asyncio.sleep(0.05)

        assert len(backend.bodies) == 1
        await _stop(worker)
        await writer.close()

    async def test_throttle_keeps_the_newest_pending_reading(
        self, backend, monkeypatch
    ):
        clock = [0.0]
        fake_time = types.SimpleNamespace(
            monotonic=lambda: clock[0], time_ns=time.time_ns
        )
        monkeypatch.setattr(writer_module, "time", fake_time)
        backend.stall()
        writer = MetricsWriter(backend.base_url, min_write_interval=60)
        worker = asyncio.create_task(writer.run())
        sauna, porch = "AA:00:00:00:00:01", "AA:00:00:00:00:02"

        writer.submit(_make_reading(mac=sauna, measurement_sequence=0), "Sauna")
        await backend.wait_for_requests(1)
        writer.submit(_make_reading(mac=porch, measurement_sequence=0), "Porch")
        writer.submit(_make_reading(mac=sauna, measurement_sequence=1), "Sauna")
        # Let the first write succeed, then stall the porch write behind it.
        backend.release.set()
        backend.release.clear()
        await backend.wait_for_requests(2)
        clock[0] = 30.0  # sauna is inside its throttle interval
        writer.submit(_make_reading(mac=sauna, measurement_sequence=2), "Sauna")
        clock[0] = 61.0  # the porch write held the worker past the interval
        backend.release.set()
        await backend.wait_for_requests(3)

        assert "measurement_sequence=2i" in backend.bodies[2]
        await _stop(worker)
        await writer.close()

    async def test_independent_macs_are_not_throttled_by_each_other(self, backend):
        writer = MetricsWriter(backend.base_url, min_write_interval=60)
        await writer.write(_make_reading(mac="AA:00:00:00:00:01"), "A")
        await writer.write(_make_reading(mac="AA:00:00:00:00:02"), "B")
        assert len(backend.bodies) == 2
        await writer.close()

    async def test_failed_write_does_not_throttle_the_sensor(self, backend):
        writer = MetricsWriter(backend.base_url, min_write_interval=60)
        reading = _make_reading()

        backend.status = 500
        await writer.write(reading, "Sauna")
        backend.status = 204
        await writer.write(reading, "Sauna")  # retried, not throttled
        await writer.write(reading, "Sauna")  # throttled after the success

        assert len(backend.bodies) == 2
        await writer.close()

    async def test_hung_backend_times_out_and_recovers(
        self, backend, monkeypatch, caplog
    ):
        monkeypatch.setattr(
            writer_module, "WRITE_TIMEOUT", aiohttp.ClientTimeout(total=0.2)
        )
        backend.stall()
        writer = MetricsWriter(backend.base_url, min_write_interval=60)
        reading = _make_reading()

        await asyncio.wait_for(writer.write(reading, "Sauna"), timeout=2)
        assert "Failed to write metrics" in caplog.text

        backend.release.set()
        await writer.write(reading, "Sauna")
        await writer.write(reading, "Sauna")  # throttled after the success
        assert len(backend.bodies) == 2
        await writer.close()

    async def test_worker_survives_unexpected_error(self, backend, monkeypatch, caplog):
        writer = MetricsWriter(backend.base_url)
        worker = asyncio.create_task(writer.run())
        real_format = writer_module.format_line_protocol

        def explode_once(reading, *args, **kwargs):
            if reading.measurement_sequence == 1:
                msg = "bad reading"
                raise ValueError(msg)
            return real_format(reading, *args, **kwargs)

        monkeypatch.setattr(writer_module, "format_line_protocol", explode_once)
        writer.submit(_make_reading(measurement_sequence=1), "Sauna")
        await _idle()
        writer.submit(_make_reading(measurement_sequence=2), "Sauna")
        await backend.wait_for_requests(1)

        assert not worker.done()
        assert "bad reading" in caplog.text
        assert "measurement_sequence=2i" in backend.bodies[0]
        await _stop(worker)
        await writer.close()

    async def test_cancel_during_inflight_write_is_clean(
        self, backend, unhandled_errors
    ):
        backend.stall()
        writer = MetricsWriter(backend.base_url)
        worker = asyncio.create_task(writer.run())
        writer.submit(_make_reading(mac="AA:00:00:00:00:01"), "A")
        await backend.wait_for_requests(1)
        writer.submit(_make_reading(mac="AA:00:00:00:00:02"), "B")

        await _stop(worker)
        await writer.close()
        backend.release.set()
        await _idle()
        gc.collect()
        await _idle()
        assert unhandled_errors == []
        assert writer.pending_count == 0
        assert len(backend.bodies) == 1  # the queued reading for B was dropped
