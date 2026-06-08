"""Service functions for day adjust/lock/listing.

Centralised here so the web routes (HTML) and JSON API both call the same
code paths (HANDOFF §6 Phase 4 + §7.G "Do not bypass the internal API from
the Telegram bot"). The bot will call these same functions in Phase 7.

Versioning rules (ARCHITECTURE §5.5):

- Adjustment: ALWAYS creates a new ``daily_summaries`` row with version+1,
  copying ``computed_seconds`` from the latest version. ``locked`` resets
  to False even if the previous version was locked.
- Lock: sets ``locked=1, locked_at=now`` on the latest version (in place).
  Idempotent — locking a locked version is a no-op.
- Resessionise: re-runs the pure sessioniser for the date; new version
  ONLY if computed_seconds differs (otherwise idempotent).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Literal

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Config, DailySummary, WorkSession
from app.schemas import (
    DailySummaryOut,
    DayDetail,
    DayList,
    DayListItem,
    WorkSessionOut,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _ensure_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


# -------------------------------------------------------- conversion helpers
def _to_summary_out(row: DailySummary, daily_cap_hours: int) -> DailySummaryOut:
    return DailySummaryOut(
        local_date=date.fromisoformat(row.local_date),
        version=row.version,
        computed_seconds=row.computed_seconds,
        adjustment_seconds=row.adjustment_seconds,
        adjustment_reason=row.adjustment_reason,
        claimed_seconds=row.claimed_seconds,
        locked=bool(row.locked),
        locked_at=_ensure_utc(row.locked_at),
        created_at=_ensure_utc(row.created_at),
        created_by=row.created_by,
        rule_version=row.rule_version,
        anomalous=row.claimed_seconds > daily_cap_hours * 3600,
    )


def _to_session_out(row: WorkSession) -> WorkSessionOut:
    return WorkSessionOut(
        started_at=_ensure_utc(row.started_at),
        ended_at=_ensure_utc(row.ended_at),
        duration_seconds=row.duration_seconds,
        devices_seen=[d for d in row.devices_seen.split(",") if d],
        bridged_gaps_count=row.bridged_gaps_count,
        bridged_gaps_seconds=row.bridged_gaps_seconds,
        rule_version=row.rule_version,
    )


def _get_config(db: Session) -> Config:
    return db.execute(select(Config).limit(1)).scalar_one()


def _latest_summary(db: Session, target_date: date) -> DailySummary | None:
    return db.execute(
        select(DailySummary)
        .where(DailySummary.local_date == target_date.isoformat())
        .order_by(DailySummary.version.desc())
        .limit(1)
    ).scalar_one_or_none()


def _all_versions(db: Session, target_date: date) -> list[DailySummary]:
    return list(
        db.execute(
            select(DailySummary)
            .where(DailySummary.local_date == target_date.isoformat())
            .order_by(DailySummary.version.asc())
        ).scalars()
    )


def _sessions_for(db: Session, target_date: date) -> list[WorkSession]:
    return list(
        db.execute(
            select(WorkSession)
            .where(WorkSession.local_date == target_date.isoformat())
            .order_by(WorkSession.started_at.asc())
        ).scalars()
    )


# ---------------------------------------------------------- read endpoints
def list_days(
    db: Session,
    from_date: date,
    to_date: date,
) -> DayList:
    """List per-date latest summary across ``[from_date, to_date]`` inclusive."""
    if from_date > to_date:
        raise HTTPException(status_code=400, detail="from must be <= to")
    cfg = _get_config(db)

    # Latest version per local_date in range.
    subq = (
        select(
            DailySummary.local_date.label("ld"),
            func.max(DailySummary.version).label("max_v"),
            func.count(DailySummary.id).label("vcnt"),
        )
        .where(DailySummary.local_date >= from_date.isoformat())
        .where(DailySummary.local_date <= to_date.isoformat())
        .group_by(DailySummary.local_date)
        .subquery()
    )
    rows = list(
        db.execute(
            select(DailySummary, subq.c.vcnt).join(
                subq,
                (DailySummary.local_date == subq.c.ld) & (DailySummary.version == subq.c.max_v),
            )
        )
    )
    latest_by_date: dict[str, tuple[DailySummary, int]] = {
        r[0].local_date: (r[0], r[1]) for r in rows
    }

    # Has-sessions flag per date in range.
    sess_rows = db.execute(
        select(WorkSession.local_date, func.count(WorkSession.id))
        .where(WorkSession.local_date >= from_date.isoformat())
        .where(WorkSession.local_date <= to_date.isoformat())
        .group_by(WorkSession.local_date)
    ).all()
    has_sessions = {r[0]: r[1] > 0 for r in sess_rows}

    # Walk the date range.
    days: list[DayListItem] = []
    d = from_date
    while d <= to_date:
        key = d.isoformat()
        latest_tuple = latest_by_date.get(key)
        if latest_tuple is None:
            days.append(DayListItem(local_date=d, latest=None, version_count=0))
        else:
            row, vcnt = latest_tuple
            days.append(
                DayListItem(
                    local_date=d,
                    latest=_to_summary_out(row, cfg.daily_cap_hours),
                    version_count=vcnt,
                    has_sessions=has_sessions.get(key, False),
                )
            )
        d = d + timedelta(days=1)
    return DayList.model_construct(from_date=from_date, to_date=to_date, days=days)


def get_day(db: Session, target_date: date) -> DayDetail:
    """Full detail for one date."""
    cfg = _get_config(db)
    versions = _all_versions(db, target_date)
    sessions = _sessions_for(db, target_date)
    latest = versions[-1] if versions else None
    return DayDetail(
        local_date=target_date,
        latest=_to_summary_out(latest, cfg.daily_cap_hours) if latest else None,
        versions=[_to_summary_out(v, cfg.daily_cap_hours) for v in versions],
        sessions=[_to_session_out(s) for s in sessions],
    )


# ------------------------------------------------------- mutation endpoints
@dataclass(frozen=True, slots=True)
class AdjustParams:
    adjustment_seconds: int
    reason: str
    created_by: Literal["web", "telegram"] = "web"


def adjust_day(
    db: Session,
    target_date: date,
    params: AdjustParams,
) -> DailySummary:
    """Apply an adjustment by creating a new ``daily_summaries`` version.

    Per ARCHITECTURE §5.5: the new version copies ``computed_seconds`` from
    the latest version, replaces the adjustment, and is unlocked even if the
    previous was locked.
    """
    if not params.reason.strip():
        raise HTTPException(status_code=400, detail="reason is required")
    cfg = _get_config(db)
    latest = _latest_summary(db, target_date)
    if latest is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No daily_summary exists for {target_date.isoformat()}; run sessionisation first."
            ),
        )
    new_computed = latest.computed_seconds
    new_claimed = max(0, new_computed + params.adjustment_seconds)
    new_row = DailySummary(
        local_date=target_date.isoformat(),
        version=latest.version + 1,
        computed_seconds=new_computed,
        adjustment_seconds=params.adjustment_seconds,
        adjustment_reason=params.reason.strip(),
        claimed_seconds=new_claimed,
        locked=False,
        locked_at=None,
        created_at=_utcnow(),
        created_by=params.created_by,
        rule_version=cfg.rule_version,
    )
    db.add(new_row)
    db.flush()
    return new_row


def lock_day(db: Session, target_date: date) -> DailySummary:
    """Lock the latest version (idempotent).

    Per ARCHITECTURE §5.5: lock sets ``locked=1, locked_at=now`` on the latest
    version. Subsequent adjustments create version+1 starting unlocked.
    """
    latest = _latest_summary(db, target_date)
    if latest is None:
        raise HTTPException(status_code=404, detail=f"no summary for {target_date.isoformat()}")
    if not bool(latest.locked):
        latest.locked = True
        latest.locked_at = _utcnow()
        db.flush()
    return latest
