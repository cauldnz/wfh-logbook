"""Phase 9 usability backlog (HANDOFF §6 Phase 9).

9.A build-day flow for summary-less days; 9.B calendar weekday headers;
9.C /rebuild bot command.
"""

from __future__ import annotations

from datetime import date, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DailySummary
from app.notifier.base import ApplyRebuild, SendMessage
from app.notifier.conversation import handle_event
from tests.test_telegram_conversation import StubReader, command
from tests.test_telegram_service import FakeNotifier, bot_settings, msg_update

TODAY = date(2026, 6, 11)
YESTERDAY = date(2026, 6, 10)


# ------------------------------------------------- 9.A: build-day in the UI


class TestBuildDayFlow:
    """A summary-less day must be reviewable end-to-end from the UI."""

    def test_no_summary_day_offers_build_button(self, client: TestClient) -> None:
        resp = client.get("/day/2026-03-02")
        assert resp.status_code == 200
        assert "Build day" in resp.text
        # No adjust form yet (no summary to adjust).
        assert "Apply adjustment" not in resp.text

    def test_build_then_adjust_then_lock_empty_day(
        self, client: TestClient, db_session: Session
    ) -> None:
        """The METHODOLOGY §4.6 outage case: zero observations, manual hours."""
        target = "2026-03-02"

        # Build: creates v1 with computed 0:00 and returns the card with form.
        resp = client.post(f"/web/days/{target}/resessionise")
        assert resp.status_code == 200
        assert "Apply adjustment" in resp.text
        assert "Build day" not in resp.text

        v1 = db_session.execute(
            select(DailySummary).where(DailySummary.local_date == target)
        ).scalar_one()
        assert v1.computed_seconds == 0

        # Adjust: +120 min with an outage reason.
        resp = client.post(
            f"/web/days/{target}/adjust",
            data={"minutes": "120", "reason": "poller outage; corroborated by Teams"},
        )
        assert resp.status_code == 200

        # Lock.
        resp = client.post(f"/web/days/{target}/lock")
        assert resp.status_code == 200
        db_session.expire_all()
        latest = db_session.execute(
            select(DailySummary)
            .where(DailySummary.local_date == target)
            .order_by(DailySummary.version.desc())
            .limit(1)
        ).scalar_one()
        assert latest.claimed_seconds == 120 * 60
        assert bool(latest.locked) is True


# ---------------------------------------------- 9.B: calendar weekday headers


class TestCalendarHeaders:
    def test_calendar_has_monday_first_weekday_headers(self, client: TestClient) -> None:
        resp = client.get("/calendar")
        assert resp.status_code == 200
        text = resp.text
        for dow in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"):
            assert f'<div class="dow-header">{dow}</div>' in text
        # Monday-first ordering.
        assert text.find('"dow-header">Mon') < text.find('"dow-header">Sun')


# --------------------------------------------------- 9.C: /rebuild command


class TestRebuildCommand:
    def test_rebuild_with_date(self) -> None:
        actions = handle_event(
            command("/rebuild", args="2026-06-09"), None, None, None, StubReader()
        )
        assert ApplyRebuild(target_date=date(2026, 6, 9)) in actions

    def test_rebuild_defaults_to_yesterday(self) -> None:
        actions = handle_event(command("/rebuild"), None, None, None, StubReader())
        assert ApplyRebuild(target_date=TODAY - timedelta(days=1)) in actions

    def test_rebuild_today_keyword(self) -> None:
        actions = handle_event(command("/rebuild", args="today"), None, None, None, StubReader())
        assert ApplyRebuild(target_date=TODAY) in actions

    def test_rebuild_bad_date_usage_hint(self) -> None:
        actions = handle_event(command("/rebuild", args="garbage"), None, None, None, StubReader())
        assert not any(isinstance(a, ApplyRebuild) for a in actions)
        send = next(a for a in actions if isinstance(a, SendMessage))
        assert "Usage" in send.text

    def test_rebuild_in_help(self) -> None:
        actions = handle_event(command("/help"), None, None, None, StubReader())
        send = next(a for a in actions if isinstance(a, SendMessage))
        assert "/rebuild" in send.text


class TestRebuildEndToEnd:
    def test_rebuild_builds_empty_day_and_replies_with_buttons(self, db_session: Session) -> None:
        from app.config import get_settings
        from app.main import seed_config_if_missing
        from app.notifier.service import process_update

        seed_config_if_missing(db_session, get_settings())
        db_session.commit()

        notifier = FakeNotifier()
        process_update(
            db_session,
            msg_update("/rebuild 2026-03-02", command=True),
            notifier,
            bot_settings(),
        )
        # Summary v1 created for the empty day.
        row = db_session.execute(
            select(DailySummary).where(DailySummary.local_date == "2026-03-02")
        ).scalar_one()
        assert row.computed_seconds == 0
        assert row.created_by == "sessioniser"
        # Reply shows the day with review buttons.
        sent = notifier.sent[-1]
        assert "rebuilt" in sent.text
        labels = [b.text for r in sent.buttons for b in r]
        assert "✏ Adjust" in labels

    def test_rebuild_is_idempotent_via_same_path_as_web(self, db_session: Session) -> None:
        from app.config import get_settings
        from app.main import seed_config_if_missing
        from app.notifier.service import process_update

        seed_config_if_missing(db_session, get_settings())
        db_session.commit()

        notifier = FakeNotifier()
        process_update(
            db_session,
            msg_update("/rebuild 2026-03-03", command=True),
            notifier,
            bot_settings(),
        )
        process_update(
            db_session,
            msg_update("/rebuild 2026-03-03", command=True),
            notifier,
            bot_settings(),
        )
        rows = (
            db_session.execute(select(DailySummary).where(DailySummary.local_date == "2026-03-03"))
            .scalars()
            .all()
        )
        assert len(rows) == 1  # unchanged computed → no new version
