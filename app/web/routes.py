"""HTML routes for the review UI.

The web layer calls the SAME service functions as the JSON API
(``app.api.days_service``). Routes that mutate state return rendered HTML
fragments so HTMX can swap them into the page without a full reload.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.days_service import (
    AdjustParams,
    _get_config,
    adjust_day,
    get_day,
    list_days,
    lock_day,
)
from app.db import get_session
from app.models import PollerState
from app.schemas import DailySummaryOut, WorkSessionOut
from app.sessions.persistence import sessionise_date
from app.sessions.rules import RuleSet

logger = logging.getLogger(__name__)

# Resolve template dir relative to this file so tests don't depend on CWD.
_BASE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(_BASE / "templates"))

router = APIRouter(tags=["web"])


# ----------------------------------------------------- AU financial year helper
def current_fy_label(today: date) -> str:
    """Return e.g. '2025-26' for any date in that AU FY (1 Jul - 30 Jun)."""
    start = today.year if today.month >= 7 else today.year - 1
    end_short = (start + 1) % 100
    return f"{start}-{end_short:02d}"


def fy_bounds(fy: str) -> tuple[date, date]:
    """Parse '2025-26' to (date(2025, 7, 1), date(2026, 6, 30))."""
    try:
        start_year_str, end_short = fy.split("-")
        start_year = int(start_year_str)
        end_year = start_year + 1
        assert int(end_short) == end_year % 100
    except (ValueError, AssertionError) as e:
        raise HTTPException(status_code=400, detail=f"bad FY label {fy!r}") from e
    return date(start_year, 7, 1), date(end_year, 6, 30)


# ------------------------------------------------------------ banner helpers
def _poll_banner_text(state: PollerState | None) -> str | None:
    """Per ARCHITECTURE §7.2: show a banner if last successful poll is stale."""
    if state is None or state.last_poll_succeeded_at is None:
        return "never"
    last = state.last_poll_succeeded_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    age = datetime.now(UTC) - last
    if age > timedelta(minutes=30):
        # Format compactly.
        minutes = int(age.total_seconds() // 60)
        if minutes < 60:
            return f"{minutes} minutes"
        hours = minutes // 60
        return f"{hours} hours"
    return None


def _base_context(db: Session) -> dict:  # type: ignore[type-arg]
    cfg = _get_config(db)
    state = db.execute(select(PollerState).limit(1)).scalar_one_or_none()
    tz = ZoneInfo(cfg.local_timezone)
    today = datetime.now(tz).date()
    return {
        "rule_version": cfg.rule_version,
        "tz": tz,
        "current_fy": current_fy_label(today),
        "poll_banner": _poll_banner_text(state),
    }


# ---------------------------------------------------------- view-model glue
@dataclass(slots=True)
class _DayViewModel:
    local_date: date
    latest: DailySummaryOut | None
    versions: list[DailySummaryOut]
    sessions: list[WorkSessionOut]


def _vm(db: Session, target_date: date) -> _DayViewModel:
    detail = get_day(db, target_date)
    return _DayViewModel(
        local_date=target_date,
        latest=detail.latest,
        versions=detail.versions,
        sessions=detail.sessions,
    )


# =============================================================== GET routes
@router.get("/", response_class=HTMLResponse)
def review(
    request: Request,
    db: Session = Depends(get_session),  # noqa: B008
) -> HTMLResponse:
    ctx = _base_context(db)
    today_local = datetime.now(ctx["tz"]).date()
    yesterday = today_local - timedelta(days=1)
    day_vm = _vm(db, yesterday)
    today_vm = _vm(db, today_local)
    # Don't show "today" card if there's nothing to show yet.
    show_today = today_vm.latest is not None or today_vm.sessions
    return templates.TemplateResponse(
        request,
        "review.html",
        {**ctx, "active": "review", "day": day_vm, "today": today_vm if show_today else None},
    )


@router.get("/review-queue", response_class=HTMLResponse)
def review_queue_view(
    request: Request,
    db: Session = Depends(get_session),  # noqa: B008
) -> HTMLResponse:
    from app.api.review_queue import build_review_queue

    ctx = _base_context(db)
    today_local = datetime.now(ctx["tz"]).date()
    queue = build_review_queue(db, today_local)
    return templates.TemplateResponse(
        request,
        "review_queue.html",
        {**ctx, "active": "queue", "queue": queue},
    )


@router.get("/calendar", response_class=HTMLResponse)
def calendar_view(
    request: Request,
    db: Session = Depends(get_session),  # noqa: B008
) -> HTMLResponse:
    ctx = _base_context(db)
    today_local = datetime.now(ctx["tz"]).date()
    start = today_local - timedelta(days=89)
    listing = list_days(db, start, today_local)
    # Pad to align with Monday-start columns. Python weekday(): Mon=0..Sun=6.
    leading_pad = start.weekday()
    return templates.TemplateResponse(
        request,
        "calendar.html",
        {
            **ctx,
            "active": "calendar",
            "days": listing.days,
            "leading_pad": leading_pad,
        },
    )


@router.get("/year/{fy}", response_class=HTMLResponse)
def year_view(
    fy: str,
    request: Request,
    db: Session = Depends(get_session),  # noqa: B008
) -> HTMLResponse:
    ctx = _base_context(db)
    fy_start, fy_end = fy_bounds(fy)
    listing = list_days(db, fy_start, fy_end)

    total_claimed_seconds = 0
    locked_count = 0
    unlocked_count = 0
    anomalous_count = 0
    months: dict[str, dict] = {}  # type: ignore[type-arg]
    for d in listing.days:
        if d.latest is None:
            continue
        total_claimed_seconds += d.latest.claimed_seconds
        if d.latest.locked:
            locked_count += 1
        else:
            unlocked_count += 1
        if d.latest.anomalous:
            anomalous_count += 1
        key = d.local_date.strftime("%Y-%m")
        m = months.setdefault(
            key, {"label": d.local_date.strftime("%b %Y"), "locked": 0, "unlocked": 0, "hours": 0.0}
        )
        if d.latest.locked:
            m["locked"] += 1
        else:
            m["unlocked"] += 1
        m["hours"] += d.latest.claimed_seconds / 3600

    return templates.TemplateResponse(
        request,
        "year.html",
        {
            **ctx,
            "active": "year",
            "fy": fy,
            "fy_start": fy_start,
            "fy_end": fy_end,
            "total_claimed_hours": total_claimed_seconds / 3600,
            "locked_count": locked_count,
            "unlocked_count": unlocked_count,
            "anomalous_count": anomalous_count,
            "months": sorted(months.values(), key=lambda m: m["label"]),
        },
    )


@router.get("/day/{target_date}", response_class=HTMLResponse)
def day_detail(
    target_date: date,
    request: Request,
    db: Session = Depends(get_session),  # noqa: B008
) -> HTMLResponse:
    ctx = _base_context(db)
    day_vm = _vm(db, target_date)
    return templates.TemplateResponse(
        request,
        "day_detail.html",
        {**ctx, "active": "calendar", "day": day_vm},
    )


# =================================== POST routes (HTMX form-encoded bodies)
def _render_day_card(
    request: Request,
    db: Session,
    target_date: date,
) -> HTMLResponse:
    ctx = _base_context(db)
    day_vm = _vm(db, target_date)
    return templates.TemplateResponse(
        request,
        "_day_card.html",
        {**ctx, "day": day_vm},
    )


@router.post("/web/days/{target_date}/adjust", response_class=HTMLResponse)
def web_adjust(
    target_date: date,
    request: Request,
    minutes: int = Form(...),
    reason: str = Form(...),
    db: Session = Depends(get_session),  # noqa: B008
) -> HTMLResponse:
    adjust_day(
        db,
        target_date,
        AdjustParams(adjustment_seconds=minutes * 60, reason=reason, created_by="web"),
    )
    return _render_day_card(request, db, target_date)


@router.post("/web/days/{target_date}/lock", response_class=HTMLResponse)
def web_lock(
    target_date: date,
    request: Request,
    db: Session = Depends(get_session),  # noqa: B008
) -> HTMLResponse:
    lock_day(db, target_date)
    return _render_day_card(request, db, target_date)


@router.post("/web/days/{target_date}/resessionise", response_class=HTMLResponse)
def web_resessionise(
    target_date: date,
    request: Request,
    db: Session = Depends(get_session),  # noqa: B008
) -> HTMLResponse:
    rules = RuleSet.from_db(db)
    sessionise_date(db, target_date, rules)
    return _render_day_card(request, db, target_date)
