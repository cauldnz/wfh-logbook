"""GET /api/health.

Phase 1: bare-minimum status + db_ok. Phase 2 wires poller state. Phase 6
enriches with DB size, observation counts, and last successful sessioniser /
backup timestamps.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_session
from app.models import Config, Observation, PollerState
from app.schemas import HealthResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health", response_model=HealthResponse)
def health(db: Session = Depends(get_session)) -> HealthResponse:  # noqa: B008 (FastAPI pattern)
    db_ok = True
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError:
        logger.exception("health: SELECT 1 failed")
        db_ok = False

    rule_version: str | None = None
    try:
        cfg = db.execute(select(Config).limit(1)).scalar_one_or_none()
        if cfg is not None:
            rule_version = cfg.rule_version
    except SQLAlchemyError:
        logger.exception("health: failed to read config")

    poller = None
    try:
        poller = db.execute(select(PollerState).limit(1)).scalar_one_or_none()
    except SQLAlchemyError:
        logger.exception("health: failed to read poller_state")

    # Phase 6 enrichments: DB file size and observation count for the
    # last 24 hours. Both best-effort; failures don't degrade /api/health.
    db_size_bytes: int | None = None
    try:
        db_path = get_settings().db_path()
        if db_path.exists():
            db_size_bytes = db_path.stat().st_size
    except OSError:
        logger.exception("health: failed to stat db")

    observations_last_24h: int | None = None
    try:
        cutoff = datetime.now(UTC) - timedelta(hours=24)
        observations_last_24h = int(
            db.execute(
                select(func.count(Observation.id)).where(Observation.observed_at >= cutoff)
            ).scalar_one()
        )
    except SQLAlchemyError:
        logger.exception("health: failed to count recent observations")

    return HealthResponse(
        status="ok" if db_ok else "degraded",
        db_ok=db_ok,
        last_poll_attempted_at=poller.last_poll_attempted_at if poller else None,
        last_poll_succeeded_at=poller.last_poll_succeeded_at if poller else None,
        consecutive_failures=poller.consecutive_failures if poller else 0,
        last_sessioniser_run_at=poller.last_sessioniser_run_at if poller else None,
        last_backup_at=poller.last_backup_at if poller else None,
        rule_version=rule_version,
        db_size_bytes=db_size_bytes,
        observations_last_24h=observations_last_24h,
    )
