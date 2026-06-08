"""APScheduler wiring for the nightly sessioniser job.

Runs at 01:15 local time. For each date in the trailing 7 days that is not
locked, plus "yesterday" unconditionally, re-runs ``sessionise_date``.
"""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.db import get_sessionmaker
from app.sessions.persistence import dates_needing_resessionisation, sessionise_date
from app.sessions.rules import RuleSet

logger = logging.getLogger(__name__)


def run_nightly_sessioniser(timezone_name: str) -> None:
    """The scheduled callable. Reads fresh rules + dates each run."""
    SessionLocal = get_sessionmaker()  # noqa: N806 (SQLAlchemy convention)
    with SessionLocal() as db:
        try:
            rules = RuleSet.from_db(db)
            today_local = datetime.now(ZoneInfo(timezone_name)).date()
            dates = dates_needing_resessionisation(db, today_local)
            logger.info(
                "sessioniser nightly: %d date(s) to process: %s",
                len(dates),
                ", ".join(d.isoformat() for d in dates),
            )
            for d in dates:
                sessionise_date(db, d, rules)
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("sessioniser nightly failed")
            raise


def register_scheduler_jobs(
    scheduler: BackgroundScheduler,
    timezone_name: str,
) -> None:
    """Register the 01:15-local nightly job."""
    scheduler.add_job(
        run_nightly_sessioniser,
        trigger=CronTrigger(hour=1, minute=15, timezone=ZoneInfo(timezone_name)),
        args=[timezone_name],
        id="nightly_sessioniser",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    logger.info("scheduler: nightly_sessioniser registered (01:15 %s)", timezone_name)
