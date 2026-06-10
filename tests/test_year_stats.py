"""Year-view statistics (HANDOFF §6 Phase 8.C). Pure-function tests."""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.web.year_stats import DayStat, compute_year_stats

FY_START = date(2025, 7, 1)
FY_END = date(2026, 6, 30)  # 365 days


def day(d: date, hours: float, locked: bool = True) -> DayStat:
    return DayStat(local_date=d, claimed_seconds=int(hours * 3600), locked=locked)


class TestProjection:
    def test_partial_year_projects_at_current_pace(self) -> None:
        # 73 elapsed days (1 Jul - 11 Sep inclusive), 100 h claimed.
        today = date(2025, 9, 12)
        stats = compute_year_stats([day(date(2025, 8, 1), 100.0)], FY_START, FY_END, today)
        assert stats.elapsed_days == 73
        assert stats.total_days == 365
        assert stats.projected_year_end_hours == pytest.approx(100.0 * 365 / 73)

    def test_fully_elapsed_year_projects_actual_total(self) -> None:
        stats = compute_year_stats(
            [day(date(2026, 1, 5), 500.0)], FY_START, FY_END, date(2026, 7, 15)
        )
        assert stats.projected_year_end_hours == pytest.approx(500.0)

    def test_before_fy_starts_no_projection(self) -> None:
        stats = compute_year_stats([], FY_START, FY_END, date(2025, 6, 1))
        assert stats.projected_year_end_hours is None
        assert stats.elapsed_days == 0
        assert stats.weekly_average_hours == 0.0


class TestAverages:
    def test_weekly_average(self) -> None:
        # 14 elapsed days = 2 weeks; 20 h claimed → 10 h/week.
        today = date(2025, 7, 15)
        stats = compute_year_stats([day(date(2025, 7, 3), 20.0)], FY_START, FY_END, today)
        assert stats.elapsed_days == 14
        assert stats.weekly_average_hours == pytest.approx(10.0)

    def test_weekday_averages_only_count_recorded_days(self) -> None:
        # Two Mondays (8 h, 6 h) and one Friday (4 h).
        days = [
            day(date(2025, 7, 7), 8.0),  # Monday
            day(date(2025, 7, 14), 6.0),  # Monday
            day(date(2025, 7, 11), 4.0),  # Friday
        ]
        stats = compute_year_stats(days, FY_START, FY_END, date(2025, 7, 20))
        by_name = {w.weekday: w for w in stats.weekday_averages}
        assert by_name["Monday"].average_hours == pytest.approx(7.0)
        assert by_name["Monday"].day_count == 2
        assert by_name["Friday"].average_hours == pytest.approx(4.0)
        assert "Sunday" not in by_name  # no data → no row


class TestLockedProgress:
    def test_locked_fraction(self) -> None:
        days = [
            day(date(2025, 7, 7), 8.0, locked=True),
            day(date(2025, 7, 8), 8.0, locked=True),
            day(date(2025, 7, 9), 8.0, locked=False),
            day(date(2025, 7, 10), 8.0, locked=False),
        ]
        stats = compute_year_stats(days, FY_START, FY_END, date(2025, 7, 20))
        assert stats.locked_days == 2
        assert stats.unlocked_days == 2
        assert stats.locked_fraction == pytest.approx(0.5)

    def test_no_data_zero_fraction(self) -> None:
        stats = compute_year_stats([], FY_START, FY_END, date(2025, 8, 1))
        assert stats.locked_fraction == 0.0
        assert stats.days_with_data == 0


class TestYearPageIntegration:
    def test_year_page_renders_stats(self, client: TestClient) -> None:
        resp = client.get("/year/2025-26")
        assert resp.status_code == 200
        assert "weekly average" in resp.text
        # The no-dollar disclaimer is present.
        assert "not a claim about deductibility" in resp.text
        # No dollar signs in the stats card (hours only).
        assert "$" not in resp.text
