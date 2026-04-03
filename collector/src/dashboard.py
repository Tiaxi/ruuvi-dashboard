"""Generate Grafana dashboard JSON from tag configuration."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _stat_panel(
    panel_id: int,
    title: str,
    expr: str,
    unit: str,
    y: int,
    x: int,
    *,
    color_mode: str = "thresholds",
    fixed_color: str | None = None,
    thresholds: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    field_defaults: dict[str, Any] = {
        "unit": unit,
        "decimals": 1,
        "color": {"mode": color_mode},
        "thresholds": {
            "mode": "absolute",
            "steps": thresholds or [{"color": "green", "value": None}],
        },
    }
    if fixed_color:
        field_defaults["color"]["fixedColor"] = fixed_color
    return {
        "type": "stat",
        "id": panel_id,
        "title": title,
        "gridPos": {"h": 3, "w": 4, "x": x, "y": y},
        "datasource": {"type": "prometheus", "uid": ""},
        "fieldConfig": {"defaults": field_defaults, "overrides": []},
        "options": {
            "colorMode": "value",
            "graphMode": "area",
            "reduceOptions": {"calcs": ["lastNotNull"]},
            "textMode": "value" if unit == "celsius" else "auto",
            "text": {"valueSize": 32},
        },
        "targets": [{"expr": expr, "legendFormat": "", "refId": "A"}],
        "timeFrom": "6h",
        "hideTimeOverride": True,
    }


def _row_panel(panel_id: int, title: str, y: int) -> dict[str, Any]:
    return {
        "type": "row",
        "id": panel_id,
        "title": title,
        "collapsed": False,
        "gridPos": {"h": 1, "w": 24, "x": 0, "y": y},
        "panels": [],
    }


def _timeseries_panel(
    panel_id: int,
    title: str,
    expr: str,
    unit: str,
    y: int,
    *,
    w: int = 24,
    x: int = 0,
    interval: str = "1s",
    legend_calcs: list[str] | None = None,
    thresholds: list[dict[str, Any]] | None = None,
    span_nulls: bool = True,
) -> dict[str, Any]:
    custom: dict[str, Any] = {
        "drawStyle": "line",
        "lineWidth": 1,
        "fillOpacity": 10,
        "spanNulls": span_nulls,
        "showPoints": "never",
    }
    if span_nulls:
        custom["pointSize"] = 5
    field_defaults: dict[str, Any] = {"unit": unit, "custom": custom}
    if thresholds:
        field_defaults["thresholds"] = {
            "mode": "absolute",
            "steps": thresholds,
        }
    return {
        "type": "timeseries",
        "id": panel_id,
        "title": title,
        "gridPos": {"h": 8, "w": w, "x": x, "y": y},
        "datasource": {"type": "prometheus", "uid": ""},
        "fieldConfig": {"defaults": field_defaults, "overrides": []},
        "options": {
            "legend": {
                "displayMode": "table",
                "placement": "right",
                "calcs": legend_calcs or ["lastNotNull", "min", "max", "mean"],
            },
            "tooltip": {"mode": "multi"},
        },
        "targets": [
            {
                "expr": expr,
                "legendFormat": "{{name}}",
                "refId": "A",
                "interval": interval,
            }
        ],
    }


COLS_PER_ROW = 5
STAT_W = 4


def _stat_grid(
    names: list[str],
    metric: str,
    unit: str,
    start_id: int,
    start_y: int,
    *,
    color_mode: str = "thresholds",
    fixed_color: str | None = None,
    thresholds: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], int, int]:
    """Lay out stat panels in rows of COLS_PER_ROW.

    Returns (panels, next_id, next_y).
    """
    panels: list[dict[str, Any]] = []
    panel_id = start_id
    for i, name in enumerate(names):
        row = i // COLS_PER_ROW
        col = i % COLS_PER_ROW
        x = col * STAT_W
        y = start_y + row * 3
        expr = f'ruuvi_{metric}{{name="{name}"}}'
        panels.append(
            _stat_panel(
                panel_id,
                name,
                expr,
                unit,
                y,
                x,
                color_mode=color_mode,
                fixed_color=fixed_color,
                thresholds=thresholds,
            )
        )
        panel_id += 1
    if names:
        last_row = (len(names) - 1) // COLS_PER_ROW
        next_y = start_y + (last_row + 1) * 3
    else:
        next_y = start_y
    return panels, panel_id, next_y


def generate_dashboard(
    tag_names: list[str],
    title: str = "Ruuvi Dashboard",
) -> dict[str, Any]:
    """Build a complete Grafana dashboard dict from tag names."""
    panels: list[dict[str, Any]] = []
    panel_id = 1
    y = 0

    # Temperature stats section
    panels.append(_row_panel(panel_id, "Temperatures", y))
    panel_id += 1
    y += 1

    temp_panels, panel_id, y = _stat_grid(
        tag_names,
        "temperature",
        "celsius",
        panel_id,
        y,
        thresholds=[
            {"color": "blue", "value": None},
            {"color": "red", "value": 0},
        ],
    )
    panels.extend(temp_panels)

    # Humidity stats section
    panels.append(_row_panel(panel_id, "Humidity", y))
    panel_id += 1
    y += 1

    hum_panels, panel_id, y = _stat_grid(
        tag_names,
        "humidity",
        "percent",
        panel_id,
        y,
        color_mode="fixed",
        fixed_color="green",
    )
    panels.extend(hum_panels)

    # History section
    panels.append(_row_panel(panel_id, "History", y))
    panel_id += 1
    y += 1

    panels.append(
        _timeseries_panel(panel_id, "Temperature", "ruuvi_temperature", "celsius", y)
    )
    panel_id += 1
    y += 8

    panels.append(
        _timeseries_panel(panel_id, "Humidity", "ruuvi_humidity", "percent", y)
    )
    panel_id += 1
    y += 8

    panels.append(
        _timeseries_panel(panel_id, "Pressure", "ruuvi_pressure", "pressurehpa", y)
    )
    panel_id += 1
    y += 8

    # Extra section
    panels.append(_row_panel(panel_id, "Extra", y))
    panel_id += 1
    y += 1

    panels.append(
        _timeseries_panel(
            panel_id,
            "Battery Voltage",
            "ruuvi_battery_voltage",
            "volt",
            y,
            w=12,
            legend_calcs=["lastNotNull"],
            thresholds=[
                {"color": "red", "value": None},
                {"color": "yellow", "value": 2.5},
                {"color": "green", "value": 2.8},
            ],
        )
    )
    panel_id += 1

    panels.append(
        _timeseries_panel(
            panel_id,
            "RSSI",
            "ruuvi_rssi",
            "dBm",
            y,
            w=12,
            x=12,
            legend_calcs=["mean"],
        )
    )
    panel_id += 1
    y += 8

    panels.append(
        _timeseries_panel(
            panel_id,
            "Movement",
            "ruuvi_movement_counter",
            "none",
            y,
            w=12,
            legend_calcs=["lastNotNull"],
        )
    )
    panel_id += 1

    panels.append(
        _timeseries_panel(
            panel_id,
            "Measurements/hour",
            "count_over_time(ruuvi_temperature[1h] offset -1h)",
            "none",
            y,
            w=12,
            x=12,
            interval="1h",
            legend_calcs=["mean"],
            span_nulls=False,
        )
    )

    return {
        "annotations": {"list": []},
        "editable": True,
        "graphTooltip": 1,
        "links": [],
        "panels": panels,
        "refresh": "10s",
        "schemaVersion": 39,
        "tags": ["ruuvi"],
        "templating": {"list": []},
        "time": {"from": "now-24h", "to": "now"},
        "title": title,
        "uid": "ruuvi-sensors",
    }


def write_dashboard(
    tag_names: list[str],
    path: str,
    title: str = "Ruuvi Dashboard",
) -> None:
    """Generate and write the Grafana dashboard JSON to disk."""
    dashboard = generate_dashboard(tag_names, title)
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(dashboard, indent=2, ensure_ascii=False))
