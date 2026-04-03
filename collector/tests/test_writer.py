from aioresponses import aioresponses

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

    async def test_write_logs_error_on_failure(self, caplog):
        reading = _make_reading()
        with aioresponses() as m:
            m.post("http://localhost:8428/write?precision=ns", status=500)
            writer = MetricsWriter("http://localhost:8428")
            await writer.write(reading, "Sauna")
            await writer.close()

        assert "Failed to write" in caplog.text or "500" in caplog.text
