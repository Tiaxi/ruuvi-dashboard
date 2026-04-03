from decoder import RuuviReading


def make_reading(**overrides) -> RuuviReading:
    defaults = {
        "mac": "AA:BB:CC:DD:EE:FF",
        "temperature": 22.5,
        "humidity": 45.0,
        "pressure": 1013.25,
        "acceleration_x": 0.0,
        "acceleration_y": 0.0,
        "acceleration_z": 1.0,
        "battery_voltage": 3.0,
        "tx_power": 4,
        "movement_counter": 0,
        "measurement_sequence": 0,
    }
    defaults.update(overrides)
    return RuuviReading(**defaults)
