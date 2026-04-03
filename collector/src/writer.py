"""Write Ruuvi sensor readings to VictoriaMetrics via Prometheus line protocol."""

from __future__ import annotations

import logging
import time

import aiohttp

from decoder import RuuviReading

logger = logging.getLogger(__name__)


def _escape_tag_value(v: str) -> str:
    return v.replace(" ", r"\ ").replace(",", r"\,").replace("=", r"\=")


def format_line_protocol(
    reading: RuuviReading,
    name: str,
    timestamp_ns: int,
    rssi: int | None = None,
) -> str | None:
    """Format a reading as a Prometheus line protocol string."""
    tags = f"mac={_escape_tag_value(reading.mac)},name={_escape_tag_value(name)}"

    fields = []
    for attr in (
        "temperature",
        "humidity",
        "pressure",
        "battery_voltage",
        "acceleration_x",
        "acceleration_y",
        "acceleration_z",
    ):
        val = getattr(reading, attr)
        if val is not None:
            fields.append(f"{attr}={val}")
    for attr in ("tx_power", "movement_counter", "measurement_sequence"):
        val = getattr(reading, attr)
        if val is not None:
            fields.append(f"{attr}={val}i")
    if rssi is not None:
        fields.append(f"rssi={rssi}i")

    if not fields:
        return None

    return f"ruuvi,{tags} {','.join(fields)} {timestamp_ns}"


class MetricsWriter:
    """Async writer that posts readings to VictoriaMetrics."""

    def __init__(self, base_url: str, min_write_interval: int | None = None) -> None:
        self._url = f"{base_url}/write?precision=ns"
        self._min_interval = min_write_interval
        self._last_write: dict[str, float] = {}
        self._session: aiohttp.ClientSession | None = None

    def update_min_interval(self, value: int | None) -> None:
        """Update the minimum write interval for throttling."""
        self._min_interval = value

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    def _is_throttled(self, mac: str) -> bool:
        if self._min_interval is None:
            return False
        last = self._last_write.get(mac)
        if last is None:
            return False
        return (time.monotonic() - last) < self._min_interval

    async def write(
        self, reading: RuuviReading, name: str, rssi: int | None = None
    ) -> None:
        """Write a reading to VictoriaMetrics, respecting throttle."""
        if self._is_throttled(reading.mac):
            return

        timestamp_ns = time.time_ns()
        line = format_line_protocol(reading, name, timestamp_ns, rssi=rssi)
        if line is None:
            return

        session = await self._get_session()
        try:
            async with session.post(self._url, data=line) as resp:
                if resp.status >= 400:  # noqa: PLR2004
                    body = await resp.text()
                    logger.error(
                        "Failed to write metrics: HTTP %d — %s",
                        resp.status,
                        body,
                    )
                else:
                    self._last_write[reading.mac] = time.monotonic()
        except aiohttp.ClientError:
            logger.exception("Failed to write metrics")

    async def close(self) -> None:
        """Close the underlying HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
