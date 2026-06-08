"""Pydantic schemas for HTTP API I/O."""

from __future__ import annotations

from datetime import date as DateType  # noqa: N812 (avoid shadowing `date` field names)
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


# --------------------------------------------------------------- health
class HealthResponse(BaseModel):
    """Response shape for ``GET /api/health``."""

    model_config = ConfigDict(extra="forbid")

    status: str = Field(..., description="overall: 'ok' | 'degraded'")
    db_ok: bool
    last_poll_attempted_at: datetime | None = None
    last_poll_succeeded_at: datetime | None = None
    consecutive_failures: int = 0
    last_sessioniser_run_at: datetime | None = None
    last_backup_at: datetime | None = None
    rule_version: str | None = None
    db_size_bytes: int | None = None
    observations_last_24h: int | None = None


# ------------------------------------------------------------- daily/sessions
class WorkSessionOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    started_at: datetime
    ended_at: datetime
    duration_seconds: int
    devices_seen: list[str]
    bridged_gaps_count: int
    bridged_gaps_seconds: int
    rule_version: str


class DailySummaryOut(BaseModel):
    """A single version of a daily summary."""

    model_config = ConfigDict(extra="forbid")

    local_date: DateType
    version: int
    computed_seconds: int
    adjustment_seconds: int
    adjustment_reason: str | None
    claimed_seconds: int
    locked: bool
    locked_at: datetime | None
    created_at: datetime
    created_by: str
    rule_version: str
    anomalous: bool = Field(
        ..., description="claimed_seconds > daily_cap_hours*3600 (METHODOLOGY §4.5)"
    )


class DayListItem(BaseModel):
    """Compact per-day item for the calendar listing.

    Only the latest version is surfaced; the version count is included so the
    UI can render a "v2" badge etc.
    """

    model_config = ConfigDict(extra="forbid")

    local_date: DateType
    latest: DailySummaryOut | None = None
    version_count: int = 0
    has_sessions: bool = False


class DayList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_date: DateType = Field(alias="from")
    to_date: DateType = Field(alias="to")
    days: list[DayListItem]


class DayDetail(BaseModel):
    """Full detail for ``GET /api/days/{date}``."""

    model_config = ConfigDict(extra="forbid")

    local_date: DateType
    latest: DailySummaryOut | None
    versions: list[DailySummaryOut]
    sessions: list[WorkSessionOut]


# ----------------------------------------------------------------- mutations
class AdjustRequest(BaseModel):
    """Request body for ``POST /api/days/{date}/adjust``.

    The grammar in `app/notifier/grammar.py` (Phase 7) yields one of these.
    """

    model_config = ConfigDict(extra="forbid")

    adjustment_seconds: int = Field(
        ...,
        description=(
            "Signed seconds: negative deducts (lunch, personal time), "
            "positive adds (e.g. poller-outage backfill). Replaces the "
            "latest version's adjustment (not additive)."
        ),
    )
    reason: str = Field(..., min_length=1, max_length=500)
    created_by: str = Field(default="web", description="web | telegram")


class ActionResponse(BaseModel):
    """Returned by adjust / lock / resessionise. The latest state after the action."""

    model_config = ConfigDict(extra="forbid")

    local_date: DateType
    latest: DailySummaryOut


class ResessioniseResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    local_date: DateType
    sessions_built: int
    computed_seconds: int
    daily_summary_version: int
    daily_summary_changed: bool
    latest: DailySummaryOut
