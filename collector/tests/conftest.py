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


def failing(exc: BaseException):
    """Return a stand-in function that raises ``exc`` when called."""

    def fail(*_args, **_kwargs):
        raise exc

    return fail


class FakeClock:
    """A settable stand-in for ``time.time``."""

    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def mac_for(i: int) -> str:
    """Return a distinct MAC address for index ``i``."""
    return "10:00:00:00:{:02X}:{:02X}".format(*divmod(i, 256))
