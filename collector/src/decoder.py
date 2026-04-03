"""Decode RuuviTag RAWv2 (Data Format 5) BLE advertisement payloads."""

from __future__ import annotations

import struct
from dataclasses import dataclass

_RAWV2_FORMAT = ">BhHHhhhHBH6s"
_RAWV2_LENGTH = struct.calcsize(_RAWV2_FORMAT)  # 24 bytes
_DATA_FORMAT_5 = 0x05


@dataclass(frozen=True)
class RuuviReading:
    """Decoded sensor values from a single RuuviTag advertisement."""

    mac: str
    temperature: float | None
    humidity: float | None
    pressure: float | None
    acceleration_x: float | None
    acceleration_y: float | None
    acceleration_z: float | None
    battery_voltage: float | None
    tx_power: int | None
    movement_counter: int | None
    measurement_sequence: int | None


def decode_rawv2(data: bytes) -> RuuviReading | None:
    """Decode a RAWv2 (Data Format 5) manufacturer specific payload.

    Returns None if the data format is not 5 or the payload is too short.
    Individual fields return None when they contain invalid/sentinel values.
    """
    if len(data) < _RAWV2_LENGTH:
        return None

    (
        data_format,
        temp_raw,
        humi_raw,
        pres_raw,
        acc_x_raw,
        acc_y_raw,
        acc_z_raw,
        power_raw,
        move_count,
        meas_seq,
        mac_bytes,
    ) = struct.unpack(_RAWV2_FORMAT, data[:_RAWV2_LENGTH])

    if data_format != _DATA_FORMAT_5:
        return None

    # Temperature: 0x8000 (-32768 signed) is invalid
    temperature = None if temp_raw == -32768 else temp_raw / 200

    # Humidity: 0xFFFF is invalid
    humidity = None if humi_raw == 0xFFFF else humi_raw / 400

    # Pressure: 0xFFFF is invalid, otherwise add 50000 Pa and convert to hPa
    pressure = None if pres_raw == 0xFFFF else (pres_raw + 50000) / 100

    # Acceleration: 0x8000 (-32768 signed) is invalid, otherwise mG -> g
    acceleration_x = None if acc_x_raw == -32768 else acc_x_raw / 1000
    acceleration_y = None if acc_y_raw == -32768 else acc_y_raw / 1000
    acceleration_z = None if acc_z_raw == -32768 else acc_z_raw / 1000

    # Power info: 0xFFFF means both voltage and tx_power invalid
    if power_raw == 0xFFFF:
        battery_voltage = None
        tx_power = None
    else:
        battery_voltage = ((power_raw >> 5) + 1600) / 1000
        tx_power = (power_raw & 0x1F) * 2 - 40

    # Movement counter: 0xFF is invalid
    movement_counter = None if move_count == 0xFF else move_count

    # Measurement sequence: 0xFFFF is invalid
    measurement_sequence = None if meas_seq == 0xFFFF else meas_seq

    mac = ":".join(f"{b:02X}" for b in mac_bytes)

    return RuuviReading(
        mac=mac,
        temperature=temperature,
        humidity=humidity,
        pressure=pressure,
        acceleration_x=acceleration_x,
        acceleration_y=acceleration_y,
        acceleration_z=acceleration_z,
        battery_voltage=battery_voltage,
        tx_power=tx_power,
        movement_counter=movement_counter,
        measurement_sequence=measurement_sequence,
    )
