"""HTTP routes for exports."""

from __future__ import annotations

import logging
from datetime import date
from io import BytesIO, StringIO

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.api.days_service import count_unlocked_in_range
from app.config import get_settings
from app.db import get_session
from app.exporters.csv import write_csv
from app.exporters.xlsx import write_xlsx

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["exports"])


def _guard_unlocked(
    db: Session, fy_start: date, fy_end: date, fy: str, allow_unlocked: bool
) -> None:
    """Block an export of a FY containing unlocked days unless explicitly
    allowed (HANDOFF §6 Phase 10.E). An ATO export should not silently ship
    un-reviewed days; the web flow re-requests with allow_unlocked=true."""
    if allow_unlocked:
        return
    n = count_unlocked_in_range(db, fy_start, fy_end)
    if n > 0:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{n} unlocked day(s) in FY {fy}; review & lock them, or re-request with "
                "allow_unlocked=true to export anyway."
            ),
        )


def _fy_bounds(fy: str) -> tuple[date, date]:
    try:
        start_year_str, end_short = fy.split("-")
        start_year = int(start_year_str)
        end_year = start_year + 1
        if int(end_short) != end_year % 100:
            raise ValueError
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"bad FY label {fy!r}") from e
    return date(start_year, 7, 1), date(end_year, 6, 30)


@router.get("/export.xlsx", response_class=Response)
def export_xlsx(
    fy: str = Query(..., description="AU financial year, e.g. 2025-26"),
    allow_unlocked: bool = Query(False, description="export even if the FY has unlocked days"),
    db: Session = Depends(get_session),  # noqa: B008
) -> Response:
    fy_start, fy_end = _fy_bounds(fy)
    _guard_unlocked(db, fy_start, fy_end, fy, allow_unlocked)
    buf = BytesIO()
    write_xlsx(db, fy_start, fy_end, fy, buf)
    return Response(
        content=buf.getvalue(),
        media_type=("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        headers={
            "Content-Disposition": f'attachment; filename="wfh-logbook-{fy}.xlsx"',
        },
    )


@router.get("/export.bundle", response_class=Response)
def export_bundle(
    fy: str = Query(..., description="AU financial year, e.g. 2025-26"),
    allow_unlocked: bool = Query(False, description="export even if the FY has unlocked days"),
    db: Session = Depends(get_session),  # noqa: B008
) -> Response:
    """Audit bundle: zip of XLSX + methodology + raw CSVs + SHA-256 manifest."""
    from app.exporters.bundle import write_bundle

    fy_start, fy_end = _fy_bounds(fy)
    _guard_unlocked(db, fy_start, fy_end, fy, allow_unlocked)
    buf = BytesIO()
    write_bundle(db, fy_start, fy_end, fy, buf)
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="wfh-logbook-audit-{fy}.zip"',
        },
    )


@router.get("/export.csv", response_class=Response)
def export_csv(
    from_: date = Query(..., alias="from"),  # noqa: B008
    to: date = Query(...),  # noqa: B008
    db: Session = Depends(get_session),  # noqa: B008
) -> Response:
    if from_ > to:
        raise HTTPException(status_code=400, detail="from must be <= to")
    tz_name = get_settings().local_timezone
    buf = StringIO()
    write_csv(db, from_, to, buf, tz_name)
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": (f'attachment; filename="wfh-logbook-{from_}-to-{to}.csv"'),
        },
    )
