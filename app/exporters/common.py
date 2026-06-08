"""Helpers shared by xlsx + csv exporters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.models import Config, DailySummary, Device

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


# Bracketed placeholders in METHODOLOGY.md that we substitute at export time.
# Keys are exact matches of the text inside the brackets (including any
# inline default annotation). The value is a callable that produces the
# replacement from the current Config row.
PLACEHOLDER_REPLACEMENTS: dict[str, str] = {
    # Filled directly from the Config row at export time:
    "WORK_SSID": "work_ssid",
    "GAP_BRIDGE_MINUTES, default 10": "gap_bridge_minutes",
    "MIN_SESSION_MINUTES, default 2": "min_session_minutes",
    "DAILY_CAP_HOURS, default 12": "daily_cap_hours",
    "GAP_BRIDGE_MINUTES": "gap_bridge_minutes",
    "MIN_SESSION_MINUTES": "min_session_minutes",
    "DAILY_CAP_HOURS": "daily_cap_hours",
}


@dataclass(frozen=True, slots=True)
class SummaryRow:
    """One row in the export Summary sheet / CSV."""

    local_date: date
    day_of_week: str
    computed_hours: float
    adjustment_hours: float
    adjustment_reason: str
    claimed_hours: float
    version: int
    locked: bool
    locked_at: datetime | None
    rule_version: str
    created_by: str


def _ensure_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def _to_local(dt: datetime | None, tz_name: str) -> datetime | None:
    aware = _ensure_utc(dt)
    if aware is None:
        return None
    return aware.astimezone(ZoneInfo(tz_name))


def latest_summaries_in_range(
    db: Session,
    from_date: date,
    to_date: date,
) -> list[DailySummary]:
    """Return the latest version of each ``daily_summaries`` row in [from, to]."""
    subq = (
        select(
            DailySummary.local_date.label("ld"),
            func.max(DailySummary.version).label("max_v"),
        )
        .where(DailySummary.local_date >= from_date.isoformat())
        .where(DailySummary.local_date <= to_date.isoformat())
        .group_by(DailySummary.local_date)
        .subquery()
    )
    rows = list(
        db.execute(
            select(DailySummary)
            .join(
                subq,
                (DailySummary.local_date == subq.c.ld) & (DailySummary.version == subq.c.max_v),
            )
            .order_by(DailySummary.local_date)
        ).scalars()
    )
    return rows


def build_summary_rows(
    db: Session,
    from_date: date,
    to_date: date,
    tz_name: str,
) -> list[SummaryRow]:
    """Build SummaryRow dataclasses for the [from_date, to_date] window."""
    out: list[SummaryRow] = []
    for ds in latest_summaries_in_range(db, from_date, to_date):
        ld = date.fromisoformat(ds.local_date)
        out.append(
            SummaryRow(
                local_date=ld,
                day_of_week=ld.strftime("%A"),
                computed_hours=ds.computed_seconds / 3600,
                adjustment_hours=ds.adjustment_seconds / 3600,
                adjustment_reason=ds.adjustment_reason or "",
                claimed_hours=ds.claimed_seconds / 3600,
                version=ds.version,
                locked=bool(ds.locked),
                locked_at=_to_local(ds.locked_at, tz_name),
                rule_version=ds.rule_version,
                created_by=ds.created_by,
            )
        )
    return out


def get_config(db: Session) -> Config:
    return db.execute(select(Config).limit(1)).scalar_one()


def get_active_devices(db: Session) -> list[Device]:
    return list(db.execute(select(Device).order_by(Device.label)).scalars())


def render_methodology_with_config(
    template: str,
    cfg: Config,
    fy_label: str | None = None,
) -> str:
    """Substitute ``[PLACEHOLDER]`` markers in METHODOLOGY.md text.

    Unknown placeholders are left in place (the methodology is a template;
    the taxpayer fills the rest manually).
    """
    text = template
    # Direct config-driven substitutions.
    for marker, attr in PLACEHOLDER_REPLACEMENTS.items():
        text = text.replace(f"[{marker}]", str(getattr(cfg, attr)))
    if fy_label is not None:
        text = text.replace("[e.g. 2025-26]", fy_label)
        # METHODOLOGY.md uses an en-dash variant of the same placeholder
        # (built at runtime via chr() to avoid the RUF001 ambiguity rule on
        # a literal en-dash in source).
        text = text.replace(f"[e.g. 2025{chr(0x2013)}26]", fy_label)
    # Rule version in effect.
    text = text.replace("[e.g. 2026.1]", cfg.rule_version)
    return text
