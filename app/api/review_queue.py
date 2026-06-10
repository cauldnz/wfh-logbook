"""Review queue: dates needing human attention (HANDOFF §6 Phase 8.A).

Read-only analysis — nothing here writes to the database.

Categories:

- ``unlocked_backlog``: latest summary version unlocked, date before today.
- ``anomalous``: claimed_seconds exceeds the daily cap (METHODOLOGY §4.5).
- ``data_gap``: a hole longer than ``2 x gap_bridge_minutes`` between
  consecutive observation rows inside a session window where the earlier
  row says *connected*. The poller writes ~every POLL_INTERVAL_SECONDS for
  a connected device, so a silent hole means poller outage or host sleep —
  NOT absence from the SSID (absence produces a disconnect row, after which
  holes are expected and handled by gap-bridging).
- ``heavy_bridging``: bridged time > 15% of a session, or ≥ 4 bridges in
  one session — worth a glance even though bridging is methodology-sanctioned.
"""

from __future__ import annotations

import itertools
import logging
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import Config, DailySummary, Observation, WorkSession

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["review-queue"])

# How far back the queue scans when there is no older unlocked backlog.
SCAN_WINDOW_DAYS = 90
# Heavy-bridging thresholds (HANDOFF Phase 8.A).
HEAVY_BRIDGE_FRACTION = 0.15
HEAVY_BRIDGE_COUNT = 4


class GapWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: datetime
    end: datetime
    seconds: int


class ReviewQueueItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    local_date: date
    reasons: list[str]
    claimed_seconds: int | None = None
    version: int | None = None
    locked: bool = False
    gaps: list[GapWindow] = []
    bridged_gaps_count: int = 0
    bridged_gaps_seconds: int = 0
    session_seconds: int = 0


class ReviewQueueResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    today: date
    from_date: date
    items: list[ReviewQueueItem]


def _ensure_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def find_observation_gaps(
    db: Session,
    target_date: date,
    gap_bridge_minutes: int,
) -> list[GapWindow]:
    """In-session silent holes in the observation stream for one date.

    Walks consecutive observation rows inside each session window; a hole
    longer than 2 x gap_bridge_minutes after a *connected* row is a gap. A
    hole after a disconnect row is genuine absence (bridged or session
    boundary), not a poller outage.
    """
    threshold = timedelta(minutes=2 * gap_bridge_minutes)
    sessions = list(
        db.execute(
            select(WorkSession).where(WorkSession.local_date == target_date.isoformat())
        ).scalars()
    )
    if not sessions:
        return []

    gaps: list[GapWindow] = []
    for sess in sessions:
        start = _ensure_utc(sess.started_at)
        end = _ensure_utc(sess.ended_at)
        assert start is not None and end is not None
        rows = list(
            db.execute(
                select(Observation).order_by(Observation.observed_at.asc(), Observation.id.asc())
            ).scalars()
        )
        # Effective timestamps inside this session window.
        stamped: list[tuple[datetime, bool]] = []
        for r in rows:
            eff = _ensure_utc(r.controller_seen_at) or _ensure_utc(r.observed_at)
            assert eff is not None
            if start <= eff <= end:
                stamped.append((eff, bool(r.is_connected)))
        stamped.sort(key=lambda t: t[0])
        for (t0, connected0), (t1, _c1) in itertools.pairwise(stamped):
            if connected0 and (t1 - t0) > threshold:
                gaps.append(GapWindow(start=t0, end=t1, seconds=int((t1 - t0).total_seconds())))
    return gaps


def build_review_queue(db: Session, today_local: date) -> ReviewQueueResponse:
    cfg = db.execute(select(Config).limit(1)).scalar_one()
    cap_seconds = cfg.daily_cap_hours * 3600
    yesterday = today_local - timedelta(days=1)
    window_start = today_local - timedelta(days=SCAN_WINDOW_DAYS)

    # Latest version per date in the scan window (older unlocked dates are
    # included too — backlog has no statute of limitations).
    rows = list(
        db.execute(
            select(DailySummary)
            .where(DailySummary.local_date <= yesterday.isoformat())
            .order_by(DailySummary.local_date.asc(), DailySummary.version.desc())
        ).scalars()
    )
    latest_by_date: dict[str, DailySummary] = {}
    for r in rows:
        latest_by_date.setdefault(r.local_date, r)

    items: list[ReviewQueueItem] = []
    for date_str, summary in sorted(latest_by_date.items()):
        d = date.fromisoformat(date_str)
        in_window = d >= window_start
        locked = bool(summary.locked)
        reasons: list[str] = []

        if not locked:
            reasons.append("unlocked_backlog")
        if summary.claimed_seconds > cap_seconds and in_window:
            reasons.append("anomalous")

        gaps: list[GapWindow] = []
        bridged_count = 0
        bridged_seconds = 0
        session_seconds = 0
        if in_window and not locked:
            sessions = list(
                db.execute(select(WorkSession).where(WorkSession.local_date == date_str)).scalars()
            )
            bridged_count = sum(s.bridged_gaps_count for s in sessions)
            bridged_seconds = sum(s.bridged_gaps_seconds for s in sessions)
            session_seconds = sum(s.duration_seconds for s in sessions)
            gaps = find_observation_gaps(db, d, cfg.gap_bridge_minutes)
            if gaps:
                reasons.append("data_gap")
            heavy = (
                session_seconds > 0 and bridged_seconds > HEAVY_BRIDGE_FRACTION * session_seconds
            ) or any(s.bridged_gaps_count >= HEAVY_BRIDGE_COUNT for s in sessions)
            if heavy:
                reasons.append("heavy_bridging")

        if reasons:
            items.append(
                ReviewQueueItem(
                    local_date=d,
                    reasons=reasons,
                    claimed_seconds=summary.claimed_seconds,
                    version=summary.version,
                    locked=locked,
                    gaps=gaps,
                    bridged_gaps_count=bridged_count,
                    bridged_gaps_seconds=bridged_seconds,
                    session_seconds=session_seconds,
                )
            )

    return ReviewQueueResponse(today=today_local, from_date=window_start, items=items)


@router.get("/review-queue", response_model=ReviewQueueResponse)
def get_review_queue(db: Session = Depends(get_session)) -> ReviewQueueResponse:  # noqa: B008
    cfg = db.execute(select(Config).limit(1)).scalar_one()
    today_local = datetime.now(ZoneInfo(cfg.local_timezone)).date()
    return build_review_queue(db, today_local)
