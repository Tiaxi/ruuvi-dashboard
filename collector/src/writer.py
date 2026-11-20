"""Write Ruuvi sensor readings to VictoriaMetrics via Prometheus line protocol."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping

import aiohttp

from decoder import RuuviReading

logger = logging.getLogger(__name__)


def _escape_tag_value(v: str) -> str:
    # Escape backslashes first so the escapes added below stay intact; without
    # this a value ending in "\" escapes the space that separates tags from fields.
    return (
        v.replace("\\", "\\\\")
        .replace(" ", r"\ ")
        .replace(",", r"\,")
        .replace("=", r"\=")
    )


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


# A write to a local VictoriaMetrics normally takes milliseconds. Fail fast when
# the database is slow or down so the worker moves on to fresher readings.
WRITE_TIMEOUT = aiohttp.ClientTimeout(total=5, connect=2)


class MetricsWriter:
    """Async writer that posts readings to VictoriaMetrics.

    The scanner hands readings over with :meth:`submit`, which never blocks.
    A single worker (:meth:`run`) sends them one at a time. Pending work is
    bounded by the number of sensors: only the latest unsent reading per MAC is
    kept, and a newer reading replaces an older one that has not been sent yet
    (it is dropped, not queued). Pending readings are dropped on shutdown.

    Writes are serialized, so the per-MAC throttle sees the previous write's
    outcome before deciding on the next one. Only a successful write starts
    the throttle interval; after a failure the next reading is tried again.
    """

    def __init__(self, base_url: str, min_write_interval: int | None = None) -> None:
        self._url = self._write_url(base_url)
        self._min_interval = min_write_interval
        self._last_write: dict[str, float] = {}
        self._session: aiohttp.ClientSession | None = None
        self._pending: dict[str, tuple[RuuviReading, str, int | None, int]] = {}
        self._wakeup = asyncio.Event()
        self._write_lock = asyncio.Lock()

    @staticmethod
    def _write_url(base_url: str) -> str:
        return f"{base_url}/write?precision=ns"

    def update_url(self, base_url: str) -> None:
        """Send later writes to a new VictoriaMetrics base URL.

        Writes already in flight finish against the old URL.
        """
        self._url = self._write_url(base_url)

    def update_min_interval(self, value: int | None) -> None:
        """Update the minimum write interval for throttling."""
        self._min_interval = value

    def update_tag_names(self, names: Mapping[str, str]) -> None:
        """Apply tag changes to readings that are still queued.

        ``names`` maps each enabled tag's MAC to its current name. A queued
        reading whose tag was disabled or removed is dropped, and one whose tag
        was renamed is sent under the new name. A reading the worker already
        took is sent as it was.
        """
        for mac, (reading, name, rssi, timestamp_ns) in list(self._pending.items()):
            new_name = names.get(mac)
            if new_name is None:
                del self._pending[mac]
            elif new_name != name:
                self._pending[mac] = (reading, new_name, rssi, timestamp_ns)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=WRITE_TIMEOUT)
        return self._session

    def _is_throttled(self, mac: str) -> bool:
        if self._min_interval is None:
            return False
        last = self._last_write.get(mac)
        if last is None:
            return False
        return (time.monotonic() - last) < self._min_interval

    @property
    def pending_count(self) -> int:
        """Number of readings waiting for the worker (at most one per MAC)."""
        return len(self._pending)

    def submit(self, reading: RuuviReading, name: str, rssi: int | None = None) -> None:
        """Queue a reading for the worker, replacing any unsent one for its MAC.

        The reading keeps the time it was received, not the time it is sent.
        The throttle is checked when the worker sends it.
        """
        self._pending[reading.mac] = (reading, name, rssi, time.time_ns())
        self._wakeup.set()

    async def run(self) -> None:
        """Send submitted readings until cancelled.

        A failed write is logged and never ends the loop, so one bad reading
        or database outage cannot stop later writes.
        """
        while True:
            await self._wakeup.wait()
            self._wakeup.clear()
            while self._pending:
                mac = next(iter(self._pending))
                reading, name, rssi, timestamp_ns = self._pending.pop(mac)
                try:
                    await self.write(
                        reading, name, rssi=rssi, timestamp_ns=timestamp_ns
                    )
                except Exception:
                    logger.exception("Unexpected error writing metrics for %s", mac)

    async def write(
        self,
        reading: RuuviReading,
        name: str,
        rssi: int | None = None,
        timestamp_ns: int | None = None,
    ) -> None:
        """Write a reading to VictoriaMetrics now, respecting the throttle.

        Concurrent calls are serialized, so at most one write per MAC goes out
        per throttle interval.
        """
        async with self._write_lock:
            if self._is_throttled(reading.mac):
                return

            if timestamp_ns is None:
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
            except (aiohttp.ClientError, TimeoutError) as exc:
                logger.error("Failed to write metrics: %r", exc)  # noqa: TRY400

    async def close(self) -> None:
        """Drop unsent readings and close the underlying HTTP session."""
        self._pending.clear()
        if self._session and not self._session.closed:
            await self._session.close()
