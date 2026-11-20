# Ruuvi Dashboard

Self-hosted monitoring for [Ruuvi](https://ruuvi.com/) BLE sensor tags with a Python collector, VictoriaMetrics for storage, and auto-generated Grafana dashboards — all running in Docker on a Raspberry Pi or any Linux host.

## Architecture

```
Ruuvi Tags ─── BLE ──▶ Collector ─── remote write ──▶ VictoriaMetrics
                            │                                │
                       generate JSON                      PromQL
                            │                                │
                            ▼                                ▼
                   dashboards/ruuvi.json ── provision ──▶ Grafana
```

## Features

- **Auto-discovery** — scans for nearby Ruuvi tags and shows unregistered ones in the admin UI
- **Live readings** — temperature, humidity, pressure, battery voltage, RSSI, and movement counter
- **Generated Grafana dashboard** — built automatically from tag config; no manual Grafana editing
- **Drag-and-drop ordering** — reorder tags in the admin UI; Grafana updates within 30 seconds
- **Dark / light theme** — admin UI follows system preference or can be toggled manually
- **Hot-reload config** — edit `config.yaml` on disk and changes apply without restart

## Prerequisites

- Raspberry Pi or Linux host with Bluetooth (BLE)
- Docker and Docker Compose
- One or more [Ruuvi sensor tags](https://ruuvi.com/)

## Quick Start

```bash
git clone https://github.com/Tiaxi/ruuvi-dashboard.git
cd ruuvi-dashboard

cp .env.example .env
nano .env                     # set GF_ADMIN_PASSWORD='<password>' (required, keep the single quotes)
cp config/config.yaml.example config/config.yaml

docker compose up -d --build

# Open the admin UI at http://<host>:8000
# Open Grafana at http://<host>:3000 (sign in as admin with GF_ADMIN_PASSWORD)
```

The defaults serve Grafana over plain HTTP to any computer on your network. To
put it behind HTTPS, see [HTTPS and reverse proxy](#https-and-reverse-proxy).

On first start with no tags configured, the Grafana dashboard will show empty graph sections.
Add tags via the admin UI — they appear in the dashboard within 30 seconds.

## Configuration

### Environment Variables (`.env`)

| Variable | Default | Description |
|---|---|---|
| `DASHBOARD_TITLE` | `Ruuvi Dashboard` | Dashboard title in Grafana and browser tab |
| `GF_ADMIN_PASSWORD` | *(required)* | Grafana admin password, only used on first start; see [Grafana admin password](#grafana-admin-password) |
| `GRAFANA_ROOT_URL` | `http://localhost:3000/` | Public Grafana URL as seen by browsers, used for links Grafana generates; e.g. `http://raspberrypi.local:3000/` on a LAN or `https://saa.example.com/` behind a reverse proxy |
| `GRAFANA_DOMAIN` | `localhost` | Public Grafana hostname, e.g. `saa.example.com`; only enforced when `GRAFANA_ENFORCE_DOMAIN=true` |
| `GRAFANA_SUBPATH` | *(empty)* | Serve Grafana under a subpath (e.g. `/grafana/`) for reverse proxies; include the same path in `GRAFANA_ROOT_URL` |
| `GRAFANA_ENFORCE_DOMAIN` | `false` | Redirect requests for any other hostname to `GRAFANA_DOMAIN`; set `true` for HTTPS/reverse proxy |
| `GRAFANA_COOKIE_SECURE` | `false` | Send the Grafana login cookie over HTTPS only; set `true` for HTTPS/reverse proxy |
| `ADMIN_PORT` | `8000` | Host port for the admin UI |
| `GRAFANA_PORT` | `3000` | Host port for Grafana |
| `VM_RETENTION` | `1y` | VictoriaMetrics data retention period |

### HTTPS and reverse proxy

The defaults are for plain HTTP on a home network. When Grafana is served over
HTTPS at a fixed hostname (for example behind a reverse proxy), use the
hardened settings in `.env`:

```bash
GRAFANA_ROOT_URL=https://saa.example.com/
GRAFANA_DOMAIN=saa.example.com
GRAFANA_ENFORCE_DOMAIN=true
GRAFANA_COOKIE_SECURE=true
# Optional, when served under a subpath:
# GRAFANA_ROOT_URL=https://saa.example.com/grafana/
# GRAFANA_SUBPATH=/grafana/
```

Do not enable `GRAFANA_ENFORCE_DOMAIN` or `GRAFANA_COOKIE_SECURE` for plain
HTTP on a LAN: the first redirects browsers that open Grafana by IP or hostname
to `GRAFANA_DOMAIN`, and the second makes login fail over HTTP. With
`GRAFANA_ENFORCE_DOMAIN=true`, the reverse proxy must pass the public `Host`
header through to Grafana, or Grafana keeps redirecting to itself.

### Grafana admin password

`GF_ADMIN_PASSWORD` must be set in `.env`; `docker compose` refuses to run
without it. Put the value in single quotes (`GF_ADMIN_PASSWORD='...'`), or
Compose expands any `$` in it. Write an apostrophe inside the value as `\'`,
e.g. `GF_ADMIN_PASSWORD='it\'s$secret'`.

Grafana only reads it on the first start, when it creates the admin user, so
changing it in `.env` later has no effect. To change the password of an
existing install:

```bash
docker compose exec grafana grafana cli admin reset-admin-password '<new password>'
```

Installs created before this was required started with the password `admin`
unless `GF_ADMIN_PASSWORD` was changed before the first start. If that is your
case, change it with the command above.

### VictoriaMetrics access

VictoriaMetrics has no authentication, so its port is not published on the
host. The collector and Grafana reach it on the internal Docker network as
`victoriametrics:8428`. For diagnostics, run commands inside the container:

```bash
docker compose exec victoriametrics wget -qO- http://127.0.0.1:8428/api/v1/status/tsdb
```

Or publish it on the host's loopback interface only with a
`docker-compose.override.yml` next to `docker-compose.yml` (Compose loads it
automatically), then use `http://127.0.0.1:8428` on the host:

```yaml
services:
  victoriametrics:
    ports:
      - "127.0.0.1:8428:8428"
```

### Tag Config (`config/config.yaml`)

```yaml
tags:
  - mac: "AA:BB:CC:DD:EE:FF"
    name: "Living Room"
  - mac: "11:22:33:44:55:66"
    name: "Kitchen"
    enabled: false          # optional, defaults to true

collector:
  # min_write_interval_seconds: 60   # optional write throttle
  victoriametrics_url: "http://victoriametrics:8428"

dashboard:
  columns_per_row: 6        # stat panels per row (default 6)
```

Tags can also be added, renamed, reordered, and deleted through the admin UI — changes are saved to `config.yaml` automatically.

## Admin UI

The collector serves a web UI on port 8000 (configurable via `ADMIN_PORT`):

- **Configured Tags** — live readings, enable/disable, edit, delete, and drag-and-drop reordering
- **Discovered Tags** — unconfigured tags detected nearby, with one-click add
- **Database** — VictoriaMetrics storage statistics
- **Settings** — write interval and dashboard layout (panels per row)

## Grafana Dashboard

The dashboard is generated automatically from your tag configuration and written to `./dashboards/ruuvi.json`. Grafana picks up changes within 30 seconds.

- **Temperature** and **Humidity** stat panels — one per tag, in config order
- **History** graphs — temperature, humidity, and pressure over time
- **Extra** graphs — battery voltage, RSSI, movement counter, measurements per hour

Every panel queries the provisioned VictoriaMetrics datasource by its fixed uid (`victoriametrics`), so the dashboard also works when shared externally (Grafana's **Share → Share externally**, formerly "public dashboards"). That gives a no-login link that runs only this dashboard's queries, which is a narrower thing to expose than anonymous access to the whole Grafana.

## Development

```bash
cd collector
uv sync --dev
uv run pytest -v
uv run ruff check src/ tests/
```

## Project Structure

```
ruuvi-dashboard/
├── collector/
│   ├── src/
│   │   ├── api.py            # FastAPI admin API + static file serving
│   │   ├── config.py         # YAML config, file watcher, readings store
│   │   ├── dashboard.py      # Grafana dashboard JSON generator
│   │   ├── decoder.py        # RuuviTag RAWv2 BLE payload decoder
│   │   ├── main.py           # Entry point, orchestrates all components
│   │   ├── scanner.py        # BLE scanner (bleak)
│   │   └── writer.py         # Prometheus remote write to VictoriaMetrics
│   ├── static/
│   │   └── index.html        # Admin UI (single-page, vanilla JS)
│   ├── tests/
│   ├── Dockerfile
│   └── pyproject.toml
├── config/
│   ├── config.yaml.example
│   └── config.yaml           # your tag config (gitignored)
├── dashboards/               # generated dashboard JSON (gitignored)
├── grafana/
│   └── provisioning/         # datasource + dashboard provider config
├── docker-compose.yml
├── .env.example
└── .env                      # your settings (gitignored)
```

## License

[MIT](LICENSE)
