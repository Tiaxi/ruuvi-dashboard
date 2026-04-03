"""Configuration loading, saving, and file watching."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, field_validator
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from decoder import RuuviReading

logger = logging.getLogger(__name__)


class TagConfig(BaseModel):
    """Configuration for a single Ruuvi sensor tag."""

    mac: str
    name: str
    enabled: bool = True

    @field_validator("mac")
    @classmethod
    def uppercase_mac(cls, v: str) -> str:
        """Normalize MAC address to uppercase."""
        return v.upper()


class DashboardConfig(BaseModel):
    """Dashboard layout settings."""

    columns_per_row: int = 6


class CollectorConfig(BaseModel):
    """Collector-level settings."""

    victoriametrics_url: str = "http://victoriametrics:8428"
    min_write_interval_seconds: int | None = None


class AppConfig(BaseModel):
    """Top-level application configuration loaded from YAML."""

    tags: list[TagConfig] = []
    collector: CollectorConfig = CollectorConfig()
    dashboard: DashboardConfig = DashboardConfig()

    @property
    def enabled_tags_map(self) -> dict[str, TagConfig]:
        """Return a MAC-to-TagConfig mapping for enabled tags only."""
        return {t.mac: t for t in self.tags if t.enabled}


def load_config(path: str) -> AppConfig:
    """Load and validate configuration from a YAML file."""
    text = Path(path).read_text()
    data = yaml.safe_load(text) or {}
    return AppConfig.model_validate(data)


def save_config(
    config: AppConfig, path: str, watcher: ConfigWatcher | None = None
) -> None:
    """Serialize configuration to YAML and write to disk."""
    if watcher is not None:
        watcher.mark_own_write()

    data = config.model_dump(exclude_defaults=False)
    Path(path).write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))


@dataclass
class TagReading:
    """A cached BLE reading with metadata."""

    reading: RuuviReading
    rssi: int
    last_seen: float


class ReadingsStore:
    """Thread-safe in-memory store for the latest reading per tag."""

    def __init__(self) -> None:
        self._tags: dict[str, TagReading] = {}
        self._lock = asyncio.Lock()

    async def update(self, mac: str, reading: RuuviReading, rssi: int) -> None:
        """Store or replace the latest reading for a MAC address."""
        async with self._lock:
            self._tags[mac] = TagReading(
                reading=reading, rssi=rssi, last_seen=time.time()
            )

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
    """Watch the config file for external changes and trigger a callback."""

    _OWN_WRITE_WINDOW = 2.0

    def __init__(self, path: str, on_change: Callable[[AppConfig], None]) -> None:
        self._path = path
        self._on_change = on_change
        self._own_write_time: float = 0.0
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

    def mark_own_write(self) -> None:
        """Record that we just wrote the config file ourselves."""
        self._own_write_time = time.monotonic()

    def _handle_change(self) -> None:
        if time.monotonic() - self._own_write_time < self._OWN_WRITE_WINDOW:
            logger.debug("Ignoring own write to config")
            return
        try:
            config = load_config(self._path)
            self._on_change(config)
            logger.info("Config reloaded from disk")
        except Exception:
            logger.exception("Failed to reload config")


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
            self._watcher._handle_change()

    def on_created(self, event: FileSystemEvent) -> None:
        if self._is_config_file(event):
            self._watcher._handle_change()

    def on_moved(self, event: FileSystemEvent) -> None:
        if self._is_config_file(event):
            self._watcher._handle_change()
