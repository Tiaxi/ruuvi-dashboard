"""Entry point for the Ruuvi Dashboard collector service."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
import time
from pathlib import Path

import uvicorn

from api import AppState, create_app
from auth import resolve_admin_password
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


async def run() -> int:
    """Start the collector, scanner, API server, and config watcher.

    Returns the process exit status: 0 after a shutdown signal, 1 when the
    BLE scanner, the metrics writer or the API server stops on its own, so
    the container's restart policy can bring the collector back.
    """
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
        config.tags,
        dashboard_path,
        dashboard_title,
        config.dashboard.columns_per_row,
    )

    def on_config_change(new_config: AppConfig) -> None:
        scanner.update_config(new_config)
        writer.update_min_interval(new_config.collector.min_write_interval_seconds)
        writer.update_url(new_config.collector.victoriametrics_url)
        app_state.config = new_config
        write_dashboard(
            new_config.tags,
            dashboard_path,
            dashboard_title,
            new_config.dashboard.columns_per_row,
        )
        logger.info("Applied config change, dashboard regenerated")

    # File events are handled on the event loop, so a reload and an admin API
    # save never interleave: a save always builds on the running config.
    watcher = ConfigWatcher(
        config_path, on_change=on_config_change, dispatch=loop.call_soon_threadsafe
    )
    app_state.watcher = watcher

    admin_password = resolve_admin_password(
        config_path, os.environ.get("ADMIN_PASSWORD")
    )
    app = create_app(app_state, admin_password=admin_password)

    stop_event = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    admin_port = int(os.environ.get("ADMIN_PORT", "8000"))
    uv_config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=admin_port,
        log_level="info",
    )
    server = uvicorn.Server(uv_config)
    # Signals are handled by the loop above; keep uvicorn from taking them over.
    server.capture_signals = contextlib.nullcontext

    watcher.start()
    server_task = asyncio.create_task(_serve(server), name="admin-api")
    workers = {
        asyncio.create_task(writer.run(), name="metrics-writer"): "Metrics writer",
        asyncio.create_task(scanner.run(), name="ble-scanner"): "BLE scanner",
        server_task: "Admin API server",
    }

    logger.info("Collector running with admin UI on port %d", admin_port)
    return await _supervise(
        workers,
        server=server,
        server_task=server_task,
        stop_event=stop_event,
        watcher=watcher,
        writer=writer,
    )


async def _serve(server: uvicorn.Server) -> None:
    """Run the admin server, turning uvicorn's sys.exit into a task error.

    uvicorn exits the process on startup failures such as a port in use;
    re-raising lets the supervisor shut everything down in order.
    """
    try:
        await server.serve()
    except SystemExit as exc:
        msg = f"uvicorn exited with status {exc.code}"
        raise RuntimeError(msg) from exc


async def _supervise(  # noqa: PLR0913
    workers: dict[asyncio.Task[None], str],
    *,
    server: uvicorn.Server,
    server_task: asyncio.Task[None],
    stop_event: asyncio.Event,
    watcher: ConfigWatcher,
    writer: MetricsWriter,
) -> int:
    """Wait for a shutdown signal or a worker task ending, then clean up.

    ``workers`` maps each long-running task, ``server_task`` included, to its
    name for logs. The admin server is stopped through ``server.should_exit``;
    the other workers are cancelled, which drops readings the metrics writer
    has not sent yet.
    Cleanup runs whatever the tasks raised. Returns the exit status.
    """
    stop_task = asyncio.create_task(stop_event.wait(), name="stop-signal")
    exit_code = 0
    try:
        done, _ = await asyncio.wait(
            {*workers, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        exit_code = _check_unexpected_exit(workers, done)
    finally:
        logger.info("Shutting down...")
        server.should_exit = True
        for task in workers:
            if task is not server_task:
                task.cancel()
        stop_task.cancel()
        try:
            await asyncio.gather(*workers, stop_task, return_exceptions=True)
        finally:
            watcher.stop()
            await writer.close()
            logger.info("Shutdown complete.")
    return exit_code


def _check_unexpected_exit(
    workers: dict[asyncio.Task[None], str],
    done: set[asyncio.Task[None]],
) -> int:
    """Log tasks that ended before a shutdown signal; return the exit status."""
    exit_code = 0
    for task, name in workers.items():
        if task not in done:
            continue
        exit_code = 1
        error = None if task.cancelled() else task.exception()
        if error is not None:
            logger.error(
                "%s failed, exiting so the collector restarts",
                name,
                exc_info=error,
            )
        else:
            logger.error(
                "%s stopped unexpectedly, exiting so the collector restarts", name
            )
    return exit_code


def main() -> None:
    """Run the collector and exit with its status."""
    sys.exit(asyncio.run(run()))


if __name__ == "__main__":
    main()
