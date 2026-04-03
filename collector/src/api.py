"""Admin API for Ruuvi Dashboard collector."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from dataclasses import fields as dataclass_fields
from pathlib import Path
from typing import Any

import aiohttp
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from config import (
    AppConfig,
    ConfigWatcher,
    ReadingsStore,
    TagConfig,
    save_config,
)
from dashboard import write_dashboard
from decoder import RuuviReading
from scanner import RuuviScanner
from writer import MetricsWriter


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


class SettingsUpdateRequest(BaseModel):
    """Request body for PATCH /api/settings."""

    min_write_interval_seconds: int | None = Field(default=None, ge=1)


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
        v = v.upper()
        if not re.fullmatch(r"([0-9A-F]{2}:){5}[0-9A-F]{2}", v):
            msg = f"Invalid MAC address: {v}"
            raise ValueError(msg)
        return v


def _apply_config(state: AppState, new_config: AppConfig) -> None:
    """Save config to disk and update all in-memory state."""
    save_config(new_config, state.config_path, state.watcher)
    state.config = new_config
    if state.scanner:
        state.scanner.update_config(new_config)
    if state.writer:
        state.writer.update_min_interval(
            new_config.collector.min_write_interval_seconds
        )
    if state.dashboard_path:
        write_dashboard(
            [t.name for t in new_config.tags],
            state.dashboard_path,
            state.dashboard_title,
        )


def _serialize_reading(reading: RuuviReading) -> dict[str, Any]:
    return {
        f.name: getattr(reading, f.name)
        for f in dataclass_fields(reading)
        if f.name != "mac"
    }


def create_app(state: AppState) -> FastAPI:
    """Create the FastAPI application with all admin routes."""
    app = FastAPI(title="Ruuvi Admin")

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {
            "uptime_seconds": round(time.time() - state.start_time),
            "tag_count": len(state.config.tags),
            "enabled_tag_count": len(state.config.enabled_tags_map),
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
        mac = body.mac.upper()
        if any(t.mac == mac for t in state.config.tags):
            raise HTTPException(409, f"Tag {mac} already configured")
        new_tag = TagConfig(mac=mac, name=body.name, enabled=body.enabled)
        new_config = state.config.model_copy(
            update={"tags": [*state.config.tags, new_tag]}
        )
        _apply_config(state, new_config)
        return {"mac": new_tag.mac, "name": new_tag.name, "enabled": new_tag.enabled}

    @app.patch("/api/tags/{mac}")
    async def update_tag(mac: str, body: TagUpdateRequest) -> dict[str, Any]:
        mac = mac.upper()
        idx = next((i for i, t in enumerate(state.config.tags) if t.mac == mac), None)
        if idx is None:
            raise HTTPException(404, f"Tag {mac} not found")
        tag = state.config.tags[idx]
        updated = tag.model_copy(update=body.model_dump(exclude_unset=True))
        new_tags = list(state.config.tags)
        new_tags[idx] = updated
        new_config = state.config.model_copy(update={"tags": new_tags})
        _apply_config(state, new_config)
        return {"mac": updated.mac, "name": updated.name, "enabled": updated.enabled}

    @app.delete("/api/tags/{mac}", status_code=204)
    async def delete_tag(mac: str) -> None:
        mac = mac.upper()
        new_tags = [t for t in state.config.tags if t.mac != mac]
        if len(new_tags) == len(state.config.tags):
            raise HTTPException(404, f"Tag {mac} not found")
        new_config = state.config.model_copy(update={"tags": new_tags})
        _apply_config(state, new_config)
        await state.readings.remove(mac)

    @app.put("/api/tags/order")
    async def reorder_tags(body: TagOrderRequest) -> dict[str, bool]:
        macs = [m.upper() for m in body.macs]
        current = {t.mac: t for t in state.config.tags}
        if set(macs) != set(current):
            raise HTTPException(400, "MAC list must contain exactly the current tags")
        new_tags = [current[mac] for mac in macs]
        new_config = state.config.model_copy(update={"tags": new_tags})
        _apply_config(state, new_config)
        return {"ok": True}

    @app.get("/api/settings")
    async def get_settings() -> dict[str, Any]:
        collector = state.config.collector
        return {
            "min_write_interval_seconds": collector.min_write_interval_seconds,
            "victoriametrics_url": collector.victoriametrics_url,
        }

    @app.patch("/api/settings")
    async def update_settings(body: SettingsUpdateRequest) -> dict[str, Any]:
        new_collector = state.config.collector.model_copy(
            update=body.model_dump(exclude_unset=True)
        )
        new_config = state.config.model_copy(update={"collector": new_collector})
        _apply_config(state, new_config)
        collector = new_config.collector
        return {
            "min_write_interval_seconds": collector.min_write_interval_seconds,
            "victoriametrics_url": collector.victoriametrics_url,
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
