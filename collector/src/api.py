"""Admin API for Ruuvi Dashboard collector."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from dataclasses import fields as dataclass_fields
from pathlib import Path
from typing import Any

import aiohttp
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError, field_validator

from auth import BasicAuthMiddleware
from config import (
    AppConfig,
    ConfigWatcher,
    ReadingsStore,
    normalize_mac,
    save_config,
)
from dashboard import render_dashboard, write_dashboard_text
from decoder import RuuviReading
from scanner import RuuviScanner
from writer import MetricsWriter

logger = logging.getLogger(__name__)


@dataclass
class AppState:
    """Shared mutable state for the collector application."""

    config: AppConfig
    readings: ReadingsStore
    watcher: ConfigWatcher | None
    config_path: str
    start_time: float
    scanner: RuuviScanner | None = None
    writer: MetricsWriter | None = None
    dashboard_path: str | None = None
    dashboard_title: str = "Ruuvi Dashboard"


class TagUpdateRequest(BaseModel):
    """Request body for PATCH /api/tags/{mac}."""

    name: str | None = None
    enabled: bool | None = None

    @field_validator("name", "enabled")
    @classmethod
    def reject_null(cls, v: object) -> object:
        """Reject explicit nulls; omit a field to leave it unchanged."""
        if v is None:
            msg = "Field must not be null; omit it to leave it unchanged"
            raise ValueError(msg)
        return v


class SettingsUpdateRequest(BaseModel):
    """Request body for PATCH /api/settings."""

    min_write_interval_seconds: int | None = Field(default=None, ge=1)
    columns_per_row: int | None = Field(default=None, ge=1, le=12)


class TagOrderRequest(BaseModel):
    """Request body for PUT /api/tags/order."""

    macs: list[str]


class TagCreateRequest(BaseModel):
    """Request body for POST /api/tags."""

    mac: str
    name: str
    enabled: bool = True

    @field_validator("mac")
    @classmethod
    def validate_mac(cls, v: str) -> str:
        """Normalize and validate MAC address format."""
        return normalize_mac(v)


def _build_config(state: AppState, **updates: Any) -> AppConfig:  # noqa: ANN401
    """Validate the current config merged with updates, or raise HTTP 422.

    Updates must be plain data (dicts and lists), so that every tag and
    setting is validated again instead of being passed through as a model.
    """
    data = state.config.model_dump()
    data.update(updates)
    try:
        return AppConfig.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in err['loc']) or 'config'}: {err['msg']}"
            for err in exc.errors(include_url=False)
        )
        raise HTTPException(422, f"Invalid configuration: {problems}") from exc


def _apply_config(state: AppState, new_config: AppConfig) -> None:
    """Save a validated config to disk, then switch the running app to it.

    The dashboard JSON is rendered before anything is written, and the
    dashboard is written before the config, so any failure leaves the config
    file and runtime state on the previous config. If the config write fails
    after the dashboard was written, the previous dashboard is put back.
    """
    if state.dashboard_path:
        dashboard_text = _render_dashboard(state, new_config)
        try:
            write_dashboard_text(dashboard_text, state.dashboard_path)
        except OSError as exc:
            raise HTTPException(
                500, f"Could not write the Grafana dashboard: {exc}"
            ) from exc

    try:
        save_config(new_config, state.config_path, state.watcher)
    except OSError as exc:
        if state.dashboard_path:
            try:
                write_dashboard_text(
                    _render_dashboard(state, state.config), state.dashboard_path
                )
            except OSError:
                logger.exception("Failed to restore the previous dashboard")
        raise HTTPException(500, f"Could not save the configuration: {exc}") from exc

    state.config = new_config
    if state.scanner:
        state.scanner.update_config(new_config)
    if state.writer:
        state.writer.update_min_interval(
            new_config.collector.min_write_interval_seconds
        )
        state.writer.update_url(new_config.collector.victoriametrics_url)


def _render_dashboard(state: AppState, config: AppConfig) -> str:
    return render_dashboard(
        [t.name for t in config.tags],
        state.dashboard_title,
        config.dashboard.columns_per_row,
    )


def _serialize_reading(reading: RuuviReading) -> dict[str, Any]:
    return {
        f.name: getattr(reading, f.name)
        for f in dataclass_fields(reading)
        if f.name != "mac"
    }


def create_app(state: AppState, *, admin_password: str) -> FastAPI:
    """Create the FastAPI application with all admin routes.

    Everything it serves, the admin UI included, requires HTTP Basic auth
    with ``admin_password``, except ``GET /api/health``.
    """
    if not admin_password:
        msg = "Admin password must not be empty"
        raise ValueError(msg)
    app = FastAPI(title="Ruuvi Admin")
    app.add_middleware(BasicAuthMiddleware, password=admin_password)

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {
            "uptime_seconds": round(time.time() - state.start_time),
            "tag_count": len(state.config.tags),
            "enabled_tag_count": len(state.config.enabled_tags_map),
            "scanner": state.scanner.status if state.scanner else "unknown",
        }

    @app.get("/api/tags")
    async def get_tags() -> list[dict[str, Any]]:
        all_readings = await state.readings.get_all()
        result = []
        for tag in state.config.tags:
            entry: dict[str, Any] = {
                "mac": tag.mac,
                "name": tag.name,
                "enabled": tag.enabled,
            }
            tr = all_readings.get(tag.mac)
            if tr:
                entry["reading"] = _serialize_reading(tr.reading)
                entry["rssi"] = tr.rssi
                entry["last_seen"] = tr.last_seen
            else:
                entry["reading"] = None
                entry["rssi"] = None
                entry["last_seen"] = None
            result.append(entry)
        return result

    @app.get("/api/discovered")
    async def get_discovered() -> list[dict[str, Any]]:
        all_readings = await state.readings.get_all()
        configured_macs = {t.mac for t in state.config.tags}
        result = []
        for mac, tr in all_readings.items():
            if mac not in configured_macs:
                result.append(
                    {
                        "mac": mac,
                        "reading": _serialize_reading(tr.reading),
                        "rssi": tr.rssi,
                        "last_seen": tr.last_seen,
                    }
                )
        return result

    @app.post("/api/tags", status_code=201)
    async def add_tag(body: TagCreateRequest) -> dict[str, Any]:
        mac = body.mac
        if any(t.mac == mac for t in state.config.tags):
            raise HTTPException(409, f"Tag {mac} already configured")
        tags = [t.model_dump() for t in state.config.tags]
        tags.append({"mac": mac, "name": body.name, "enabled": body.enabled})
        new_config = _build_config(state, tags=tags)
        new_tag = new_config.tags[-1]
        _apply_config(state, new_config)
        return {"mac": new_tag.mac, "name": new_tag.name, "enabled": new_tag.enabled}

    @app.patch("/api/tags/{mac}")
    async def update_tag(mac: str, body: TagUpdateRequest) -> dict[str, Any]:
        mac = mac.upper()
        idx = next((i for i, t in enumerate(state.config.tags) if t.mac == mac), None)
        if idx is None:
            raise HTTPException(404, f"Tag {mac} not found")
        tags = [t.model_dump() for t in state.config.tags]
        tags[idx].update(body.model_dump(exclude_unset=True))
        new_config = _build_config(state, tags=tags)
        updated = new_config.tags[idx]
        _apply_config(state, new_config)
        return {"mac": updated.mac, "name": updated.name, "enabled": updated.enabled}

    @app.delete("/api/tags/{mac}", status_code=204)
    async def delete_tag(mac: str) -> None:
        mac = mac.upper()
        new_tags = [t.model_dump() for t in state.config.tags if t.mac != mac]
        if len(new_tags) == len(state.config.tags):
            raise HTTPException(404, f"Tag {mac} not found")
        new_config = _build_config(state, tags=new_tags)
        _apply_config(state, new_config)
        await state.readings.remove(mac)

    @app.put("/api/tags/order")
    async def reorder_tags(body: TagOrderRequest) -> dict[str, bool]:
        macs = [m.upper() for m in body.macs]
        current = {t.mac: t for t in state.config.tags}
        if len(macs) != len(set(macs)):
            raise HTTPException(400, "MAC list must not contain duplicates")
        if set(macs) != set(current):
            raise HTTPException(400, "MAC list must contain exactly the current tags")
        new_tags = [current[mac].model_dump() for mac in macs]
        new_config = _build_config(state, tags=new_tags)
        _apply_config(state, new_config)
        return {"ok": True}

    @app.get("/api/settings")
    async def get_settings() -> dict[str, Any]:
        collector = state.config.collector
        return {
            "min_write_interval_seconds": collector.min_write_interval_seconds,
            "victoriametrics_url": collector.victoriametrics_url,
            "columns_per_row": state.config.dashboard.columns_per_row,
            "dashboard_title": state.dashboard_title,
        }

    @app.patch("/api/settings")
    async def update_settings(body: SettingsUpdateRequest) -> dict[str, Any]:
        collector_data = state.config.collector.model_dump()
        collector_data.update(
            body.model_dump(exclude_unset=True, exclude={"columns_per_row"})
        )
        dashboard_data = state.config.dashboard.model_dump()
        if body.columns_per_row is not None:
            dashboard_data["columns_per_row"] = body.columns_per_row
        new_config = _build_config(
            state, collector=collector_data, dashboard=dashboard_data
        )
        _apply_config(state, new_config)
        collector = new_config.collector
        return {
            "min_write_interval_seconds": collector.min_write_interval_seconds,
            "victoriametrics_url": collector.victoriametrics_url,
            "columns_per_row": new_config.dashboard.columns_per_row,
        }

    @app.get("/api/db-stats")
    async def db_stats() -> dict[str, Any]:
        vm_url = state.config.collector.victoriametrics_url
        result: dict[str, Any] = {}
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(f"{vm_url}/metrics") as resp:
                    if resp.status == 200:
                        text = await resp.text()
                        metrics: dict[str, str] = {}
                        for line in text.splitlines():
                            if line.startswith("#"):
                                continue
                            parts = line.split()
                            if len(parts) >= 2:
                                metrics[parts[0]] = parts[1]
                        rows_big = int(
                            float(metrics.get('vm_rows{type="storage/big"}', "0"))
                        )
                        rows_small = int(
                            float(metrics.get('vm_rows{type="storage/small"}', "0"))
                        )
                        result["total_datapoints"] = rows_big + rows_small
                        size_big = int(
                            float(
                                metrics.get(
                                    'vm_data_size_bytes{type="storage/big"}',
                                    "0",
                                )
                            )
                        )
                        size_small = int(
                            float(
                                metrics.get(
                                    'vm_data_size_bytes{type="storage/small"}',
                                    "0",
                                )
                            )
                        )
                        result["storage_bytes"] = size_big + size_small
                        result["active_time_series"] = int(
                            float(
                                metrics.get(
                                    'vm_cache_entries{type="storage/metricName"}',
                                    "0",
                                )
                            )
                        )
            except aiohttp.ClientError:
                pass
            try:
                async with session.get(f"{vm_url}/api/v1/status/tsdb") as resp:
                    if resp.status == 200:
                        data = (await resp.json()).get("data", {})
                        result["total_series"] = data.get("totalSeries", 0)
                        result["series_by_metric"] = [
                            {"name": s["name"], "count": s["value"]}
                            for s in data.get("seriesCountByMetricName", [])
                        ]
            except aiohttp.ClientError:
                pass
        return result

    static_dir = Path(__file__).parent.parent / "static"
    if static_dir.exists():
        app.mount(
            "/",
            StaticFiles(directory=str(static_dir), html=True),
            name="static",
        )

    return app
