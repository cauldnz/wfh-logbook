"""Shared pytest fixtures.

Each test gets a fresh on-disk SQLite DB under ``tmp_path`` with all
migrations applied and the immutability triggers installed. This keeps tests
hermetic and lets us exercise the real trigger SQL rather than relying on
ORM events alone.
"""

from __future__ import annotations

import os
from collections.abc import Generator
from pathlib import Path

import pytest
from alembic.config import Config as AlembicConfig
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from alembic import command
from app import config as config_mod
from app import db as db_mod
from app.main import create_app


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> config_mod.Settings:
    """Test-local settings pointing at a tmp data dir."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    # Neutralise live credentials. IMPORTANT: set to EMPTY, do not delenv —
    # pydantic-settings falls back to the repo-root `.env` file for any var
    # absent from the process environment, and the developer's real `.env`
    # carries live UniFi credentials and a live bot token. A deleted var
    # would silently hand tests the REAL controller/bot (observed: the app
    # lifespan started a real Telegram polling loop mid-suite).
    for var in (
        "UNIFI_HOST",
        "UNIFI_USERNAME",
        "UNIFI_PASSWORD",
        "WORK_DEVICE_MACS",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_ALLOWED_USER_IDS",
        "TELEGRAM_WEBHOOK_SECRET",
        "PUBLIC_BASE_URL",
        "CLOUDFLARE_TUNNEL_TOKEN",
    ):
        monkeypatch.setenv(var, "")
    # Literal-typed: empty string would fail validation; pin a valid value.
    monkeypatch.setenv("TELEGRAM_MODE", "webhook")
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("WORK_SSID", "WFH-TEST")
    monkeypatch.setenv("RULE_VERSION", "2026.1")
    monkeypatch.setenv("GAP_BRIDGE_MINUTES", "10")
    monkeypatch.setenv("MIN_SESSION_MINUTES", "2")
    monkeypatch.setenv("DAILY_CAP_HOURS", "12")
    monkeypatch.setenv("LOCAL_TIMEZONE", "Australia/Sydney")
    # Force a clean read of env into Settings.
    monkeypatch.setattr(config_mod, "_settings", None)
    db_mod.reset_engine_cache()
    s = config_mod.get_settings()
    return s


@pytest.fixture
def migrated_db(settings: config_mod.Settings) -> Generator[None, None, None]:
    """Run `alembic upgrade head` against the per-test DB."""
    repo_root = Path(__file__).resolve().parents[1]
    alembic_cfg = AlembicConfig(str(repo_root / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(repo_root / "alembic"))
    alembic_cfg.set_main_option("sqlalchemy.url", settings.db_url())
    # Alembic env.py reads from get_settings() too — keep them aligned.
    old_cwd = Path.cwd()
    try:
        os.chdir(repo_root)
        command.upgrade(alembic_cfg, "head")
        yield
    finally:
        os.chdir(old_cwd)
        db_mod.reset_engine_cache()


@pytest.fixture
def db_session(migrated_db: None) -> Generator[Session, None, None]:
    """Yield an ORM session against the migrated test DB."""
    SessionLocal = db_mod.get_sessionmaker()  # noqa: N806 (SQLAlchemy convention)
    with SessionLocal() as s:
        yield s


@pytest.fixture
def client(migrated_db: None) -> Generator[TestClient, None, None]:
    """TestClient against the FastAPI app, with lifespan running."""
    app = create_app()
    with TestClient(app) as c:
        yield c
