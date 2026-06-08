"""Sessionisation tests — the audit-defence core.

Per CLAUDE.md "Sessionisation is the audit-defence core. Effectively 100%
branch coverage required in app/sessions/. Every rule in METHODOLOGY.md §4
has at least one corresponding test that names the rule in its docstring."

The pure ``build_sessions_for_date`` is tested in isolation. The persistence
layer is tested separately to keep test diagnostics narrow.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from app.sessions.builder import (
    ObservationRecord,
    build_sessions_for_date,
    computed_seconds_total,
    utc_buffer_for,
)
from app.sessions.rules import RuleSet

# ---------------------------------------------------- shared rule/test helpers

SYDNEY = "Australia/Sydney"


def default_rules(
    *,
    gap_bridge_minutes: int = 10,
    min_session_minutes: int = 2,
    daily_cap_hours: int = 12,
    rule_version: str = "2026.1",
    timezone: str = SYDNEY,
) -> RuleSet:
    return RuleSet(
        gap_bridge_minutes=gap_bridge_minutes,
        min_session_minutes=min_session_minutes,
        daily_cap_hours=daily_cap_hours,
        local_timezone=timezone,
        rule_version=rule_version,
    )


def utc(y: int, m: int, d: int, hh: int = 0, mm: int = 0, ss: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, ss, tzinfo=UTC)


def obs(
    mac: str,
    timestamp: datetime,
    *,
    connected: bool = True,
    label: str | None = None,
) -> ObservationRecord:
    return ObservationRecord(
        mac=mac,
        device_label=label or {"a": "iPhone", "b": "Laptop"}.get(mac, mac),
        timestamp=timestamp,
        is_connected=connected,
    )


# =============================================================================
# Builder tests — pure function
# =============================================================================


class TestEmptyDay:
    """METHODOLOGY §4.1: a day with no observations produces no sessions."""

    def test_no_observations_no_sessions(self) -> None:
        result = build_sessions_for_date(
            target_date=date(2026, 5, 20),
            observations=[],
            rules=default_rules(),
        )
        assert result == []

    def test_only_disconnect_observations_produce_nothing(self) -> None:
        observations = [
            obs("a", utc(2026, 5, 20, 9, 0), connected=False),
        ]
        result = build_sessions_for_date(date(2026, 5, 20), observations, default_rules())
        assert result == []


class TestMinSessionFilter:
    """METHODOLOGY §4.3: drop sessions shorter than min_session_minutes."""

    def test_one_minute_session_dropped_when_min_is_two(self) -> None:
        # 09:00:00 → 09:01:00 = 60s < 2 min
        observations = [
            obs("a", utc(2026, 5, 20, 9, 0, 0), connected=True),
            obs("a", utc(2026, 5, 20, 9, 1, 0), connected=False),
        ]
        result = build_sessions_for_date(
            date(2026, 5, 20),
            observations,
            default_rules(min_session_minutes=2),
        )
        assert result == []

    def test_two_minute_session_kept_at_threshold(self) -> None:
        observations = [
            obs("a", utc(2026, 5, 20, 9, 0, 0), connected=True),
            obs("a", utc(2026, 5, 20, 9, 2, 0), connected=False),
        ]
        result = build_sessions_for_date(
            date(2026, 5, 20),
            observations,
            default_rules(min_session_minutes=2),
        )
        assert len(result) == 1
        assert result[0].duration_seconds == 120


class TestGapBridging:
    """METHODOLOGY §4.2: gaps ≤ gap_bridge_minutes merge into one session."""

    def test_gap_below_threshold_merged(self) -> None:
        # Session A: 09:00-09:30, gap 8 min, Session B: 09:38-10:00.
        # Bridge threshold 10 min → merged into one [09:00, 10:00].
        observations = [
            obs("a", utc(2026, 5, 20, 9, 0), connected=True),
            obs("a", utc(2026, 5, 20, 9, 30), connected=False),
            obs("a", utc(2026, 5, 20, 9, 38), connected=True),
            obs("a", utc(2026, 5, 20, 10, 0), connected=False),
        ]
        result = build_sessions_for_date(
            date(2026, 5, 20),
            observations,
            default_rules(gap_bridge_minutes=10),
        )
        assert len(result) == 1
        s = result[0]
        assert s.started_at == utc(2026, 5, 20, 9, 0)
        assert s.ended_at == utc(2026, 5, 20, 10, 0)
        assert s.bridged_gaps_count == 1
        assert s.bridged_gaps_seconds == 8 * 60

    def test_gap_above_threshold_not_merged(self) -> None:
        # Session A: 09:00-09:30, gap 15 min, Session B: 09:45-10:00.
        # Bridge threshold 10 min → two separate sessions.
        observations = [
            obs("a", utc(2026, 5, 20, 9, 0), connected=True),
            obs("a", utc(2026, 5, 20, 9, 30), connected=False),
            obs("a", utc(2026, 5, 20, 9, 45), connected=True),
            obs("a", utc(2026, 5, 20, 10, 0), connected=False),
        ]
        result = build_sessions_for_date(
            date(2026, 5, 20),
            observations,
            default_rules(gap_bridge_minutes=10),
        )
        assert len(result) == 2
        assert result[0].started_at == utc(2026, 5, 20, 9, 0)
        assert result[0].ended_at == utc(2026, 5, 20, 9, 30)
        assert result[0].bridged_gaps_count == 0
        assert result[1].started_at == utc(2026, 5, 20, 9, 45)

    def test_gap_exactly_at_threshold_merged(self) -> None:
        # Threshold is inclusive (≤): a gap of exactly 10 min bridges.
        observations = [
            obs("a", utc(2026, 5, 20, 9, 0), connected=True),
            obs("a", utc(2026, 5, 20, 9, 30), connected=False),
            obs("a", utc(2026, 5, 20, 9, 40), connected=True),
            obs("a", utc(2026, 5, 20, 10, 0), connected=False),
        ]
        result = build_sessions_for_date(
            date(2026, 5, 20),
            observations,
            default_rules(gap_bridge_minutes=10),
        )
        assert len(result) == 1
        assert result[0].bridged_gaps_count == 1
        assert result[0].bridged_gaps_seconds == 600


class TestMultiDeviceUnion:
    """METHODOLOGY §4.1: 'If only one device disconnects the session continues.'"""

    def test_overlapping_devices_merged_into_one_session(self) -> None:
        # Laptop on 09:00-09:30, iPhone on 09:15-10:00 → union [09:00, 10:00].
        observations = [
            obs("a", utc(2026, 5, 20, 9, 0), connected=True, label="iPhone"),
            obs("a", utc(2026, 5, 20, 9, 30), connected=False, label="iPhone"),
            obs("b", utc(2026, 5, 20, 9, 15), connected=True, label="Laptop"),
            obs("b", utc(2026, 5, 20, 10, 0), connected=False, label="Laptop"),
        ]
        result = build_sessions_for_date(date(2026, 5, 20), observations, default_rules())
        assert len(result) == 1
        s = result[0]
        assert s.started_at == utc(2026, 5, 20, 9, 0)
        assert s.ended_at == utc(2026, 5, 20, 10, 0)
        assert s.devices_seen == ("Laptop", "iPhone")  # sorted

    def test_touching_devices_merged(self) -> None:
        # iPhone ends at 09:30, Laptop starts at 09:30 → continuous.
        observations = [
            obs("a", utc(2026, 5, 20, 9, 0), connected=True, label="iPhone"),
            obs("a", utc(2026, 5, 20, 9, 30), connected=False, label="iPhone"),
            obs("b", utc(2026, 5, 20, 9, 30), connected=True, label="Laptop"),
            obs("b", utc(2026, 5, 20, 10, 0), connected=False, label="Laptop"),
        ]
        result = build_sessions_for_date(date(2026, 5, 20), observations, default_rules())
        assert len(result) == 1
        assert result[0].duration_seconds == 3600


class TestMidnightCrossing:
    """METHODOLOGY §4.4: midnight-crossing sessions attribute to START date."""

    def test_session_starting_22_30_extending_to_01_30_attributes_to_start(self) -> None:
        # Sydney is UTC+10/+11. Use UTC times for the test then validate the
        # ATTRIBUTED date in Sydney local terms.
        # 2026-05-20 22:30 Sydney = 2026-05-20 12:30 UTC (Sydney standard time = +10).
        # 2026-05-21 01:30 Sydney = 2026-05-20 15:30 UTC.
        observations = [
            obs("a", utc(2026, 5, 20, 12, 30), connected=True),
            obs("a", utc(2026, 5, 20, 15, 30), connected=False),
        ]
        # Building for the start date returns the session.
        result_for_20th = build_sessions_for_date(
            date(2026, 5, 20),
            observations,
            default_rules(),
        )
        assert len(result_for_20th) == 1
        assert result_for_20th[0].local_date == date(2026, 5, 20)
        assert result_for_20th[0].duration_seconds == 3 * 3600

        # Building for the NEXT date returns nothing — session belongs to start date.
        result_for_21st = build_sessions_for_date(
            date(2026, 5, 21),
            observations,
            default_rules(),
        )
        assert result_for_21st == []


class TestDailyCap:
    """METHODOLOGY §4.5: long days are flagged, NOT truncated."""

    def test_15_hour_session_is_not_truncated(self) -> None:
        # 06:00 → 21:00 UTC = 15h. Daily cap 12h, but session preserved intact.
        observations = [
            obs("a", utc(2026, 5, 20, 6, 0), connected=True),
            obs("a", utc(2026, 5, 20, 21, 0), connected=False),
        ]
        result = build_sessions_for_date(
            date(2026, 5, 20),
            observations,
            default_rules(daily_cap_hours=12),
        )
        # The session date attribution may land on either day depending on
        # the local-time interpretation; we don't care here — just verify
        # the total duration is preserved across whichever day it lands on.
        total = computed_seconds_total(result)
        # If attribution lands on a different local date, the test target
        # date may not match — in that case re-run with attribution date.
        if not result:
            from zoneinfo import ZoneInfo

            attribution_date = utc(2026, 5, 20, 6, 0).astimezone(ZoneInfo(SYDNEY)).date()
            result = build_sessions_for_date(attribution_date, observations, default_rules())
            total = computed_seconds_total(result)
        assert total == 15 * 3600


class TestIdempotence:
    """HANDOFF §6 Phase 3 acceptance: running twice produces identical rows."""

    def test_running_twice_produces_identical_output(self) -> None:
        observations = [
            obs("a", utc(2026, 5, 20, 9, 0), connected=True),
            obs("a", utc(2026, 5, 20, 9, 30), connected=False),
            obs("a", utc(2026, 5, 20, 9, 38), connected=True),
            obs("a", utc(2026, 5, 20, 12, 0), connected=False),
            obs("b", utc(2026, 5, 20, 14, 0), connected=True),
            obs("b", utc(2026, 5, 20, 17, 30), connected=False),
        ]
        a = build_sessions_for_date(date(2026, 5, 20), observations, default_rules())
        b = build_sessions_for_date(date(2026, 5, 20), observations, default_rules())
        assert a == b
        # And the ordering is stable.
        assert [s.started_at for s in a] == sorted(s.started_at for s in a)


class TestObservationCoalescing:
    """ARCHITECTURE §5.2 step 1: caller coalesces controller_seen_at→observed_at.

    The builder treats ``ObservationRecord.timestamp`` as authoritative — the
    persistence layer is responsible for picking ``controller_seen_at`` over
    ``observed_at``. This test asserts the builder uses the timestamp it was
    given.
    """

    def test_builder_uses_record_timestamp(self) -> None:
        observations = [
            obs("a", utc(2026, 5, 20, 9, 0), connected=True),
            obs("a", utc(2026, 5, 20, 11, 0), connected=False),
        ]
        result = build_sessions_for_date(date(2026, 5, 20), observations, default_rules())
        assert result[0].duration_seconds == 2 * 3600


class TestBufferHelper:
    """``utc_buffer_for`` returns UTC bounds with a buffer either side."""

    def test_buffer_brackets_local_day(self) -> None:
        lower, upper = utc_buffer_for(date(2026, 5, 20), SYDNEY)
        # 2026-05-20 in Sydney spans approximately 14:00 UTC of the 19th to
        # 14:00 UTC of the 20th (Sydney is +10 in winter). The buffer adds
        # 12h either side.
        assert lower < utc(2026, 5, 19, 14, 0)
        assert upper > utc(2026, 5, 20, 14, 0)
        assert (upper - lower) == timedelta(days=1) + timedelta(hours=24)


class TestUnusualPatterns:
    """Corner cases that have bitten similar systems before."""

    def test_data_ends_mid_session_uses_last_true_as_close(self) -> None:
        # No closing disconnect — builder closes at the last true observation.
        observations = [
            obs("a", utc(2026, 5, 20, 9, 0), connected=True),
            obs("a", utc(2026, 5, 20, 9, 30), connected=True),
            obs("a", utc(2026, 5, 20, 10, 0), connected=True),
        ]
        result = build_sessions_for_date(date(2026, 5, 20), observations, default_rules())
        assert len(result) == 1
        assert result[0].duration_seconds == 3600

    def test_unsorted_observations_handled(self) -> None:
        # Caller should sort, but builder is robust to disorder.
        observations = [
            obs("a", utc(2026, 5, 20, 9, 30), connected=False),
            obs("a", utc(2026, 5, 20, 9, 0), connected=True),
        ]
        result = build_sessions_for_date(date(2026, 5, 20), observations, default_rules())
        assert len(result) == 1
        assert result[0].duration_seconds == 1800

    def test_multiple_separate_sessions_on_one_day(self) -> None:
        observations = [
            # Morning
            obs("a", utc(2026, 5, 20, 9, 0), connected=True),
            obs("a", utc(2026, 5, 20, 12, 0), connected=False),
            # Afternoon (gap of 60 min > bridge)
            obs("a", utc(2026, 5, 20, 13, 0), connected=True),
            obs("a", utc(2026, 5, 20, 17, 30), connected=False),
        ]
        result = build_sessions_for_date(date(2026, 5, 20), observations, default_rules())
        assert len(result) == 2
        assert result[0].duration_seconds == 3 * 3600
        assert result[1].duration_seconds == int(4.5 * 3600)


# =============================================================================
# Persistence tests — DB-touching
# =============================================================================


@pytest.fixture
def rules(db_session):  # type: ignore[no-untyped-def]
    """A RuleSet seeded from the test DB's Config row."""
    from app.config import get_settings
    from app.main import seed_config_if_missing

    seed_config_if_missing(db_session, get_settings())
    db_session.commit()
    return RuleSet.from_db(db_session)


class TestSessioniseDatePersistence:
    """The persistence wrapper writes sessions + daily_summaries correctly."""

    def test_first_run_creates_v1_summary(self, db_session, rules) -> None:  # type: ignore[no-untyped-def]
        from app.models import Observation
        from app.sessions.persistence import sessionise_date

        # Insert observations directly (Observation is INSERT-only, fine).
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
                    signal_dbm=-55,
                    raw_json="{}",
                )
            )
        db_session.commit()

        result = sessionise_date(db_session, date(2026, 5, 20), rules)
        db_session.commit()

        assert result.sessions_built == 1
        assert result.computed_seconds == 3 * 3600
        assert result.daily_summary_version == 1
        assert result.daily_summary_changed is True

    def test_second_run_with_same_inputs_is_idempotent(self, db_session, rules) -> None:  # type: ignore[no-untyped-def]
        from app.models import DailySummary, Observation, WorkSession
        from app.sessions.persistence import sessionise_date

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

        sessionise_date(db_session, date(2026, 5, 20), rules)
        db_session.commit()
        first_summaries = db_session.query(DailySummary).count()
        first_sessions = db_session.query(WorkSession).count()

        result_2 = sessionise_date(db_session, date(2026, 5, 20), rules)
        db_session.commit()

        assert result_2.daily_summary_changed is False
        assert db_session.query(DailySummary).count() == first_summaries
        # Sessions are replaced atomically; count is the same.
        assert db_session.query(WorkSession).count() == first_sessions

    def test_recompute_with_changed_observations_creates_new_version(
        self,
        db_session,
        rules,  # type: ignore[no-untyped-def]
    ) -> None:
        from app.models import DailySummary, Observation
        from app.sessions.persistence import sessionise_date

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
        sessionise_date(db_session, date(2026, 5, 20), rules)
        db_session.commit()

        # Backfill observations extending the session.
        for ts, conn in [
            (utc(2026, 5, 20, 13, 0), True),
            (utc(2026, 5, 20, 17, 0), False),
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

        result_2 = sessionise_date(db_session, date(2026, 5, 20), rules)
        db_session.commit()

        assert result_2.daily_summary_changed is True
        assert result_2.daily_summary_version == 2
        # Both versions are still present (no overwrites — CLAUDE.md).
        rows = (
            db_session.query(DailySummary)
            .filter(DailySummary.local_date == "2026-05-20")
            .order_by(DailySummary.version)
            .all()
        )
        assert [r.version for r in rows] == [1, 2]
        assert rows[0].computed_seconds == 3 * 3600
        assert rows[1].computed_seconds == 3 * 3600 + 4 * 3600
        # Both unlocked at this point (SQLite stores bool as 0/1).
        assert all(not bool(r.locked) for r in rows)

    def test_post_lock_recompute_creates_new_unlocked_version(
        self,
        db_session,
        rules,  # type: ignore[no-untyped-def]
    ) -> None:
        """Recompute after a lock creates a new unlocked v+1 (ARCH §5.5)."""
        from app.models import DailySummary, Observation
        from app.sessions.persistence import sessionise_date

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
        sessionise_date(db_session, date(2026, 5, 20), rules)
        # Lock v1.
        v1 = db_session.query(DailySummary).filter(DailySummary.local_date == "2026-05-20").first()
        assert v1 is not None
        v1.locked = True
        v1.locked_at = datetime.now(UTC)
        db_session.commit()

        # Add more observations and re-run.
        for ts, conn in [
            (utc(2026, 5, 20, 13, 0), True),
            (utc(2026, 5, 20, 14, 0), False),
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
        sessionise_date(db_session, date(2026, 5, 20), rules)
        db_session.commit()

        rows = (
            db_session.query(DailySummary)
            .filter(DailySummary.local_date == "2026-05-20")
            .order_by(DailySummary.version)
            .all()
        )
        assert [r.version for r in rows] == [1, 2]
        # SQLite returns 0/1; compare truthy.
        assert bool(rows[0].locked) is True
        # New version starts unlocked, even though previous was locked.
        assert bool(rows[1].locked) is False
