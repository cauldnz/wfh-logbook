"""CLI entry point: ``python -m app.sessions --date 2026-05-20``.

Useful for ad-hoc re-runs (e.g. after fixing observations or changing
sessionisation rules). With ``--dry-run`` (Phase 6), the run does not commit;
useful for "what would happen if?" exploration.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.db import get_sessionmaker, init_engine
from app.sessions.persistence import dates_needing_resessionisation, sessionise_date
from app.sessions.rules import RuleSet

logger = logging.getLogger(__name__)


def _parse_date(s: str) -> date:
    return date.fromisoformat(s)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.sessions",
        description="Re-run sessionisation for one date, or for the nightly window.",
    )
    grp = parser.add_mutually_exclusive_group(required=True)
    grp.add_argument("--date", type=_parse_date, help="Single local date (YYYY-MM-DD).")
    grp.add_argument(
        "--nightly-window",
        action="store_true",
        help="Sessionise yesterday plus any non-locked date in the trailing 7 days.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute and log, but roll back instead of committing.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    settings = get_settings()
    init_engine(settings)
    SessionLocal = get_sessionmaker()  # noqa: N806 (SQLAlchemy convention)

    with SessionLocal() as db:
        rules = RuleSet.from_db(db)
        if args.nightly_window:
            today_local = datetime.now(ZoneInfo(rules.local_timezone)).date()
            dates = dates_needing_resessionisation(db, today_local)
        else:
            dates = [args.date]
        if not dates:
            logger.info("no dates to process")
            return 0
        for d in dates:
            result = sessionise_date(db, d, rules)
            logger.info(
                "  %s: sessions=%d computed_seconds=%d version=%d changed=%s",
                d.isoformat(),
                result.sessions_built,
                result.computed_seconds,
                result.daily_summary_version,
                result.daily_summary_changed,
            )
        if args.dry_run:
            db.rollback()
            logger.warning("--dry-run: changes ROLLED BACK")
        else:
            db.commit()
    return 0


if __name__ == "__main__":
    sys.exit(main())


# Silence unused-import warnings for the timedelta convenience re-export above.
_ = timedelta
