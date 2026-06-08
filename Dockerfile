# syntax=docker/dockerfile:1.7
# Multi-stage build per HANDOFF.md §3. Slim runtime, non-root, volume at /data.

# ---------------------------------------------------------------- builder ---
FROM python:3.12-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir uv

WORKDIR /build
COPY pyproject.toml README.md ./
COPY app ./app
COPY alembic.ini ./
COPY alembic ./alembic

# Install into an isolated prefix we'll copy into the runtime image.
RUN uv pip install --system --no-cache --target /install \
    "fastapi>=0.115" \
    "uvicorn[standard]>=0.32" \
    "apscheduler>=3.10,<4" \
    "sqlalchemy>=2.0" \
    "alembic>=1.13" \
    "httpx>=0.27" \
    "pydantic>=2.7" \
    "pydantic-settings>=2.4" \
    "jinja2>=3.1" \
    "openpyxl>=3.1" \
    "python-multipart>=0.0.9"

# ---------------------------------------------------------------- runtime ---
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app:/opt/site-packages \
    DATA_DIR=/data \
    HTTP_HOST=0.0.0.0 \
    HTTP_PORT=8088

# Non-root user.
RUN groupadd --system --gid 1000 wfh \
 && useradd  --system --uid 1000 --gid 1000 --create-home --home-dir /home/wfh wfh \
 && mkdir -p /data /app /opt/site-packages \
 && chown -R wfh:wfh /data /app /opt/site-packages

COPY --from=builder /install /opt/site-packages

WORKDIR /app
COPY --chown=wfh:wfh app ./app
COPY --chown=wfh:wfh alembic.ini ./alembic.ini
COPY --chown=wfh:wfh alembic ./alembic

USER wfh

EXPOSE 8088
VOLUME ["/data"]

# Run migrations then start the app.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host ${HTTP_HOST} --port ${HTTP_PORT}"]
