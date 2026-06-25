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

from app.api.backups import router as backups_router
from app.api.days import router as days_router
from app.api.exports import router as exports_router
from app.api.health import router as health_router
from app.api.review_queue import router as review_queue_router
from app.backup.snapshot import run_snapshot
from app.config import Settings, get_settings
from app.db import get_engine, get_sessionmaker, init_engine, install_triggers_now
from app.models import Config, Device, PollerState
from app.notifier.webhook import router as telegram_webhook_router
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


def seed_devices_if_missing(db: Session, settings: Settings) -> None:
    """Insert ``devices`` rows from WORK_DEVICE_MACS for MACs not yet tracked.

    `.env` seeds; the DB is authoritative thereafter (same contract as the
    config row). An existing active row with a different label is reported
    but never modified — label changes are a deliberate DB edit, and MAC
    rotation is handled by end-dating per ARCHITECTURE §4.4.
    """
    for mac, label in settings.parsed_device_macs():
        existing = db.execute(
            select(Device).where(Device.mac == mac).where(Device.active_to.is_(None))
        ).scalar_one_or_none()
        if existing is None:
            db.add(Device(mac=mac, label=label, active_from=_utcnow(), active_to=None))
            logger.info("devices: tracking %s as %r (seeded from .env)", mac, label)
        elif existing.label != label:
            logger.warning(
                "devices: %s has label %r in DB but %r in .env; DB wins",
                mac,
                existing.label,
                label,
            )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    import asyncio

    from app.logging_config import configure_logging

    settings = get_settings()
    configure_logging(level=settings.log_level, structured=settings.log_format == "json")
    init_engine(settings)
    install_triggers_now()
    SessionLocal = get_sessionmaker()  # noqa: N806 (SQLAlchemy convention)
    with SessionLocal() as db:
        cfg = seed_config_if_missing(db, settings)
        ensure_poller_state(db)
        seed_devices_if_missing(db, settings)
        db.commit()
        timezone_name = cfg.local_timezone
        work_ssid = cfg.work_ssid

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

    # UniFi poller (Phase 2). Disabled with a single INFO line if no
    # controller is configured — the rest of the app works without it.
    adapter = None
    if settings.unifi_host and work_ssid:
        from app.unifi.client import create_adapter
        from app.unifi.poller import register_poller_job

        try:
            adapter = create_adapter(settings)
            register_poller_job(scheduler, adapter, settings, work_ssid)
        except Exception:
            # Startup must not die because the controller is unreachable or
            # unsupported; health surfaces the absence of polls.
            logger.exception("unifi: poller not started")
    else:
        logger.info("unifi: no UNIFI_HOST/WORK_SSID configured; poller disabled")

    scheduler.start()

    # Telegram bot (Phase 7). Disabled entirely with one INFO line when no
    # token is configured (HANDOFF 7.G) — the rest of the app is unaffected.
    telegram_client = None
    polling_task: asyncio.Task[None] | None = None
    polling_stop: asyncio.Event | None = None
    if settings.telegram_bot_token:
        from app.notifier.polling import polling_loop
        from app.notifier.telegram import TelegramClient

        telegram_client = TelegramClient(settings.telegram_bot_token)
        app.state.telegram_client = telegram_client
        # Daily lock-backlog reminder (Phase 10.A) — works in either mode,
        # since sending is independent of how updates are received.
        from app.notifier.reminders import register_reminder_job

        register_reminder_job(scheduler, telegram_client, settings, timezone_name)
        if settings.telegram_mode == "webhook":
            if settings.public_base_url and settings.telegram_webhook_secret:
                try:
                    telegram_client.set_webhook(
                        f"{settings.public_base_url.rstrip('/')}"
                        f"/webhook/telegram/{settings.telegram_webhook_secret}",
                        settings.telegram_webhook_secret,
                    )
                except Exception:
                    logger.exception("telegram: webhook registration failed")
            else:
                logger.error(
                    "telegram: webhook mode needs PUBLIC_BASE_URL and "
                    "TELEGRAM_WEBHOOK_SECRET; bot inactive"
                )
        else:  # polling
            polling_stop = asyncio.Event()
            polling_task = asyncio.get_running_loop().create_task(
                polling_loop(telegram_client, settings, polling_stop)
            )
        logger.info("telegram: bot enabled (mode=%s)", settings.telegram_mode)
    else:
        logger.info("telegram: no TELEGRAM_BOT_TOKEN configured; bot disabled")

    logger.info("wfh-logbook started")
    try:
        yield
    finally:
        if polling_stop is not None:
            polling_stop.set()
        if polling_task is not None:
            polling_task.cancel()
        if telegram_client is not None:
            telegram_client.close()
        scheduler.shutdown(wait=False)
        if adapter is not None:
            adapter.close()
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
    app.include_router(review_queue_router)
    app.include_router(backups_router)
    app.include_router(telegram_webhook_router)
    app.include_router(web_router)
    # Static assets — vendored, never CDN (CLAUDE.md / HANDOFF §6 Phase 4).
    static_dir = Path(__file__).resolve().parent / "web" / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    return app


app = create_app()
