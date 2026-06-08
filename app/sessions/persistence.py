"""DB-touching wrapper around the pure builder.

Responsibilities:

- Load observations for a target local date with buffer (ARCHITECTURE §5.2 step 1).
- Call ``build_sessions_for_date`` (pure).
- In a single transaction:
   - Delete existing ``sessions`` rows for ``target_date`` and insert the new set.
   - Create a new unlocked ``daily_summaries`` version if the new
     ``computed_seconds`` differs from the latest version's, preserving any
     adjustment. Idempotent if computed values match.
- Update ``poller_state.last_sessioniser_run_at``.

Per CLAUDE.md, ``daily_summaries`` rows are NEVER overwritten — sessioniser
re-runs that change ``computed_seconds`` create a new version (which itself
starts unlocked, per ARCHITECTURE §5.5).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import DailySummary, Observation, PollerState, WorkSession
from app.sessions.builder import (
    ComputedSession,
    ObservationRecord,
    build_sessions_for_date,
    computed_seconds_total,
    utc_buffer_for,
)
from app.sessions.rules import RuleSet

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SessioniseResult:
    """Summary of one sessionisation run for a date."""

    target_date: date
    sessions_built: int
    computed_seconds: int
    daily_summary_version: int
    daily_summary_changed: bool


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _ensure_utc(dt: datetime) -> datetime:
    """SQLite drops timezone info on round-trip; re-attach UTC if naive.

    We only ever store tz-aware UTC datetimes (CLAUDE.md: never store naive).
    Any naive datetime read back from SQLite was originally UTC.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def _load_observations(db: Session, target_date: date, rules: RuleSet) -> list[ObservationRecord]:
    lower, upper = utc_buffer_for(target_date, rules.local_timezone)
    # Filter in Python (using coalesce(controller_seen_at, observed_at) per
    # ARCHITECTURE §5.2 step 1) so we don't depend on a particular SQL dialect.
    stmt = select(Observation).order_by(Observation.observed_at.asc(), Observation.id.asc())
    out: list[ObservationRecord] = []
    for row in db.execute(stmt).scalars():
        seen = row.controller_seen_at if row.controller_seen_at is not None else row.observed_at
        effective = _ensure_utc(seen)
        if effective < lower or effective > upper:
            continue
        out.append(
            ObservationRecord(
                mac=row.mac,
                device_label=row.device_label,
                timestamp=effective,
                is_connected=bool(row.is_connected),
            )
        )
    return out


def _latest_summary(db: Session, target_date: date) -> DailySummary | None:
    stmt = (
        select(DailySummary)
        .where(DailySummary.local_date == target_date.isoformat())
        .order_by(DailySummary.version.desc())
        .limit(1)
    )
    return db.execute(stmt).scalar_one_or_none()


def _replace_sessions(
    db: Session,
    target_date: date,
    sessions: list[ComputedSession],
    rules: RuleSet,
) -> None:
    db.execute(delete(WorkSession).where(WorkSession.local_date == target_date.isoformat()))
    now = _utcnow()
    for s in sessions:
        db.add(
            WorkSession(
                local_date=target_date.isoformat(),
                started_at=s.started_at,
                ended_at=s.ended_at,
                duration_seconds=s.duration_seconds,
                devices_seen=",".join(s.devices_seen),
                bridged_gaps_count=s.bridged_gaps_count,
                bridged_gaps_seconds=s.bridged_gaps_seconds,
                created_at=now,
                rule_version=rules.rule_version,
            )
        )


def _ensure_summary_version(
    db: Session,
    target_date: date,
    new_computed_seconds: int,
    rules: RuleSet,
) -> tuple[int, bool]:
    """Insert a new version if computed_seconds differs, else no-op.

    Returns ``(version, changed)``. Versioning rules (ARCHITECTURE §5.5):

    - No prior version → insert v1 with adjustment=0, claimed=computed.
    - Prior latest with same computed_seconds → idempotent; return (version, False).
    - Prior latest with different computed_seconds → insert version+1,
      carrying adjustment/reason forward, ``locked=0`` even if previous locked.
    """
    latest = _latest_summary(db, target_date)
    now = _utcnow()
    if latest is None:
        new_row = DailySummary(
            local_date=target_date.isoformat(),
            version=1,
            computed_seconds=new_computed_seconds,
            adjustment_seconds=0,
            adjustment_reason=None,
            claimed_seconds=new_computed_seconds,
            locked=False,
            locked_at=None,
            created_at=now,
            created_by="sessioniser",
            rule_version=rules.rule_version,
        )
        db.add(new_row)
        db.flush()
        return new_row.version, True

    if latest.computed_seconds == new_computed_seconds:
        return latest.version, False

    new_claimed = max(0, new_computed_seconds + latest.adjustment_seconds)
    new_row = DailySummary(
        local_date=target_date.isoformat(),
        version=latest.version + 1,
        computed_seconds=new_computed_seconds,
        adjustment_seconds=latest.adjustment_seconds,
        adjustment_reason=latest.adjustment_reason,
        claimed_seconds=new_claimed,
        locked=False,
        locked_at=None,
        created_at=now,
        created_by="sessioniser",
        rule_version=rules.rule_version,
    )
    db.add(new_row)
    db.flush()
    return new_row.version, True


def sessionise_date(db: Session, target_date: date, rules: RuleSet) -> SessioniseResult:
    """Run sessionisation for ``target_date`` and persist results.

    Single transaction (caller's responsibility to commit / rollback). Returns
    a summary of what happened.
    """
    observations = _load_observations(db, target_date, rules)
    sessions = build_sessions_for_date(target_date, observations, rules)
    new_total = computed_seconds_total(sessions)

    _replace_sessions(db, target_date, sessions, rules)
    version, changed = _ensure_summary_version(db, target_date, new_total, rules)

    state = db.execute(select(PollerState).limit(1)).scalar_one_or_none()
    if state is not None:
        state.last_sessioniser_run_at = _utcnow()

    logger.info(
        "sessioniser: date=%s sessions=%d computed_seconds=%d version=%d changed=%s",
        target_date.isoformat(),
        len(sessions),
        new_total,
        version,
        changed,
    )
    return SessioniseResult(
        target_date=target_date,
        sessions_built=len(sessions),
        computed_seconds=new_total,
        daily_summary_version=version,
        daily_summary_changed=changed,
    )


def dates_needing_resessionisation(db: Session, today_local: date) -> list[date]:
    """Return dates that the nightly job should re-run.

    Per HANDOFF: yesterday plus any non-locked dates in the trailing 7 days.
    """
    from datetime import timedelta

    yesterday = today_local - timedelta(days=1)
    window_start = today_local - timedelta(days=7)
    # Always include yesterday.
    out: set[date] = {yesterday}
    # Find non-locked summaries in the window.
    stmt = (
        select(DailySummary.local_date, DailySummary.locked, DailySummary.version)
        .where(DailySummary.local_date >= window_start.isoformat())
        .where(DailySummary.local_date <= yesterday.isoformat())
        .order_by(DailySummary.local_date.asc(), DailySummary.version.desc())
    )
    latest_per_date: dict[str, bool] = {}
    for local_date_str, locked, _version in db.execute(stmt):
        if local_date_str not in latest_per_date:
            latest_per_date[local_date_str] = bool(locked)
    for d_str, locked in latest_per_date.items():
        if not locked:
            out.add(date.fromisoformat(d_str))
    return sorted(out)
