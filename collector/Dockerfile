# Pinned versions; see "Pinned versions" in the README for how to upgrade.
# The uv image is a named stage so Dependabot sees it as a normal FROM line.
FROM ghcr.io/astral-sh/uv:0.13.9 AS uv

FROM python:3.14.10-slim-bookworm

COPY --from=uv /uv /usr/local/bin/uv

RUN apt-get update \
    && apt-get install -y --no-install-recommends bluez \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-editable

COPY src/ ./src/
COPY static/ ./static/

ENV PYTHONPATH=/app/src
ENV PYTHONUNBUFFERED=1
# uv run needs a cache directory, and a user without a home folder has no
# writable one. The venv is already synced, so skip the cache.
ENV UV_NO_CACHE=1

# Run as a non-root user. Docker Compose sets the IDs from PUID/PGID so the
# files the collector writes to config/ and dashboards/ belong to the host user.
USER 1000:1000

CMD ["uv", "run", "--no-sync", "python", "src/main.py"]
