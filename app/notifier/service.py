"""Bot service: raw update → evidence → conversation → transport (7.C/7.E/7.G).

Ordering rules this module owes the audit trail:

1. The raw inbound update is persisted to ``bot_messages`` BEFORE any
   processing (HANDOFF 7.C) in its own committed transaction — if
   processing crashes, the evidence survives.
2. Idempotency rides on the partial unique index over
   ``telegram_update_id``: a replayed update hits IntegrityError on that
   first insert and the whole processing step is skipped.
3. Every outbound message is also persisted to ``bot_messages``.
4. Adjust / confirm / lock run through ``days_service`` — the same code
   paths as the web UI, ``created_by='telegram'`` (HANDOFF 7.G).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.days_service import AdjustParams, adjust_day, get_day, lock_day
from app.config import Settings
from app.models import BotChat, BotMessage, BotState, Config, DailySummary, PollerState
from app.notifier.base import (
    AnswerCallback,
    ApplyAdjustment,
    ApplyConfirm,
    ApplyLock,
    ApplyRebuild,
    Button,
    ClearAwaiting,
    DayView,
    EditMessage,
    IncomingEvent,
    Notifier,
    OutgoingMessage,
    SendMessage,
    SessionView,
    SetAwaiting,
    StatusView,
    WeekDayTotal,
    YearView,
)
from app.notifier.conversation import handle_event, render_day_text
from app.notifier.telegram import parse_update
from app.web.routes import current_fy_label, fy_bounds

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _ensure_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


# --------------------------------------------------------------- db reader


@dataclass
class SqlDbReader:
    """DbReader implementation over the live database (read-only)."""

    db: Session
    tz_name: str
    daily_cap_hours: int

    def _tz(self) -> ZoneInfo:
        return ZoneInfo(self.tz_name)

    def today(self) -> date:
        return datetime.now(self._tz()).date()

    def day_view(self, target_date: date) -> DayView | None:
        detail = get_day(self.db, target_date)
        if detail.latest is None:
            return None
        tz = self._tz()
        sessions = tuple(
            SessionView(
                start_hhmm=s.started_at.astimezone(tz).strftime("%H:%M"),
                end_hhmm=s.ended_at.astimezone(tz).strftime("%H:%M"),
                duration_seconds=s.duration_seconds,
            )
            for s in detail.sessions
        )
        latest = detail.latest
        return DayView(
            local_date=target_date,
            computed_seconds=latest.computed_seconds,
            adjustment_seconds=latest.adjustment_seconds,
            adjustment_reason=latest.adjustment_reason,
            claimed_seconds=latest.claimed_seconds,
            version=latest.version,
            locked=latest.locked,
            anomalous=latest.anomalous,
            sessions=sessions,
        )

    def week_totals(self) -> list[WeekDayTotal]:
        today = self.today()
        out: list[WeekDayTotal] = []
        for i in range(7, 0, -1):
            d = today - timedelta(days=i)
            row = self.db.execute(
                select(DailySummary)
                .where(DailySummary.local_date == d.isoformat())
                .order_by(DailySummary.version.desc())
                .limit(1)
            ).scalar_one_or_none()
            if row is not None:
                out.append(WeekDayTotal(local_date=d, claimed_seconds=row.claimed_seconds))
        return out

    def year_view(self) -> YearView:
        today = self.today()
        fy = current_fy_label(today)
        fy_start, fy_end = fy_bounds(fy)
        rows = list(
            self.db.execute(
                select(DailySummary)
                .where(DailySummary.local_date >= fy_start.isoformat())
                .where(DailySummary.local_date <= fy_end.isoformat())
                .order_by(DailySummary.local_date.asc(), DailySummary.version.desc())
            ).scalars()
        )
        latest_by_date: dict[str, DailySummary] = {}
        for r in rows:
            latest_by_date.setdefault(r.local_date, r)
        total = sum(r.claimed_seconds for r in latest_by_date.values())
        locked = sum(1 for r in latest_by_date.values() if bool(r.locked))
        return YearView(
            fy_label=fy,
            total_claimed_seconds=total,
            locked_days=locked,
            unlocked_days=len(latest_by_date) - locked,
        )

    def status_view(self) -> StatusView:
        state = self.db.execute(select(PollerState).limit(1)).scalar_one_or_none()
        tz = self._tz()

        def fmt(dt: datetime | None) -> str | None:
            aware = _ensure_utc(dt)
            return aware.astimezone(tz).strftime("%Y-%m-%d %H:%M") if aware else None

        if state is None:
            return StatusView(None, None, None, 0)
        return StatusView(
            last_poll_succeeded_at=fmt(state.last_poll_succeeded_at),
            last_sessioniser_run_at=fmt(state.last_sessioniser_run_at),
            last_backup_at=fmt(state.last_backup_at),
            consecutive_failures=state.consecutive_failures,
        )


# ------------------------------------------------------------ persistence


def record_inbound(db: Session, raw: dict[str, Any], event_chat_id: int | None) -> bool:
    """Persist the raw update BEFORE processing. False = duplicate (skip).

    Commits its own transaction so the evidence row survives any later
    processing crash (HANDOFF 7.C).
    """
    msg_obj = raw.get("message") or (raw.get("callback_query") or {}).get("message") or {}
    text = msg_obj.get("text") if isinstance(msg_obj, dict) else None
    if "callback_query" in raw:
        text = (raw["callback_query"] or {}).get("data")
    row = BotMessage(
        chat_id=event_chat_id or 0,
        direction="in",
        telegram_update_id=raw.get("update_id"),
        telegram_message_id=None,
        text=text if isinstance(text, str) else None,
        raw_json=json.dumps(raw, separators=(",", ":")),
        created_at=_utcnow(),
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        logger.info(
            "telegram: duplicate update_id %s — already processed, skipping",
            raw.get("update_id"),
        )
        return False
    return True


def record_outbound(
    db: Session,
    chat_id: int,
    text: str | None,
    telegram_message_id: int | None,
    raw: dict[str, Any],
) -> None:
    db.add(
        BotMessage(
            chat_id=chat_id,
            direction="out",
            telegram_update_id=None,
            telegram_message_id=telegram_message_id,
            text=text,
            raw_json=json.dumps(raw, separators=(",", ":")),
            created_at=_utcnow(),
        )
    )


# -------------------------------------------------------------- chat state


def upsert_chat(db: Session, event: IncomingEvent, allowed_ids: list[int]) -> BotChat:
    """Create/update the bot_chats row; refresh authorisation each contact."""
    now = _utcnow()
    chat = db.get(BotChat, event.chat_id)
    authorised = event.user_id in allowed_ids
    if chat is None:
        chat = BotChat(
            chat_id=event.chat_id,
            telegram_user_id=event.user_id,
            authorised=authorised,
            rejection_sent=False,
            first_seen_at=now,
            last_seen_at=now,
        )
        db.add(chat)
        db.flush()
    else:
        chat.authorised = authorised  # refreshed per ARCH §4.5
        chat.last_seen_at = now
    return chat


def load_state(db: Session, chat_id: int) -> BotState | None:
    return db.get(BotState, chat_id)


def set_awaiting(db: Session, chat_id: int, awaiting_date: date) -> None:
    state = db.get(BotState, chat_id)
    now = _utcnow()
    if state is None:
        state = BotState(
            chat_id=chat_id,
            awaiting="adjustment",
            awaiting_date=awaiting_date.isoformat(),
            updated_at=now,
        )
        db.add(state)
    else:
        state.awaiting = "adjustment"
        state.awaiting_date = awaiting_date.isoformat()
        state.updated_at = now


def clear_awaiting(db: Session, chat_id: int) -> None:
    state = db.get(BotState, chat_id)
    if state is not None:
        state.awaiting = None
        state.awaiting_date = None
        state.updated_at = _utcnow()


# ----------------------------------------------------------- apply helpers

LOCK_READJUST_BUTTONS_HINT = "post-adjust reply buttons per HANDOFF 7.F"


def _post_adjust_buttons(target_date: date) -> tuple[tuple[Button, ...], ...]:
    d = target_date.isoformat()
    return ((Button("🔒 Lock", f"lock:{d}"), Button("✏ Re-adjust", f"adjust:{d}")),)


def _locked_text(reader: SqlDbReader, target_date: date) -> str:
    view = reader.day_view(target_date)
    return render_day_text(view) if view else f"{target_date.isoformat()} locked."


# ----------------------------------------------------------------- engine


def process_update(
    db: Session,
    raw: dict[str, Any],
    notifier: Notifier,
    settings: Settings,
    now: datetime | None = None,
) -> None:
    """Full pipeline for one raw update. Never raises on user errors;
    transport/DB errors propagate to the caller (webhook returns 500,
    polling logs and continues)."""
    now = now or _utcnow()
    event = parse_update(raw, now=now)

    # Evidence first — even for update kinds we don't handle.
    if not record_inbound(db, raw, event.chat_id if event else None):
        return  # duplicate: single side effect already happened (7.C)
    if event is None:
        return

    cfg = db.execute(select(Config).limit(1)).scalar_one()
    reader = SqlDbReader(db=db, tz_name=cfg.local_timezone, daily_cap_hours=cfg.daily_cap_hours)

    chat = upsert_chat(db, event, settings.parsed_allowed_user_ids())
    event = _with_auth(
        event, authorised=bool(chat.authorised), rejection_sent=bool(chat.rejection_sent)
    )

    state = load_state(db, event.chat_id)
    awaiting = state.awaiting if state else None
    awaiting_date = (
        date.fromisoformat(state.awaiting_date) if state and state.awaiting_date else None
    )
    awaiting_age = (
        now - _ensure_utc(state.updated_at) if state and state.updated_at else None  # type: ignore[operator]
    )

    actions = handle_event(event, awaiting, awaiting_date, awaiting_age, reader)

    if not event.authorised and actions:
        # The one rejection is about to be sent; remember it durably (7.E).
        chat.rejection_sent = True

    for action in actions:
        _execute(db, action, event, notifier, reader)

    db.commit()


def _with_auth(event: IncomingEvent, *, authorised: bool, rejection_sent: bool) -> IncomingEvent:
    from dataclasses import replace

    return replace(event, authorised=authorised, rejection_already_sent=rejection_sent)


def _send(
    db: Session,
    notifier: Notifier,
    chat_id: int,
    text: str,
    buttons: tuple[tuple[Button, ...], ...] = (),
) -> None:
    msg = OutgoingMessage(chat_id=chat_id, text=text, buttons=buttons)
    sent = notifier.send(msg)
    record_outbound(db, chat_id, text, sent.message_id, {"kind": "sendMessage"})


def _execute(
    db: Session,
    action: object,
    event: IncomingEvent,
    notifier: Notifier,
    reader: SqlDbReader,
) -> None:
    chat_id = event.chat_id

    if isinstance(action, SendMessage):
        _send(db, notifier, chat_id, action.text, action.buttons)

    elif isinstance(action, EditMessage):
        notifier.edit(
            chat_id,
            action.message_id,
            OutgoingMessage(chat_id=chat_id, text=action.text, buttons=action.buttons),
        )
        record_outbound(db, chat_id, action.text, action.message_id, {"kind": "editMessageText"})

    elif isinstance(action, AnswerCallback):
        notifier.answer_callback(action.callback_query_id, action.text)
        record_outbound(
            db,
            chat_id,
            action.text,
            None,
            {"kind": "answerCallbackQuery", "callback_query_id": action.callback_query_id},
        )

    elif isinstance(action, SetAwaiting):
        set_awaiting(db, chat_id, action.awaiting_date)

    elif isinstance(action, ClearAwaiting):
        clear_awaiting(db, chat_id)

    elif isinstance(action, ApplyAdjustment):
        try:
            adjust_day(
                db,
                action.target_date,
                AdjustParams(
                    adjustment_seconds=action.minutes * 60,
                    reason=action.reason,
                    created_by="telegram",
                ),
            )
        except HTTPException as e:
            _send(db, notifier, chat_id, f"Couldn't adjust: {e.detail}")
            return
        view = reader.day_view(action.target_date)
        text = render_day_text(view) if view else "Adjusted."
        # 7.F: reply with new state plus [🔒 Lock] [✏ Re-adjust].
        _send(db, notifier, chat_id, text, _post_adjust_buttons(action.target_date))
        logger.info(
            "telegram: adjustment applied date=%s minutes=%+d version=%s",
            action.target_date.isoformat(),
            action.minutes,
            view.version if view else "?",
        )

    elif isinstance(action, ApplyConfirm):
        try:
            adjust_day(
                db,
                action.target_date,
                AdjustParams(
                    adjustment_seconds=0,
                    reason="Confirmed via Telegram",
                    created_by="telegram",
                ),
            )
        except HTTPException as e:
            _send(db, notifier, chat_id, f"Couldn't confirm: {e.detail}")
            return
        view = reader.day_view(action.target_date)
        text = (render_day_text(view) + "\n✓ confirmed") if view else "Confirmed."
        if action.message_id is not None:
            # Spec: edit the message to reflect the new state.
            notifier.edit(chat_id, action.message_id, OutgoingMessage(chat_id=chat_id, text=text))
            record_outbound(db, chat_id, text, action.message_id, {"kind": "editMessageText"})
        else:
            _send(db, notifier, chat_id, text)

    elif isinstance(action, ApplyLock):
        try:
            lock_day(db, action.target_date)
        except HTTPException as e:
            _send(db, notifier, chat_id, f"Couldn't lock: {e.detail}")
            return
        text = _locked_text(reader, action.target_date)
        if action.message_id is not None:
            # Spec: show locked state, REMOVE Adjust/Lock buttons.
            notifier.edit(chat_id, action.message_id, OutgoingMessage(chat_id=chat_id, text=text))
            record_outbound(db, chat_id, text, action.message_id, {"kind": "editMessageText"})
        else:
            _send(db, notifier, chat_id, text)
        logger.info("telegram: day locked date=%s", action.target_date.isoformat())

    elif isinstance(action, ApplyRebuild):
        from app.notifier.conversation import day_buttons
        from app.sessions.persistence import sessionise_date
        from app.sessions.rules import RuleSet

        rules = RuleSet.from_db(db)
        result = sessionise_date(db, action.target_date, rules)
        view = reader.day_view(action.target_date)
        if view is None:  # pragma: no cover - sessionise always creates v1
            _send(db, notifier, chat_id, "Rebuild ran but produced no summary.")
            return
        is_today = action.target_date == reader.today()
        text = render_day_text(view, today=is_today)
        if action.announce:
            text += (
                f"\n(rebuilt: {result.sessions_built} session(s), v{result.daily_summary_version})"
            )
        _send(db, notifier, chat_id, text, day_buttons(view, today=is_today))
        logger.info(
            "telegram: rebuild date=%s sessions=%d version=%d",
            action.target_date.isoformat(),
            result.sessions_built,
            result.daily_summary_version,
        )

    else:  # pragma: no cover - defensive
        logger.error("telegram: unknown action type %s", type(action).__name__)
