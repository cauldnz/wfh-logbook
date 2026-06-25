"""Bulk 'lock clean days' (HANDOFF §6 Phase 10.B).

``lock_clean_days`` locks only days the review queue considers clean (sole
reason ``unlocked_backlog`` and > 0 claimed hours). Anomalous, flagged, and
0-hour days are deliberately left for manual review.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
import time_machine
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.days_service import _latest_summary, lock_clean_days
from app.config import get_settings
from app.main import seed_config_if_missing
from app.models import DailySummary

TODAY = date(2026, 6, 10)


def utc(y: int, m: int, d: int, hh: int = 0, mm: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=UTC)


@pytest.fixture
def seeded(db_session: Session) -> Session:
    """db_session with the config row seeded (build_review_queue needs it)."""
    seed_config_if_missing(db_session, get_settings())
    db_session.commit()
    return db_session


def _summary(db: Session, local_date: str, hours: float, *, locked: bool = False) -> None:
    secs = int(hours * 3600)
    db.add(
        DailySummary(
            local_date=local_date,
            version=1,
            computed_seconds=secs,
            adjustment_seconds=0,
            adjustment_reason=None,
            claimed_seconds=secs,
            locked=locked,
            locked_at=utc(2026, 6, 9) if locked else None,
            created_at=utc(2026, 6, 9),
            created_by="sessioniser",
            rule_version="2026.1",
        )
    )


def _locked(db: Session, d: date) -> bool:
    row = _latest_summary(db, d)
    assert row is not None
    return bool(row.locked)


class TestLockCleanDays:
    def test_locks_only_clean_positive_days(self, seeded: Session) -> None:
        _summary(seeded, "2026-06-08", 6)  # clean → LOCK
        _summary(seeded, "2026-06-07", 20)  # anomalous (> cap) → skip
        _summary(seeded, "2026-06-06", 0)  # 0h → skip
        _summary(seeded, "2026-06-05", 4, locked=True)  # already locked → not in queue
        seeded.commit()

        result = lock_clean_days(seeded, TODAY)

        assert result.locked_dates == [date(2026, 6, 8)]
        assert result.skipped_count == 2  # the anomalous day + the 0h day
        assert _locked(seeded, date(2026, 6, 8)) is True
        assert _locked(seeded, date(2026, 6, 7)) is False
        assert _locked(seeded, date(2026, 6, 6)) is False

    def test_noop_when_nothing_clean(self, seeded: Session) -> None:
        _summary(seeded, "2026-06-07", 20)  # anomalous only
        seeded.commit()
        result = lock_clean_days(seeded, TODAY)
        assert result.locked_dates == []
        assert result.skipped_count == 1

    def test_noop_on_empty_history(self, seeded: Session) -> None:
        result = lock_clean_days(seeded, TODAY)
        assert result.locked_dates == []
        assert result.skipped_count == 0

    def test_multiple_clean_days_locked_in_date_order(self, seeded: Session) -> None:
        _summary(seeded, "2026-06-09", 3)
        _summary(seeded, "2026-06-08", 7)
        seeded.commit()
        result = lock_clean_days(seeded, TODAY)
        assert result.locked_dates == [date(2026, 6, 8), date(2026, 6, 9)]
        assert result.skipped_count == 0


class TestLockCleanEndpoint:
    def test_endpoint_locks_clean_day(self, client: TestClient, db_session: Session) -> None:
        # Config is seeded by the app lifespan; insert a clean day to lock.
        _summary(db_session, "2026-06-08", 6)
        _summary(db_session, "2026-06-07", 20)  # anomalous → skipped
        db_session.commit()

        # Freeze "today" so the fixtures stay inside the 90-day scan window
        # regardless of when the suite runs (2026-06-10 12:00 Sydney).
        with time_machine.travel(datetime(2026, 6, 10, 2, 0, tzinfo=UTC), tick=False):
            resp = client.post("/api/days/lock-clean")
        assert resp.status_code == 200
        body = resp.json()
        assert body["locked_count"] == 1
        assert body["locked_dates"] == ["2026-06-08"]
        assert body["skipped_count"] == 1
