"""Pydantic schemas for API I/O.

Grows phase-by-phase. Phase 1 covers only the /api/health response.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class HealthResponse(BaseModel):
    """Response shape for ``GET /api/health``.

    Phase 1 carries the minimum fields; Phase 6 enriches with DB size, 24h
    observation counts, and last-sessioniser/last-backup timestamps.
    """

    model_config = ConfigDict(extra="forbid")

    status: str = Field(..., description="overall: 'ok' | 'degraded'")
    db_ok: bool
    last_poll_attempted_at: datetime | None = None
    last_poll_succeeded_at: datetime | None = None
    consecutive_failures: int = 0
    last_sessioniser_run_at: datetime | None = None
    last_backup_at: datetime | None = None
    rule_version: str | None = None
    # Phase 6 enrichments. Optional in Phase 1; populated later.
    db_size_bytes: int | None = None
    observations_last_24h: int | None = None
