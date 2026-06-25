"""HTTP routes for /api/days/*.

Thin layer over ``days_service``; the bot (Phase 7) calls the service
functions directly rather than HTTP, but the contract is identical.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.days_service import (
    AdjustParams,
    _get_config,
    _latest_summary,
    _to_summary_out,
    adjust_day,
    get_day,
    list_days,
    lock_clean_days,
    lock_day,
)
from app.db import get_session
from app.schemas import (
    ActionResponse,
    AdjustRequest,
    BulkLockResponse,
    DayDetail,
    DayList,
    ResessioniseResponse,
)
from app.sessions.persistence import sessionise_date
from app.sessions.rules import RuleSet

router = APIRouter(prefix="/api/days", tags=["days"])


@router.get("", response_model=DayList)
def get_days(
    from_: date = Query(..., alias="from"),  # noqa: B008 (FastAPI pattern)
    to: date = Query(...),  # noqa: B008 (FastAPI pattern)
    db: Session = Depends(get_session),  # noqa: B008
) -> DayList:
    return list_days(db, from_, to)


@router.get("/{target_date}", response_model=DayDetail)
def get_day_detail(
    target_date: date,
    db: Session = Depends(get_session),  # noqa: B008
) -> DayDetail:
    return get_day(db, target_date)


@router.post("/{target_date}/adjust", response_model=ActionResponse)
def post_adjust(
    target_date: date,
    body: AdjustRequest,
    db: Session = Depends(get_session),  # noqa: B008
) -> ActionResponse:
    created_by: Literal["web", "telegram"] = "telegram" if body.created_by == "telegram" else "web"
    new_row = adjust_day(
        db,
        target_date,
        AdjustParams(
            adjustment_seconds=body.adjustment_seconds,
            reason=body.reason,
            created_by=created_by,
        ),
    )
    cfg = _get_config(db)
    return ActionResponse(
        local_date=target_date, latest=_to_summary_out(new_row, cfg.daily_cap_hours)
    )


@router.post("/{target_date}/lock", response_model=ActionResponse)
def post_lock(
    target_date: date,
    db: Session = Depends(get_session),  # noqa: B008
) -> ActionResponse:
    latest = lock_day(db, target_date)
    cfg = _get_config(db)
    return ActionResponse(
        local_date=target_date, latest=_to_summary_out(latest, cfg.daily_cap_hours)
    )


@router.post("/{target_date}/resessionise", response_model=ResessioniseResponse)
def post_resessionise(
    target_date: date,
    db: Session = Depends(get_session),  # noqa: B008
) -> ResessioniseResponse:
    rules = RuleSet.from_db(db)
    result = sessionise_date(db, target_date, rules)
    latest = _latest_summary(db, target_date)
    cfg = _get_config(db)
    if latest is None:
        # Shouldn't happen — sessionise_date inserts at minimum version 1.
        from fastapi import HTTPException

        raise HTTPException(status_code=500, detail="sessionisation produced no summary")
    return ResessioniseResponse(
        local_date=target_date,
        sessions_built=result.sessions_built,
        computed_seconds=result.computed_seconds,
        daily_summary_version=result.daily_summary_version,
        daily_summary_changed=result.daily_summary_changed,
        latest=_to_summary_out(latest, cfg.daily_cap_hours),
    )


@router.post("/lock-clean", response_model=BulkLockResponse)
def post_lock_clean(db: Session = Depends(get_session)) -> BulkLockResponse:  # noqa: B008
    """Lock every 'clean' unlocked past day in one action (HANDOFF §6 Phase 10.B).

    Clean = the review queue's only reason is ``unlocked_backlog`` and the day
    has > 0 claimed hours. Anomalous / flagged / 0h days are left for review.
    """
    cfg = _get_config(db)
    today_local = datetime.now(ZoneInfo(cfg.local_timezone)).date()
    result = lock_clean_days(db, today_local)
    return BulkLockResponse(
        locked_dates=result.locked_dates,
        locked_count=len(result.locked_dates),
        skipped_count=result.skipped_count,
    )
