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

CMD ["uv", "run", "--no-sync", "python", "src/main.py"]
