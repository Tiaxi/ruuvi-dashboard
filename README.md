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
nano .env                     # set GF_ADMIN_PASSWORD='<password>' (required, keep the single quotes);
                              # set PUID/PGID if `id -u` / `id -g` are not 1000
cp config/config.yaml.example config/config.yaml

docker compose up -d --build

# Open the admin UI at http://<host>:8000 and log in as "admin". Unless
# ADMIN_PASSWORD is set in .env, the collector generates a password a few
# seconds after it starts; once `docker compose logs collector` mentions it:
cat config/admin_password
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
| `PUID` / `PGID` | `1000` / `1000` | User and group ID the collector runs as; see [Running as your user](#running-as-your-user) |
| `ADMIN_PORT` | `8000` | Host port for the admin UI |
| `ADMIN_PASSWORD` | *(empty)* | Admin UI password for user `admin`; if empty, a random one is generated into `config/admin_password` |
| `GRAFANA_PORT` | `3000` | Host port for Grafana |
| `VM_RETENTION` | `1y` | VictoriaMetrics data retention period |

### Running as your user

The collector runs as the user and group ID set by `PUID` and `PGID` (default
1000, the first user on Raspberry Pi OS), not as root. Files it writes to
`config/` and `dashboards/` belong to that user, so you can read and edit them
without `sudo`. Set both to the output of `id -u` and `id -g` for the user
that owns the project folder. That user must be able to write to `config/` and
`dashboards/`, and Grafana must be able to read `dashboards/`; both folders
come with the clone, and the usual umask (022) gives that.

**Bluetooth.** The collector talks to BlueZ through the host's system D-Bus.
Raspberry Pi OS and Debian 12 (bookworm) let every user do that (their
`/etc/dbus-1/system.d/bluetooth.conf` has `<allow send_destination="org.bluez"/>`
under `<policy context="default">`), so scanning needs nothing more. If your
host's policy only allows root and the `bluetooth` group, give the container
that group by its host ID (`getent group bluetooth`), for example in a
`docker-compose.override.yml`:

```yaml
services:
  collector:
    group_add:
      - "112"   # the GID of the host's bluetooth group
```

Adding your host user to the group is not enough: D-Bus checks the groups of
the container process.

**Upgrading from a version that ran as root.** Older versions ran the
collector as root, so `config/admin_password`, the generated dashboard, and
possibly `config/config.yaml` and the `dashboards/` folder belong to root. The
collector then stops at start with a "Permission denied" error in
`docker compose logs collector`. Hand the files to your user once, before
`git pull` (if `id -u` or `id -g` is not 1000, also add `PUID` and `PGID` to
`.env`):

```bash
docker compose down
mkdir -p dashboards
sudo chown -R "$(id -u):$(id -g)" config dashboards   # the same IDs as PUID/PGID
git pull
docker compose up -d --build
```

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

### Pinned versions

Every image is pinned to a release tag, so a version change only comes with a
reviewed commit. Rebuilding the collector can still pick up Debian security
updates (in the base image and the `bluez` package), but not a new Python, uv,
Grafana or VictoriaMetrics release:

| Component | Pinned in | Version |
| --- | --- | --- |
| Grafana | `docker-compose.yml` | `grafana/grafana:13.3.1` |
| VictoriaMetrics | `docker-compose.yml` | `victoriametrics/victoria-metrics:v1.156.0` |
| Python (collector base) | `collector/Dockerfile` | `python:3.14.10-slim-bookworm` |
| uv (collector build) | `collector/Dockerfile` | `ghcr.io/astral-sh/uv:0.13.9` |

Python packages are pinned by `collector/uv.lock`. Dependabot
(`.github/dependabot.yml`) opens a pull request each week when a newer image
tag or GitHub Action is out. To upgrade by hand, or to test such a pull request:

1. Change the tag in the file above and update this table, in a reviewed commit.
   Read the release notes first; a new Grafana major can change provisioning.
2. Pull and rebuild: `docker compose pull && docker compose up -d --build`.
3. Check that `docker compose logs grafana` shows no provisioning errors, that
   the Ruuvi dashboard loads with data, and that the admin UI shows readings.
   The CI smoke test (see [Development](#development)) checks the Grafana side
   of this on every pull request.

To roll back, revert the commit and run step 2 again. Downgrading Grafana or
VictoriaMetrics across a major version may not read data the newer one wrote,
so back up the `grafana_data` and `vmdata` volumes before a major upgrade.

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

### Admin login

The admin UI and every `/api/*` route require HTTP Basic auth; only `GET /api/health` is open, for health checks. The user name is `admin`. The password is:

- `ADMIN_PASSWORD` from `.env`, if set. Put it in single quotes, e.g. `ADMIN_PASSWORD='my pa$$word'`, so Compose does not expand `$` in it, and run `docker compose up -d` after changing it. Otherwise:
- a random password generated on first start and saved to `config/admin_password`. It has mode 0600 and belongs to the `PUID` user, so read it with `cat config/admin_password`. The collector log says where it is but never prints the password. Edit the file to choose your own password, or delete it to generate a new one, then `docker compose restart collector`.

Basic auth over plain HTTP is only as private as your LAN: anyone who can watch the traffic can read the password. If the admin UI is reachable from anywhere wider, put it behind a reverse proxy that serves HTTPS.

## Grafana Dashboard

The dashboard is generated automatically from your tag configuration and written to `./dashboards/ruuvi.json`. Grafana picks up changes within 30 seconds.

- **Temperature** and **Humidity** stat panels — one per tag, in config order
- **History** graphs — temperature, humidity, and pressure over time
- **Extra** graphs — battery voltage, RSSI, movement counter, measurements per hour

Every panel queries the provisioned VictoriaMetrics datasource by its fixed uid (`victoriametrics`), so the dashboard also works when shared externally (Grafana's **Share → Share externally**, formerly "public dashboards"). That gives a no-login link that runs only this dashboard's queries, which is a narrower thing to expose than anonymous access to the whole Grafana.

## Development

```bash
cd collector
uv sync --locked --dev
uv run pytest -v
uv run ruff check src/ tests/
```

`scripts/compose-smoke.sh` starts Grafana and VictoriaMetrics with a
throwaway admin password, provisions a dashboard generated by the collector
code, and checks through Grafana's API that the datasource is healthy, the
dashboard loads, a written sample comes back, and Grafana logs no errors. CI
runs it on every pull request. It needs Docker, uv, curl, jq and openssl,
binds Grafana to 127.0.0.1 on a free port, ignores `.env`, and removes its
containers and volumes when done.

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
│   └── provisioning/         # datasource + dashboard provider config;
│                             # empty alerting/ and plugins/ placeholders
├── scripts/
│   └── compose-smoke.sh      # Compose stack smoke test (also run in CI)
├── docker-compose.yml
├── .env.example
└── .env                      # your settings (gitignored)
```

## License

[MIT](LICENSE)
