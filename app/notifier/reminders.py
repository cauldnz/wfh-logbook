"""Proactive lock-backlog reminders (HANDOFF §6 Phase 10.A).

A daily scheduled job that, when unlocked days have accumulated, sends one
Telegram message per allowed user nudging a review-and-lock. Detection reuses
the review queue; delivery reuses the Notifier; every send is recorded to
``bot_messages`` like any other outbound. Silent when there is nothing to nudge
about or no allowed users. The per-day reasons are never logged above DEBUG.
"""

from __future__ import annotations

import functools
import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.orm import Session, sessionmaker

from app.api.review_queue import ReviewQueueResponse, build_review_queue
from app.config import Settings
from app.db import get_sessionmaker
from app.notifier.base import Notifier, OutgoingMessage
from app.notifier.service import record_outbound

logger = logging.getLogger(__name__)

# Reasons that mean a day needs a careful look before locking — i.e. it is NOT
# safe for one-tap bulk lock. Mirrors the exclusions in lock_clean_days.
_FLAG_REASONS = ("anomalous", "data_gap", "heavy_bridging", "long_session", "suspect_zero")
_MAX_LISTED = 7


def _hhmm(seconds: int) -> str:
    h, m = divmod(max(0, seconds) // 60, 60)
    return f"{h}:{m:02d}"


def build_reminder_text(
    queue: ReviewQueueResponse,
    today: date,
    threshold_days: int,
) -> str | None:
    """Compose the reminder, or ``None`` when there is nothing worth nudging.

    Lists unlocked days at least ``threshold_days`` old, oldest first, marking
    days that need correction (anomalies/flags) so the user can tell which are
    safe for ``/lockall`` and which need a look. Tone escalates with the oldest
    age (gentle ≤ 7 days; overdue beyond).
    """
    unlocked = [
        i
        for i in queue.items
        if not i.locked
        and "unlocked_backlog" in i.reasons
        and (today - i.local_date).days >= threshold_days
    ]
    if not unlocked:
        return None
    unlocked.sort(key=lambda i: i.local_date)
    oldest_age = (today - unlocked[0].local_date).days
    n = len(unlocked)
    flagged = sum(1 for i in unlocked if any(r in _FLAG_REASONS for r in i.reasons))
    clean = n - flagged

    head = "⚠ Overdue: " if oldest_age > 7 else "🔒 "
    lines = [
        f"{head}{n} unlocked day{'s' if n != 1 else ''} "
        f"(oldest {oldest_age} day{'s' if oldest_age != 1 else ''} ago)."
    ]
    for i in unlocked[:_MAX_LISTED]:
        mark = " ⚠" if any(r in _FLAG_REASONS for r in i.reasons) else ""
        lines.append(f"  {i.local_date.isoformat()}  {_hhmm(i.claimed_seconds or 0)}{mark}")
    if n > _MAX_LISTED:
        lines.append(f"  …and {n - _MAX_LISTED} more.")
    if flagged and clean:
        lines.append(
            f"⚠ = needs a look. /lockall locks the {clean} clean one(s); /yesterday to review."
        )
    elif flagged:
        lines.append("All need a look before locking — /yesterday to review & correct.")
    else:
        lines.append("All clean — /lockall to lock them, or /yesterday to review.")
    return "\n".join(lines)


def run_lock_reminder(
    notifier: Notifier,
    settings: Settings,
    session_factory: sessionmaker[Session],
    tz_name: str,
) -> int:
    """Build the reminder and send it to each allowed user. Returns send count."""
    allowed = settings.parsed_allowed_user_ids()
    if not allowed:
        return 0
    with session_factory() as db:
        today = datetime.now(ZoneInfo(tz_name)).date()
        queue = build_review_queue(db, today)
        text = build_reminder_text(queue, today, settings.lock_reminder_threshold_days)
        if text is None:
            logger.info("reminder: no unlocked backlog; nothing sent")
            return 0
        sent = 0
        for chat_id in allowed:
            try:
                result = notifier.send(OutgoingMessage(chat_id=chat_id, text=text))
            except Exception:
                logger.exception("reminder: send failed for chat %s", chat_id)
                continue
            record_outbound(db, chat_id, text, result.message_id, {"kind": "lock_reminder"})
            sent += 1
        db.commit()
    logger.info("reminder: lock-backlog nudge sent to %d user(s)", sent)
    return sent


def register_reminder_job(
    scheduler: BackgroundScheduler,
    notifier: Notifier,
    settings: Settings,
    tz_name: str,
) -> None:
    """Register the daily lock-reminder job (HANDOFF §6 Phase 10.A)."""
    session_factory = get_sessionmaker()
    scheduler.add_job(
        functools.partial(run_lock_reminder, notifier, settings, session_factory, tz_name),
        trigger=CronTrigger(hour=settings.lock_reminder_hour, minute=0, timezone=ZoneInfo(tz_name)),
        id="lock_reminder",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
