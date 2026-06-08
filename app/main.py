"""FastAPI application factory + startup wiring.

Phase 1: minimal — opens the DB, seeds the config singleton on first run,
exposes /api/health. Later phases attach routers (days, exports, web UI)
and APScheduler jobs (poller, sessioniser, backup) here.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.days import router as days_router
from app.api.exports import router as exports_router
from app.api.health import router as health_router
from app.backup.snapshot import run_snapshot
from app.config import Settings, get_settings
from app.db import get_engine, get_sessionmaker, init_engine, install_triggers_now
from app.models import Config, PollerState
from app.sessions.scheduler import register_scheduler_jobs
from app.web.routes import router as web_router

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def seed_config_if_missing(db: Session, settings: Settings) -> Config:
    """Insert the singleton Config row from `.env` values if absent.

    If a row exists already, log a WARNING if any sessionisation-relevant
    value in `.env` differs from the DB (the DB is authoritative — never
    overwritten). Returns the live Config row.
    """
    existing = db.execute(select(Config).limit(1)).scalar_one_or_none()
    if existing is not None:
        env_view = {
            "gap_bridge_minutes": settings.gap_bridge_minutes,
            "min_session_minutes": settings.min_session_minutes,
            "daily_cap_hours": settings.daily_cap_hours,
            "local_timezone": settings.local_timezone,
            "rule_version": settings.rule_version,
            "work_ssid": settings.work_ssid,
        }
        db_view = {
            "gap_bridge_minutes": existing.gap_bridge_minutes,
            "min_session_minutes": existing.min_session_minutes,
            "daily_cap_hours": existing.daily_cap_hours,
            "local_timezone": existing.local_timezone,
            "rule_version": existing.rule_version,
            "work_ssid": existing.work_ssid,
        }
        diffs = {k: (env_view[k], db_view[k]) for k in env_view if env_view[k] != db_view[k]}
        if diffs:
            logger.warning(
                "config: .env disagrees with DB on %s; DB wins (per ARCHITECTURE §7.5)",
                ", ".join(diffs),
            )
        return existing

    cfg = Config(
        work_ssid=settings.work_ssid,
        gap_bridge_minutes=settings.gap_bridge_minutes,
        min_session_minutes=settings.min_session_minutes,
        daily_cap_hours=settings.daily_cap_hours,
        local_timezone=settings.local_timezone,
        rule_version=settings.rule_version,
        updated_at=_utcnow(),
    )
    db.add(cfg)
    db.flush()
    logger.info("config: seeded from .env (rule_version=%s)", cfg.rule_version)
    return cfg


def ensure_poller_state(db: Session) -> PollerState:
    """Ensure the singleton PollerState row exists."""
    state = db.execute(select(PollerState).limit(1)).scalar_one_or_none()
    if state is None:
        state = PollerState(consecutive_failures=0)
        db.add(state)
        db.flush()
    return state


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    init_engine(settings)
    install_triggers_now()
    SessionLocal = get_sessionmaker()  # noqa: N806 (SQLAlchemy convention)
    with SessionLocal() as db:
        cfg = seed_config_if_missing(db, settings)
        ensure_poller_state(db)
        db.commit()
        timezone_name = cfg.local_timezone

    scheduler = BackgroundScheduler()
    register_scheduler_jobs(scheduler, timezone_name)
    # Nightly backup at 02:00 local (ARCHITECTURE §7.1).
    from apscheduler.triggers.cron import CronTrigger

    scheduler.add_job(
        run_snapshot,
        trigger=CronTrigger(hour=2, minute=0, timezone=ZoneInfo(timezone_name)),
        args=[settings.data_dir / "backups", timezone_name],
        id="nightly_backup",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    scheduler.start()
    logger.info("wfh-logbook started")
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)
        engine = get_engine()
        engine.dispose()
        logger.info("wfh-logbook stopped")


def create_app() -> FastAPI:
    """Build and return the FastAPI application."""
    app = FastAPI(
        title="WFH Logbook",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.include_router(health_router)
    app.include_router(days_router)
    app.include_router(exports_router)
    app.include_router(web_router)
    # Static assets — vendored, never CDN (CLAUDE.md / HANDOFF §6 Phase 4).
    static_dir = Path(__file__).resolve().parent / "web" / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    return app


app = create_app()
