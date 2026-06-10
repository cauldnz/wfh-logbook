"""Review queue + data-quality flags (HANDOFF §6 Phase 8.A)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.review_queue import build_review_queue, find_observation_gaps
from app.models import DailySummary, Observation
from app.sessions.persistence import sessionise_date
from app.sessions.rules import RuleSet

TODAY = date(2026, 6, 10)


def utc(y: int, m: int, d: int, hh: int = 0, mm: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=UTC)


@pytest.fixture
def rules(db_session: Session) -> RuleSet:
    from app.config import get_settings
    from app.main import seed_config_if_missing

    seed_config_if_missing(db_session, get_settings())
    db_session.commit()
    return RuleSet.from_db(db_session)


def _obs(db: Session, ts: datetime, connected: bool = True) -> None:
    db.add(
        Observation(
            observed_at=ts,
            controller_seen_at=ts,
            mac="a",
            device_label="iPhone",
            ssid="WFH-TEST",
            is_connected=connected,
            signal_dbm=None,
            raw_json="{}",
        )
    )


def _polled_day(
    db: Session,
    day: tuple[int, int, int],
    start_hh: int,
    end_hh: int,
    hole: tuple[int, int] | None = None,
) -> None:
    """Simulate per-minute polling from start to end, optionally silent
    between hole=(from_hh, to_hh) with the device still connected."""
    cur = utc(*day, start_hh, 0)
    end = utc(*day, end_hh, 0)
    from datetime import timedelta

    while cur <= end:
        in_hole = hole is not None and utc(*day, hole[0], 0) < cur < utc(*day, hole[1], 0)
        if not in_hole:
            _obs(db, cur, connected=True)
        cur += timedelta(minutes=1)
    _obs(db, end, connected=False)
    db.commit()


class TestGapDetection:
    def test_silent_hole_inside_session_flagged(self, db_session: Session, rules: RuleSet) -> None:
        """Poller silent 10:00-11:30 while connected → data gap."""
        _polled_day(db_session, (2026, 6, 8), 9, 12, hole=(10, 11))
        # The hole is ~60 min; rebuild sessions (one continuous session 9-12).
        sessionise_date(db_session, date(2026, 6, 8), rules)
        db_session.commit()

        gaps = find_observation_gaps(db_session, date(2026, 6, 8), rules.gap_bridge_minutes)
        assert len(gaps) == 1
        assert gaps[0].seconds >= 55 * 60  # ~an hour of silence

    def test_bridged_absence_not_flagged(self, db_session: Session, rules: RuleSet) -> None:
        """Disconnect → 5 min absent → reconnect: bridging, NOT an outage."""
        from datetime import timedelta

        cur = utc(2026, 6, 8, 9, 0)
        while cur <= utc(2026, 6, 8, 9, 30):
            _obs(db_session, cur, connected=True)
            cur += timedelta(minutes=1)
        _obs(db_session, utc(2026, 6, 8, 9, 30), connected=False)  # genuine leave
        cur = utc(2026, 6, 8, 9, 35)  # back 5 min later (≤ bridge threshold)
        while cur <= utc(2026, 6, 8, 12, 0):
            _obs(db_session, cur, connected=True)
            cur += timedelta(minutes=1)
        _obs(db_session, utc(2026, 6, 8, 12, 0), connected=False)
        db_session.commit()
        sessionise_date(db_session, date(2026, 6, 8), rules)
        db_session.commit()

        gaps = find_observation_gaps(db_session, date(2026, 6, 8), rules.gap_bridge_minutes)
        assert gaps == []

    def test_continuous_polling_no_gaps(self, db_session: Session, rules: RuleSet) -> None:
        _polled_day(db_session, (2026, 6, 8), 9, 12)
        sessionise_date(db_session, date(2026, 6, 8), rules)
        db_session.commit()
        gaps = find_observation_gaps(db_session, date(2026, 6, 8), rules.gap_bridge_minutes)
        assert gaps == []

    def test_no_sessions_no_gaps(self, db_session: Session, rules: RuleSet) -> None:
        assert find_observation_gaps(db_session, date(2026, 6, 8), 10) == []


class TestQueueCategories:
    def test_gap_day_in_queue(self, db_session: Session, rules: RuleSet) -> None:
        _polled_day(db_session, (2026, 6, 8), 9, 12, hole=(10, 11))
        sessionise_date(db_session, date(2026, 6, 8), rules)
        db_session.commit()
        queue = build_review_queue(db_session, TODAY)
        item = next(i for i in queue.items if i.local_date == date(2026, 6, 8))
        assert "data_gap" in item.reasons
        assert "unlocked_backlog" in item.reasons
        assert item.gaps

    def test_anomalous_day_in_queue(self, db_session: Session, rules: RuleSet) -> None:
        db_session.add(
            DailySummary(
                local_date="2026-06-07",
                version=1,
                computed_seconds=13 * 3600,  # > 12h cap
                adjustment_seconds=0,
                adjustment_reason=None,
                claimed_seconds=13 * 3600,
                locked=False,
                locked_at=None,
                created_at=utc(2026, 6, 8),
                created_by="sessioniser",
                rule_version="2026.1",
            )
        )
        db_session.commit()
        queue = build_review_queue(db_session, TODAY)
        item = next(i for i in queue.items if i.local_date == date(2026, 6, 7))
        assert "anomalous" in item.reasons

    def test_clean_locked_day_not_in_queue(self, db_session: Session, rules: RuleSet) -> None:
        db_session.add(
            DailySummary(
                local_date="2026-06-06",
                version=1,
                computed_seconds=4 * 3600,
                adjustment_seconds=0,
                adjustment_reason=None,
                claimed_seconds=4 * 3600,
                locked=True,
                locked_at=utc(2026, 6, 7),
                created_at=utc(2026, 6, 7),
                created_by="sessioniser",
                rule_version="2026.1",
            )
        )
        db_session.commit()
        queue = build_review_queue(db_session, TODAY)
        assert not any(i.local_date == date(2026, 6, 6) for i in queue.items)

    def test_old_unlocked_day_outside_window_still_listed(
        self, db_session: Session, rules: RuleSet
    ) -> None:
        """Backlog has no statute of limitations."""
        db_session.add(
            DailySummary(
                local_date="2026-01-05",  # > 90 days before TODAY
                version=1,
                computed_seconds=2 * 3600,
                adjustment_seconds=0,
                adjustment_reason=None,
                claimed_seconds=2 * 3600,
                locked=False,
                locked_at=None,
                created_at=utc(2026, 1, 6),
                created_by="sessioniser",
                rule_version="2026.1",
            )
        )
        db_session.commit()
        queue = build_review_queue(db_session, TODAY)
        item = next(i for i in queue.items if i.local_date == date(2026, 1, 5))
        assert item.reasons == ["unlocked_backlog"]

    def test_heavy_bridging_flagged(self, db_session: Session, rules: RuleSet) -> None:
        from app.models import WorkSession

        db_session.add(
            DailySummary(
                local_date="2026-06-05",
                version=1,
                computed_seconds=4 * 3600,
                adjustment_seconds=0,
                adjustment_reason=None,
                claimed_seconds=4 * 3600,
                locked=False,
                locked_at=None,
                created_at=utc(2026, 6, 6),
                created_by="sessioniser",
                rule_version="2026.1",
            )
        )
        db_session.add(
            WorkSession(
                local_date="2026-06-05",
                started_at=utc(2026, 6, 5, 9, 0),
                ended_at=utc(2026, 6, 5, 13, 0),
                duration_seconds=4 * 3600,
                devices_seen="iPhone",
                bridged_gaps_count=5,  # ≥ HEAVY_BRIDGE_COUNT
                bridged_gaps_seconds=10 * 60,
                created_at=utc(2026, 6, 6),
                rule_version="2026.1",
            )
        )
        db_session.commit()
        queue = build_review_queue(db_session, TODAY)
        item = next(i for i in queue.items if i.local_date == date(2026, 6, 5))
        assert "heavy_bridging" in item.reasons


class TestEndpoints:
    def test_api_review_queue(self, client: TestClient) -> None:
        resp = client.get("/api/review-queue")
        assert resp.status_code == 200
        body = resp.json()
        assert "items" in body
        assert "today" in body

    def test_web_review_queue_renders(self, client: TestClient) -> None:
        resp = client.get("/review-queue")
        assert resp.status_code == 200
        assert "Review queue" in resp.text
