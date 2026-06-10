"""Pure year-statistics computation for the year view (HANDOFF §6 Phase 8.C).

Hours only — no dollar figures anywhere (hard constraint HANDOFF §2.5).
Pure functions: no DB, no clock reads; ``today`` is an argument.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta


@dataclass(frozen=True, slots=True)
class DayStat:
    """Minimal per-day input: the latest version's claimed seconds."""

    local_date: date
    claimed_seconds: int
    locked: bool


@dataclass(frozen=True, slots=True)
class WeekdayAverage:
    weekday: str  # "Monday" ... "Sunday"
    average_hours: float
    day_count: int


@dataclass(frozen=True, slots=True)
class YearStats:
    total_claimed_hours: float
    locked_days: int
    unlocked_days: int
    days_with_data: int
    elapsed_days: int  # days of the FY elapsed as of `today` (clamped)
    total_days: int  # length of the FY
    weekly_average_hours: float  # claimed-to-date / elapsed weeks
    projected_year_end_hours: float | None  # None before the FY starts
    locked_fraction: float  # locked / days-with-data (0.0 if none)
    weekday_averages: list[WeekdayAverage] = field(default_factory=list)


def compute_year_stats(
    days: list[DayStat],
    fy_start: date,
    fy_end: date,
    today: date,
) -> YearStats:
    """Aggregate stats for a (possibly partially elapsed) financial year.

    ``days`` carries only dates that have a summary; absent dates count as
    zero hours but do not affect weekday averages or locked fractions.
    """
    total_days = (fy_end - fy_start).days + 1
    # Elapsed: complete days from fy_start through min(today-1, fy_end).
    last_complete = min(today - timedelta(days=1), fy_end)
    elapsed_days = max(0, (last_complete - fy_start).days + 1)

    total_claimed_seconds = sum(d.claimed_seconds for d in days)
    total_claimed_hours = total_claimed_seconds / 3600
    locked_days = sum(1 for d in days if d.locked)
    unlocked_days = sum(1 for d in days if not d.locked)
    days_with_data = len(days)

    elapsed_weeks = elapsed_days / 7
    weekly_average_hours = total_claimed_hours / elapsed_weeks if elapsed_weeks > 0 else 0.0

    projected: float | None
    if elapsed_days <= 0:
        projected = None
    elif elapsed_days >= total_days:
        projected = total_claimed_hours
    else:
        projected = total_claimed_hours * total_days / elapsed_days

    locked_fraction = locked_days / days_with_data if days_with_data else 0.0

    # Per-weekday averages across days WITH data only.
    by_weekday: dict[int, list[int]] = {}
    for d in days:
        by_weekday.setdefault(d.local_date.weekday(), []).append(d.claimed_seconds)
    weekday_names = [
        "Monday",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday",
        "Saturday",
        "Sunday",
    ]
    weekday_averages = [
        WeekdayAverage(
            weekday=weekday_names[wd],
            average_hours=(sum(secs) / len(secs)) / 3600,
            day_count=len(secs),
        )
        for wd, secs in sorted(by_weekday.items())
    ]

    return YearStats(
        total_claimed_hours=total_claimed_hours,
        locked_days=locked_days,
        unlocked_days=unlocked_days,
        days_with_data=days_with_data,
        elapsed_days=elapsed_days,
        total_days=total_days,
        weekly_average_hours=weekly_average_hours,
        projected_year_end_hours=projected,
        locked_fraction=locked_fraction,
        weekday_averages=weekday_averages,
    )
