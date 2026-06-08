"""Versioning rules per ARCHITECTURE §5.5 and HANDOFF §6 Phase 4 acceptance.

Adjustments and post-lock edits MUST create new ``daily_summaries`` rows —
nothing is ever overwritten in place. Computed_seconds is preserved across
versions; history is retrievable.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy.orm import Session

from app.api.days_service import AdjustParams, adjust_day, lock_day
from app.models import DailySummary, Observation
from app.sessions.persistence import sessionise_date
from app.sessions.rules import RuleSet


def utc(y: int, m: int, d: int, hh: int = 0, mm: int = 0, ss: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, ss, tzinfo=UTC)


@pytest.fixture
def rules(db_session: Session) -> RuleSet:
    from app.config import get_settings
    from app.main import seed_config_if_missing

    seed_config_if_missing(db_session, get_settings())
    db_session.commit()
    return RuleSet.from_db(db_session)


@pytest.fixture
def seeded_day(db_session: Session, rules: RuleSet) -> date:
    """A day with a v1 daily_summary from the sessioniser. 3 hours computed."""
    target = date(2026, 5, 20)
    for ts, conn in [
        (utc(2026, 5, 20, 9, 0), True),
        (utc(2026, 5, 20, 12, 0), False),
    ]:
        db_session.add(
            Observation(
                observed_at=ts,
                controller_seen_at=ts,
                mac="a",
                device_label="iPhone",
                ssid="WFH-TEST",
                is_connected=conn,
                signal_dbm=None,
                raw_json="{}",
            )
        )
    db_session.commit()
    sessionise_date(db_session, target, rules)
    db_session.commit()
    return target


class TestAdjustmentOnUnlocked:
    """Adjustment on an unlocked day creates v+1 (ARCH §5.5)."""

    def test_adjustment_creates_new_version(self, db_session: Session, seeded_day: date) -> None:
        new_row = adjust_day(
            db_session,
            seeded_day,
            AdjustParams(adjustment_seconds=-45 * 60, reason="lunch 12:30-13:15"),
        )
        db_session.commit()
        assert new_row.version == 2
        assert new_row.computed_seconds == 3 * 3600
        assert new_row.adjustment_seconds == -45 * 60
        assert new_row.claimed_seconds == 3 * 3600 - 45 * 60
        assert bool(new_row.locked) is False
        assert new_row.created_by == "web"

    def test_v1_is_not_mutated(self, db_session: Session, seeded_day: date) -> None:
        v1_before = (
            db_session.query(DailySummary)
            .filter(DailySummary.local_date == seeded_day.isoformat())
            .filter(DailySummary.version == 1)
            .one()
        )
        v1_snapshot = (
            v1_before.computed_seconds,
            v1_before.adjustment_seconds,
            v1_before.claimed_seconds,
            bool(v1_before.locked),
        )
        adjust_day(
            db_session,
            seeded_day,
            AdjustParams(adjustment_seconds=-30 * 60, reason="break"),
        )
        db_session.commit()
        v1_after = (
            db_session.query(DailySummary)
            .filter(DailySummary.local_date == seeded_day.isoformat())
            .filter(DailySummary.version == 1)
            .one()
        )
        assert (
            v1_after.computed_seconds,
            v1_after.adjustment_seconds,
            v1_after.claimed_seconds,
            bool(v1_after.locked),
        ) == v1_snapshot


class TestAdjustmentAfterLock:
    """Adjustment after a lock creates a new unlocked version (ARCH §5.5)."""

    def test_adjustment_after_lock_creates_unlocked_v_plus_one(
        self, db_session: Session, seeded_day: date
    ) -> None:
        lock_day(db_session, seeded_day)
        db_session.commit()
        # Confirm v1 is locked.
        v1 = (
            db_session.query(DailySummary)
            .filter(DailySummary.local_date == seeded_day.isoformat())
            .filter(DailySummary.version == 1)
            .one()
        )
        assert bool(v1.locked) is True

        v2 = adjust_day(
            db_session,
            seeded_day,
            AdjustParams(adjustment_seconds=-15 * 60, reason="follow-up correction"),
        )
        db_session.commit()
        assert v2.version == 2
        assert bool(v2.locked) is False
        # v1 still locked.
        v1_after = (
            db_session.query(DailySummary)
            .filter(DailySummary.local_date == seeded_day.isoformat())
            .filter(DailySummary.version == 1)
            .one()
        )
        assert bool(v1_after.locked) is True


class TestComputedSecondsPreserved:
    """computed_seconds is the same across adjustment versions (only adj changes).

    A sessioniser re-run with different observations CAN bump computed_seconds
    in a new version — that's tested in test_sessionisation.
    """

    def test_computed_seconds_carried_across_adjustments(
        self, db_session: Session, seeded_day: date
    ) -> None:
        v2 = adjust_day(
            db_session,
            seeded_day,
            AdjustParams(adjustment_seconds=-45 * 60, reason="lunch"),
        )
        db_session.commit()
        v3 = adjust_day(
            db_session,
            seeded_day,
            AdjustParams(adjustment_seconds=-60 * 60, reason="longer lunch"),
        )
        db_session.commit()
        assert v2.computed_seconds == v3.computed_seconds == 3 * 3600
        # The latest adjustment REPLACES the prior (not additive).
        assert v3.adjustment_seconds == -60 * 60
        assert v3.claimed_seconds == 3 * 3600 - 60 * 60


class TestHistoryRetrievable:
    """The full version history is preserved and readable."""

    def test_three_adjustments_produce_three_versions_plus_v1(
        self, db_session: Session, seeded_day: date
    ) -> None:
        for adj in (-30, -45, +20):
            adjust_day(
                db_session,
                seeded_day,
                AdjustParams(adjustment_seconds=adj * 60, reason=f"adj {adj}"),
            )
            db_session.commit()
        rows = (
            db_session.query(DailySummary)
            .filter(DailySummary.local_date == seeded_day.isoformat())
            .order_by(DailySummary.version)
            .all()
        )
        assert [r.version for r in rows] == [1, 2, 3, 4]
        # adjustment_reason recorded on each row.
        assert [r.adjustment_reason for r in rows[1:]] == ["adj -30", "adj -45", "adj 20"]


class TestLockIdempotent:
    def test_locking_already_locked_is_noop(self, db_session: Session, seeded_day: date) -> None:
        a = lock_day(db_session, seeded_day)
        db_session.commit()
        first_locked_at = a.locked_at
        b = lock_day(db_session, seeded_day)
        db_session.commit()
        # Same row, same lock timestamp.
        assert a.id == b.id
        assert b.locked_at == first_locked_at


class TestAdjustReasonRequired:
    def test_empty_reason_rejected(self, db_session: Session, seeded_day: date) -> None:
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            adjust_day(
                db_session,
                seeded_day,
                AdjustParams(adjustment_seconds=-30 * 60, reason=""),
            )
        assert exc.value.status_code == 400

    def test_no_summary_yet_returns_404(self, db_session: Session, rules: RuleSet) -> None:
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            adjust_day(
                db_session,
                date(2099, 1, 1),
                AdjustParams(adjustment_seconds=-30 * 60, reason="too early"),
            )
        assert exc.value.status_code == 404
