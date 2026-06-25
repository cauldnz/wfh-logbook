"""Phase 10 /lockall bot command (HANDOFF §6 Phase 10.B)."""

from __future__ import annotations

from datetime import UTC, datetime

import time_machine
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.main import seed_config_if_missing
from app.models import DailySummary
from app.notifier.base import ApplyBulkLock, SendMessage
from app.notifier.conversation import handle_event
from app.notifier.service import process_update
from tests.test_telegram_conversation import StubReader, command
from tests.test_telegram_service import FakeNotifier, bot_settings, msg_update


def _summary(db: Session, local_date: str, hours: float, *, locked: bool = False) -> None:
    db.add(
        DailySummary(
            local_date=local_date,
            version=1,
            computed_seconds=int(hours * 3600),
            adjustment_seconds=0,
            adjustment_reason=None,
            claimed_seconds=int(hours * 3600),
            locked=locked,
            locked_at=None,
            created_at=datetime(2026, 6, 21, tzinfo=UTC),
            created_by="sessioniser",
            rule_version="2026.1",
        )
    )


class TestLockAllCommand:
    def test_lockall_returns_bulk_lock_action(self) -> None:
        actions = handle_event(command("/lockall"), None, None, None, StubReader())
        assert any(isinstance(a, ApplyBulkLock) for a in actions)

    def test_lockall_in_help(self) -> None:
        actions = handle_event(command("/help"), None, None, None, StubReader())
        send = next(a for a in actions if isinstance(a, SendMessage))
        assert "/lockall" in send.text


class TestLockAllEndToEnd:
    def test_lockall_locks_clean_skips_flagged(self, db_session: Session) -> None:
        seed_config_if_missing(db_session, get_settings())
        _summary(db_session, "2026-06-20", 5)  # clean → lock
        _summary(db_session, "2026-06-21", 20)  # anomalous → skip
        db_session.commit()

        notifier = FakeNotifier()
        with time_machine.travel(datetime(2026, 6, 25, 2, 0, tzinfo=UTC), tick=False):
            process_update(
                db_session, msg_update("/lockall", command=True), notifier, bot_settings()
            )

        db_session.expire_all()
        clean = db_session.execute(
            select(DailySummary).where(DailySummary.local_date == "2026-06-20")
        ).scalar_one()
        flagged = db_session.execute(
            select(DailySummary).where(DailySummary.local_date == "2026-06-21")
        ).scalar_one()
        assert bool(clean.locked) is True
        assert bool(flagged.locked) is False
        assert "Locked 1 clean day" in notifier.sent[-1].text

    def test_lockall_empty_backlog_message(self, db_session: Session) -> None:
        seed_config_if_missing(db_session, get_settings())
        db_session.commit()
        notifier = FakeNotifier()
        with time_machine.travel(datetime(2026, 6, 25, 2, 0, tzinfo=UTC), tick=False):
            process_update(
                db_session, msg_update("/lockall", command=True), notifier, bot_settings()
            )
        assert "Nothing to lock" in notifier.sent[-1].text
