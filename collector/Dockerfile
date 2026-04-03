FROM python:3.14-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

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
