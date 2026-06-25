"""Lock-backlog reminders (HANDOFF §6 Phase 10.A)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import time_machine
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.review_queue import ReviewQueueItem, ReviewQueueResponse
from app.config import Settings, get_settings
from app.db import get_sessionmaker
from app.main import seed_config_if_missing
from app.models import BotMessage, DailySummary
from app.notifier.base import OutgoingMessage, SentMessage
from app.notifier.reminders import build_reminder_text, run_lock_reminder

TODAY = date(2026, 6, 25)


def _item(
    d: date, reasons: list[str], claimed_h: float = 5.0, *, locked: bool = False
) -> ReviewQueueItem:
    return ReviewQueueItem(
        local_date=d,
        reasons=reasons,
        claimed_seconds=int(claimed_h * 3600),
        version=1,
        locked=locked,
    )


def _queue(items: list[ReviewQueueItem]) -> ReviewQueueResponse:
    return ReviewQueueResponse(today=TODAY, from_date=date(2026, 3, 27), items=items)


class TestReminderText:
    def test_empty_queue_no_message(self) -> None:
        assert build_reminder_text(_queue([]), TODAY, 1) is None

    def test_locked_items_excluded(self) -> None:
        q = _queue([_item(date(2026, 6, 20), ["anomalous"], locked=True)])
        assert build_reminder_text(q, TODAY, 1) is None

    def test_clean_backlog_lists_and_offers_lockall(self) -> None:
        q = _queue([_item(date(2026, 6, 24), ["unlocked_backlog"], 5)])
        text = build_reminder_text(q, TODAY, 1)
        assert text is not None
        assert "1 unlocked day" in text
        assert "2026-06-24" in text
        assert "/lockall" in text
        assert "⚠" not in text  # nothing flagged

    def test_flagged_day_marked_and_needs_look(self) -> None:
        q = _queue([_item(date(2026, 6, 19), ["unlocked_backlog", "anomalous"], 26)])
        text = build_reminder_text(q, TODAY, 1)
        assert text is not None
        assert "⚠" in text
        assert "look" in text.lower()

    def test_mixed_offers_lockall_for_clean_only(self) -> None:
        q = _queue(
            [
                _item(date(2026, 6, 24), ["unlocked_backlog"], 5),
                _item(date(2026, 6, 19), ["unlocked_backlog", "long_session"], 26),
            ]
        )
        text = build_reminder_text(q, TODAY, 1)
        assert text is not None
        assert "/lockall locks the 1 clean" in text

    def test_overdue_escalation_beyond_7_days(self) -> None:
        q = _queue([_item(date(2026, 6, 10), ["unlocked_backlog"], 5)])  # 15 days old
        text = build_reminder_text(q, TODAY, 1)
        assert text is not None
        assert "Overdue" in text

    def test_threshold_excludes_too_recent(self) -> None:
        q = _queue([_item(date(2026, 6, 24), ["unlocked_backlog"], 5)])  # 1 day old
        assert build_reminder_text(q, TODAY, 2) is None


class FakeNotifier:
    """Records sends; never touches the network."""

    def __init__(self) -> None:
        self.sent: list[OutgoingMessage] = []

    def send(self, message: OutgoingMessage) -> SentMessage:
        self.sent.append(message)
        return SentMessage(chat_id=message.chat_id, message_id=len(self.sent))

    def edit(self, chat_id: int, message_id: int, message: OutgoingMessage) -> None:
        raise AssertionError("unused")

    def answer_callback(self, callback_query_id: str, text: str | None = None) -> None:
        raise AssertionError("unused")


def _settings_with_allowed(allowed: str, web_base_url: str = "") -> Settings:
    return get_settings().model_copy(
        update={"telegram_allowed_user_ids": allowed, "web_base_url": web_base_url}
    )


def _seed_unlocked_day(db: Session, local_date: str, hours: float) -> None:
    db.add(
        DailySummary(
            local_date=local_date,
            version=1,
            computed_seconds=int(hours * 3600),
            adjustment_seconds=0,
            adjustment_reason=None,
            claimed_seconds=int(hours * 3600),
            locked=False,
            locked_at=None,
            created_at=datetime(2026, 6, 21, tzinfo=UTC),
            created_by="sessioniser",
            rule_version="2026.1",
        )
    )


class TestRunLockReminder:
    def test_sends_to_each_allowed_user_and_records(self, db_session: Session) -> None:
        seed_config_if_missing(db_session, get_settings())
        _seed_unlocked_day(db_session, "2026-06-20", 5)
        db_session.commit()

        notifier = FakeNotifier()
        settings = _settings_with_allowed("111,222")
        with time_machine.travel(datetime(2026, 6, 25, 2, 0, tzinfo=UTC), tick=False):
            sent = run_lock_reminder(notifier, settings, get_sessionmaker(), "Australia/Sydney")

        assert sent == 2
        assert {m.chat_id for m in notifier.sent} == {111, 222}
        # Each send is audit-recorded to bot_messages (read via a fresh session).
        with get_sessionmaker()() as check:
            out = (
                check.execute(select(BotMessage).where(BotMessage.direction == "out"))
                .scalars()
                .all()
            )
        assert len(out) == 2

    def test_reminder_carries_review_queue_button(self, db_session: Session) -> None:
        seed_config_if_missing(db_session, get_settings())
        _seed_unlocked_day(db_session, "2026-06-20", 5)
        db_session.commit()
        notifier = FakeNotifier()
        settings = _settings_with_allowed("111", web_base_url="http://wtrmax.local:8088")
        with time_machine.travel(datetime(2026, 6, 25, 2, 0, tzinfo=UTC), tick=False):
            run_lock_reminder(notifier, settings, get_sessionmaker(), "Australia/Sydney")
        assert notifier.sent[0].buttons[0][0].url == "http://wtrmax.local:8088/review-queue"

    def test_no_allowed_users_sends_nothing(self, db_session: Session) -> None:
        seed_config_if_missing(db_session, get_settings())
        _seed_unlocked_day(db_session, "2026-06-20", 5)
        db_session.commit()
        notifier = FakeNotifier()
        sent = run_lock_reminder(
            notifier, _settings_with_allowed(""), get_sessionmaker(), "Australia/Sydney"
        )
        assert sent == 0
        assert notifier.sent == []

    def test_empty_backlog_sends_nothing(self, db_session: Session) -> None:
        seed_config_if_missing(db_session, get_settings())
        db_session.commit()
        notifier = FakeNotifier()
        with time_machine.travel(datetime(2026, 6, 25, 2, 0, tzinfo=UTC), tick=False):
            sent = run_lock_reminder(
                notifier, _settings_with_allowed("111"), get_sessionmaker(), "Australia/Sydney"
            )
        assert sent == 0
