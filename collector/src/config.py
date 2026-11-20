"""Configuration loading, saving, and file watching."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import threading
import time
import unicodedata
from collections.abc import Callable, Collection, Iterator
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from decoder import RuuviReading
from fileutil import atomic_write_text

logger = logging.getLogger(__name__)


_MAC_PATTERN = re.compile(r"([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}")


def normalize_mac(value: str) -> str:
    """Uppercase a MAC address and check it has the XX:XX:XX:XX:XX:XX format."""
    if not _MAC_PATTERN.fullmatch(value):
        msg = f"Invalid MAC address: {value!r}"
        raise ValueError(msg)
    return value.upper()


class TagConfig(BaseModel):
    """Configuration for a single Ruuvi sensor tag."""

    mac: str
    name: str
    enabled: bool = True

    @field_validator("mac")
    @classmethod
    def validate_mac(cls, v: str) -> str:
        """Normalize MAC address to uppercase and validate its format."""
        return normalize_mac(v)

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        """Reject blank names and control characters such as newlines."""
        if not v.strip():
            msg = "Tag name must not be empty"
            raise ValueError(msg)
        if any(unicodedata.category(ch) == "Cc" for ch in v):
            msg = "Tag name must not contain control characters"
            raise ValueError(msg)
        return v


class DashboardConfig(BaseModel):
    """Dashboard layout settings."""

    columns_per_row: int = Field(default=6, ge=1, le=12)


class CollectorConfig(BaseModel):
    """Collector-level settings."""

    victoriametrics_url: str = "http://victoriametrics:8428"
    min_write_interval_seconds: int | None = Field(default=None, ge=1)


class AppConfig(BaseModel):
    """Top-level application configuration loaded from YAML."""

    tags: list[TagConfig] = []
    collector: CollectorConfig = CollectorConfig()
    dashboard: DashboardConfig = DashboardConfig()

    @model_validator(mode="after")
    def check_unique_macs(self) -> AppConfig:
        """Reject configurations that list the same MAC address twice."""
        seen: set[str] = set()
        duplicates = []
        for tag in self.tags:
            if tag.mac in seen and tag.mac not in duplicates:
                duplicates.append(tag.mac)
            seen.add(tag.mac)
        if duplicates:
            msg = f"Duplicate tag MAC addresses: {', '.join(duplicates)}"
            raise ValueError(msg)
        return self

    @property
    def enabled_tags_map(self) -> dict[str, TagConfig]:
        """Return a MAC-to-TagConfig mapping for enabled tags only."""
        return {t.mac: t for t in self.tags if t.enabled}


def parse_config(text: str) -> AppConfig:
    """Parse and validate configuration from YAML text."""
    data = yaml.safe_load(text)
    if data is None:
        data = {}
    return AppConfig.model_validate(data)


def load_config(path: str) -> AppConfig:
    """Load and validate configuration from a YAML file."""
    return parse_config(Path(path).read_text(encoding="utf-8"))


def dump_config(config: AppConfig) -> str:
    """Serialize configuration to YAML text."""
    data = config.model_dump(exclude_defaults=False)
    return yaml.dump(data, default_flow_style=False, sort_keys=False)


class ConfigConflictError(Exception):
    """The config file holds a valid config other than the one a save expected."""


def save_config(
    config: AppConfig,
    path: str,
    watcher: ConfigWatcher | None = None,
    *,
    base: AppConfig | None = None,
) -> None:
    """Write configuration to disk atomically.

    With a watcher, the write is recorded so the watcher does not reload
    the file we just wrote.

    With ``base``, the config the new one was built from, the save raises
    ConfigConflictError instead of writing when the file holds a valid config
    other than ``base``, so an outside edit the app has not applied yet is not
    overwritten. A missing or invalid file is overwritten; a blank one counts
    as an empty config, as it does at startup.
    """
    text = dump_config(config)
    with watcher.own_write(text) if watcher else contextlib.nullcontext():
        if base is not None and _read_valid_config(path) not in (None, base):
            msg = "The config file was changed outside the app"
            raise ConfigConflictError(msg)
        atomic_write_text(path, text)


def _read_valid_config(path: str) -> AppConfig | None:
    try:
        text = Path(path).read_text(encoding="utf-8")
        return parse_config(text) if text.strip() else AppConfig()
    except Exception:  # noqa: BLE001 - as in the watcher, unreadable is no config
        return None


@dataclass
class TagReading:
    """A cached BLE reading with metadata."""

    reading: RuuviReading
    rssi: int
    last_seen: float


# An unconfigured tag not heard from for this long is dropped from the store,
# so it leaves the discovered list and is logged again if it comes back.
DISCOVERY_TTL_SECONDS = 15 * 60
# At most this many unconfigured tags are kept; the least recently seen go first.
MAX_DISCOVERED_TAGS = 100
# Expired unconfigured tags are swept on update at most this often.
PRUNE_INTERVAL_SECONDS = 60


class ReadingsStore:
    """Thread-safe in-memory store for the latest reading per tag.

    Readings of configured tags are kept until removed. Readings of other
    (discovered) tags expire after ``DISCOVERY_TTL_SECONDS`` and at most
    ``MAX_DISCOVERED_TAGS`` of them are kept. Callers pass the configured
    MACs as ``keep``.
    """

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._tags: dict[str, TagReading] = {}
        self._lock = asyncio.Lock()
        self._clock = clock
        self._next_prune = 0.0

    async def update(
        self,
        mac: str,
        reading: RuuviReading,
        rssi: int,
        keep: Collection[str] = (),
    ) -> bool:
        """Store or replace the latest reading for a MAC address.

        Returns True when the tag is new to the store or had not been seen
        for ``DISCOVERY_TTL_SECONDS``, i.e. when it was (re)discovered.
        """
        async with self._lock:
            now = self._clock()
            previous = self._tags.get(mac)
            discovered = (
                previous is None or now - previous.last_seen > DISCOVERY_TTL_SECONDS
            )
            self._tags[mac] = TagReading(reading=reading, rssi=rssi, last_seen=now)
            if previous is None and mac not in keep:
                self._enforce_cap(keep)
            if now >= self._next_prune:
                self._prune_expired(now, keep)
            return discovered

    async def prune(self, keep: Collection[str]) -> None:
        """Drop expired and over-cap readings of tags not in ``keep``."""
        async with self._lock:
            self._prune_expired(self._clock(), keep)
            self._enforce_cap(keep)

    def _prune_expired(self, now: float, keep: Collection[str]) -> None:
        self._next_prune = now + PRUNE_INTERVAL_SECONDS
        cutoff = now - DISCOVERY_TTL_SECONDS
        for mac in [
            m for m, tr in self._tags.items() if m not in keep and tr.last_seen < cutoff
        ]:
            del self._tags[mac]

    def _enforce_cap(self, keep: Collection[str]) -> None:
        unconfigured = [m for m in self._tags if m not in keep]
        excess = len(unconfigured) - MAX_DISCOVERED_TAGS
        if excess <= 0:
            return
        unconfigured.sort(key=lambda m: self._tags[m].last_seen)
        for mac in unconfigured[:excess]:
            del self._tags[mac]

    async def get_all(self) -> dict[str, TagReading]:
        """Return a snapshot of all stored readings."""
        async with self._lock:
            return dict(self._tags)

    async def get_by_mac(self, mac: str) -> TagReading | None:
        """Return the latest reading for a specific MAC, or None."""
        async with self._lock:
            return self._tags.get(mac)

    async def remove(self, mac: str) -> None:
        """Remove a tag's reading from the store."""
        async with self._lock:
            self._tags.pop(mac, None)


class ConfigWatcher:
    """Watch the config file for external changes and trigger a callback.

    The watcher remembers the text of the configuration the app is running
    (our last save, or the last external edit it applied). A file event whose
    current file text matches it is ignored, so our own saves are not
    reloaded, while any other content is reloaded no matter when it appears.
    A blank file is never applied: it is most likely an edit caught half-way.

    File events are handled through ``dispatch``, called with the handler
    from the observer thread. By default the handler runs right there; pass
    ``loop.call_soon_threadsafe`` to handle events on the event loop, in
    order with everything else that reads or saves the config there.
    """

    def __init__(
        self,
        path: str,
        on_change: Callable[[AppConfig], None],
        dispatch: Callable[[Callable[[], None]], object] | None = None,
    ) -> None:
        self._path = path
        self._on_change = on_change
        self._dispatch = dispatch or (lambda handler: handler())
        self._known_text: str | None = None
        # Held while writing or reading the file and updating _known_text, so
        # an event can never see our new file text before it is recorded.
        self._lock = threading.Lock()
        self._observer = Observer()

        handler = _ConfigFileHandler(self)
        watch_dir = str(Path(path).parent)
        self._observer.schedule(handler, watch_dir, recursive=False)

    def start(self) -> None:
        """Start the file system observer."""
        self._observer.start()

    def stop(self) -> None:
        """Stop the file system observer and wait for it to finish."""
        self._observer.stop()
        self._observer.join()

    @contextlib.contextmanager
    def own_write(self, text: str) -> Iterator[None]:
        """Wrap our own write of ``text``; the watcher will not reload it.

        File events are held back until the write finishes, and the text is
        recorded only if the write succeeds.
        """
        with self._lock:
            yield
            self._known_text = text

    def _handle_change(self) -> None:
        with self._lock:
            try:
                text = Path(self._path).read_text(encoding="utf-8")
            except OSError, UnicodeError:
                logger.exception("Failed to read config")
                return
            if text == self._known_text:
                logger.debug("Config file unchanged, ignoring event")
                return
            if not text.strip():
                logger.warning("Config file is empty, keeping the running config")
                return
            try:
                config = parse_config(text)
            except Exception:
                logger.exception("Failed to reload config")
                return
            self._known_text = text
        try:
            self._on_change(config)
            logger.info("Config reloaded from disk")
        except Exception:
            logger.exception("Failed to apply reloaded config")


class _ConfigFileHandler(FileSystemEventHandler):
    def __init__(self, watcher: ConfigWatcher) -> None:
        self._watcher = watcher
        self._config_path = str(Path(watcher._path).resolve())

    def _is_config_file(self, event: FileSystemEvent) -> bool:
        if event.is_directory:
            return False
        dest = getattr(event, "dest_path", "")
        path = dest or event.src_path
        return str(Path(path).resolve()) == self._config_path

    def on_modified(self, event: FileSystemEvent) -> None:
        if self._is_config_file(event):
            self._watcher._dispatch(self._watcher._handle_change)

    def on_created(self, event: FileSystemEvent) -> None:
        if self._is_config_file(event):
            self._watcher._dispatch(self._watcher._handle_change)

    def on_moved(self, event: FileSystemEvent) -> None:
        if self._is_config_file(event):
            self._watcher._dispatch(self._watcher._handle_change)
