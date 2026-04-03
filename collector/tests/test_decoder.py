import struct

import pytest

from decoder import decode_rawv2


def _build_payload(
    data_format=0x05,
    temperature_raw=4500,  # 22.50 C
    humidity_raw=18000,  # 45.00%
    pressure_raw=51325,  # 1013.25 hPa
    accel_x_raw=10,  # 0.010 g
    accel_y_raw=-20,  # -0.020 g
    accel_z_raw=1000,  # 1.000 g
    power_info_raw=44822,  # 3.000V, 4 dBm
    movement_counter=42,
    measurement_seq=1234,
    mac_bytes=b"\xaa\xbb\xcc\xdd\xee\xff",
) -> bytes:
    return struct.pack(
        ">BhHHhhhHBH6s",
        data_format,
        temperature_raw,
        humidity_raw,
        pressure_raw,
        accel_x_raw,
        accel_y_raw,
        accel_z_raw,
        power_info_raw,
        movement_counter,
        measurement_seq,
        mac_bytes,
    )


VALID_PAYLOAD = _build_payload()


class TestDecodeRawv2:
    def test_valid_payload(self):
        r = decode_rawv2(VALID_PAYLOAD)
        assert r is not None
        assert r.temperature == pytest.approx(22.50)
        assert r.humidity == pytest.approx(45.00)
        assert r.pressure == pytest.approx(1013.25)
        assert r.acceleration_x == pytest.approx(0.010)
        assert r.acceleration_y == pytest.approx(-0.020)
        assert r.acceleration_z == pytest.approx(1.000)
        assert r.battery_voltage == pytest.approx(3.000)
        assert r.tx_power == 4
        assert r.movement_counter == 42
        assert r.measurement_sequence == 1234
        assert r.mac == "AA:BB:CC:DD:EE:FF"

    def test_negative_temperature(self):
        payload = _build_payload(temperature_raw=-1000)  # -5.00 C
        r = decode_rawv2(payload)
        assert r is not None
        assert r.temperature == pytest.approx(-5.00)

    def test_invalid_temperature_sentinel(self):
        # 0x8000 = -32768 as signed int16 is the sentinel for "invalid"
        payload = _build_payload(temperature_raw=-32768)
        r = decode_rawv2(payload)
        assert r is not None
        assert r.temperature is None

    def test_invalid_humidity_sentinel(self):
        payload = _build_payload(humidity_raw=0xFFFF)
        r = decode_rawv2(payload)
        assert r is not None
        assert r.humidity is None

    def test_invalid_pressure_sentinel(self):
        payload = _build_payload(pressure_raw=0xFFFF)
        r = decode_rawv2(payload)
        assert r is not None
        assert r.pressure is None

    def test_invalid_power_info_sentinel(self):
        payload = _build_payload(power_info_raw=0xFFFF)
        r = decode_rawv2(payload)
        assert r is not None
        assert r.battery_voltage is None
        assert r.tx_power is None

    def test_invalid_acceleration_sentinel(self):
        payload = _build_payload(accel_x_raw=-32768)
        r = decode_rawv2(payload)
        assert r is not None
        assert r.acceleration_x is None
        assert r.acceleration_y == pytest.approx(-0.020)

    def test_wrong_data_format_returns_none(self):
        payload = _build_payload(data_format=0x03)
        assert decode_rawv2(payload) is None

    def test_too_short_payload_returns_none(self):
        assert decode_rawv2(b"\x05\x11\x94") is None

    def test_empty_payload_returns_none(self):
        assert decode_rawv2(b"") is None

    def test_reading_is_immutable(self):
        r = decode_rawv2(VALID_PAYLOAD)
        with pytest.raises(AttributeError):
            r.temperature = 99.0
