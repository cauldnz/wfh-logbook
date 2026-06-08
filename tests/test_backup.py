"""Backup snapshot + retention tests."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from app.backup.snapshot import (
    backup_filename_for,
    prune_old_snapshots,
    snapshot,
)


def _touch_snapshot(backup_dir: Path, d: date) -> Path:
    """Create an empty file with the snapshot's expected name."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    p = backup_dir / backup_filename_for(d)
    p.write_bytes(b"")
    return p


class TestSnapshotCreation:
    def test_snapshot_creates_file(self, migrated_db, tmp_path) -> None:  # type: ignore[no-untyped-def]
        out = snapshot(tmp_path / "backups", target_date=date(2026, 5, 20))
        assert out.exists()
        assert out.name == "wfh-logbook-20260520.sqlite"
        # SQLite signature.
        with out.open("rb") as f:
            head = f.read(16)
        assert head.startswith(b"SQLite format 3")


class TestRetention:
    def test_keeps_30_daily(self, tmp_path: Path) -> None:
        backup_dir = tmp_path / "b"
        today = date(2026, 6, 30)
        # Create 60 daily snapshots ending at today (no monthly bonus
        # except those naturally first-of-month within the 60-day window).
        for i in range(60):
            _touch_snapshot(backup_dir, today.fromordinal(today.toordinal() - i))
        prune_old_snapshots(backup_dir, today)
        remaining = sorted(p.name for p in backup_dir.iterdir())
        # The most recent 30 daily snapshots are kept.
        kept_dates = [today.fromordinal(today.toordinal() - i) for i in range(30)]
        for d in kept_dates:
            assert backup_filename_for(d) in remaining

    def test_keeps_first_of_month_for_monthly_window(self, tmp_path: Path) -> None:
        backup_dir = tmp_path / "b"
        today = date(2026, 12, 31)
        # One snapshot on the 1st of every month for 18 months back.
        from datetime import timedelta

        for months_back in range(18):
            year = today.year if today.month - months_back > 0 else today.year - 1
            month_index = today.month - months_back
            while month_index <= 0:
                month_index += 12
                year -= 1
            d = date(year, month_index, 1)
            _touch_snapshot(backup_dir, d)
        # Plus one daily for the past 35 days, NOT on the 1st (use the 15th).
        for i in range(35):
            d = today - timedelta(days=i)
            if d.day == 1:
                continue
            _touch_snapshot(backup_dir, d)
        prune_old_snapshots(backup_dir, today)
        names = {p.name for p in backup_dir.iterdir()}

        # The 12 most recent first-of-month snapshots should all be present.
        recent_first_of_month: list[date] = []
        y, m = today.year, today.month
        for _ in range(12):
            recent_first_of_month.append(date(y, m, 1))
            m -= 1
            if m == 0:
                m = 12
                y -= 1
        for d in recent_first_of_month:
            assert backup_filename_for(d) in names, (
                f"expected first-of-month {d.isoformat()} retained"
            )

    def test_prunes_non_first_of_month_outside_daily_window(self, tmp_path: Path) -> None:
        backup_dir = tmp_path / "b"
        today = date(2026, 6, 30)
        # The 1st of January 2024 IS the first-of-month for that month, so it
        # would be kept by monthly retention if Jan-2024 is among the 12 most
        # recent months with any snapshot. To force pruning we use a NON-
        # first-of-month date well outside the 30-day daily window.
        ancient_mid_month = date(2024, 1, 15)
        p = _touch_snapshot(backup_dir, ancient_mid_month)
        # Also add the 1st of Jan 2024 so the first-of-month for that month
        # is NOT our test-target file.
        _touch_snapshot(backup_dir, date(2024, 1, 1))
        # And today.
        _touch_snapshot(backup_dir, today)
        deleted = prune_old_snapshots(backup_dir, today)
        assert p in deleted
        assert not p.exists()

    def test_empty_dir_returns_empty(self, tmp_path: Path) -> None:
        deleted = prune_old_snapshots(tmp_path / "nope", date(2026, 6, 30))
        assert deleted == []
