"""Entry point for the Ruuvi Dashboard collector service."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import time
from pathlib import Path

import uvicorn

from api import AppState, create_app
from config import AppConfig, ConfigWatcher, ReadingsStore, load_config, save_config
from dashboard import write_dashboard
from scanner import RuuviScanner
from writer import MetricsWriter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = "/config/config.yaml"


def _ensure_config(config_path: str) -> AppConfig:
    """Load config from disk, creating a default file if it doesn't exist."""
    config_file = Path(config_path)
    if not config_file.exists():
        logger.info("Config file not found at %s, creating default", config_path)
        config_file.parent.mkdir(parents=True, exist_ok=True)
        save_config(AppConfig(), config_path)

    logger.info("Loading config from %s", config_path)
    return load_config(config_path)


async def run() -> None:
    """Start the collector, scanner, API server, and config watcher."""
    config_path = os.environ.get("CONFIG_PATH", DEFAULT_CONFIG_PATH)
    config = _ensure_config(config_path)
    logger.info(
        "Loaded %d tags (%d enabled)",
        len(config.tags),
        len(config.enabled_tags_map),
    )

    writer = MetricsWriter(
        config.collector.victoriametrics_url,
        min_write_interval=config.collector.min_write_interval_seconds,
    )
    readings = ReadingsStore()
    scanner = RuuviScanner(config, writer, readings)

    loop = asyncio.get_running_loop()
    start_time = time.time()

    dashboard_path = os.environ.get("DASHBOARD_PATH", "/dashboards/ruuvi.json")
    dashboard_title = os.environ.get("DASHBOARD_TITLE", "Ruuvi Dashboard")

    app_state = AppState(
        config=config,
        readings=readings,
        watcher=None,
        config_path=config_path,
        start_time=start_time,
        scanner=scanner,
        writer=writer,
        dashboard_path=dashboard_path,
        dashboard_title=dashboard_title,
    )

    write_dashboard(
        [t.name for t in config.tags],
        dashboard_path,
        dashboard_title,
        config.dashboard.columns_per_row,
    )

    def on_config_change(new_config: AppConfig) -> None:
        def apply() -> None:
            scanner.update_config(new_config)
            writer.update_min_interval(new_config.collector.min_write_interval_seconds)
            app_state.config = new_config
            write_dashboard(
                [t.name for t in new_config.tags],
                dashboard_path,
                dashboard_title,
                new_config.dashboard.columns_per_row,
            )
            logger.info("Applied config change, dashboard regenerated")

        loop.call_soon_threadsafe(apply)

    watcher = ConfigWatcher(config_path, on_change=on_config_change)
    app_state.watcher = watcher

    app = create_app(app_state)

    stop_event = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    watcher.start()
    scan_task = asyncio.create_task(scanner.run())

    admin_port = int(os.environ.get("ADMIN_PORT", "8000"))
    uv_config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=admin_port,
        log_level="info",
    )
    server = uvicorn.Server(uv_config)
    server.install_signal_handlers = lambda: None
    server_task = asyncio.create_task(server.serve())

    logger.info("Collector running with admin UI on port %d", admin_port)
    try:
        await stop_event.wait()
    finally:
        logger.info("Shutting down...")
        scan_task.cancel()
        server.should_exit = True
        with contextlib.suppress(asyncio.CancelledError):
            await scan_task
        await server_task
        watcher.stop()
        await writer.close()
        logger.info("Shutdown complete.")


def main() -> None:
    """Run the collector."""
    asyncio.run(run())


if __name__ == "__main__":
    main()
