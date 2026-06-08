"""SQLite ``VACUUM INTO`` snapshots with rotation.

ARCHITECTURE §7.1: nightly at 02:00 local time. Retain 30 daily snapshots and
12 monthly snapshots. The first snapshot of each calendar month is the
monthly retention candidate.

The snapshot is a complete, consistent copy of the SQLite DB at a point in
time. ``VACUUM INTO`` is safer than file-level copy because it runs through
SQLite and respects WAL mode / open writes.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from sqlalchemy import select, text

from app.db import get_engine, get_sessionmaker
from app.models import PollerState

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

SNAPSHOT_FILENAME_RE = re.compile(r"wfh-logbook-(\d{8})\.sqlite$")
DAILY_RETAIN = 30
MONTHLY_RETAIN = 12


def backup_filename_for(d: date) -> str:
    return f"wfh-logbook-{d.strftime('%Y%m%d')}.sqlite"


def _parse_snapshot_date(path: Path) -> date | None:
    m = SNAPSHOT_FILENAME_RE.search(path.name)
    if not m:
        return None
    try:
        return date(int(m.group(1)[0:4]), int(m.group(1)[4:6]), int(m.group(1)[6:8]))
    except ValueError:
        return None


def _list_snapshots(backup_dir: Path) -> list[tuple[date, Path]]:
    if not backup_dir.exists():
        return []
    out: list[tuple[date, Path]] = []
    for p in backup_dir.iterdir():
        if not p.is_file():
            continue
        d = _parse_snapshot_date(p)
        if d is None:
            continue
        out.append((d, p))
    out.sort()
    return out


def snapshot(backup_dir: Path, target_date: date | None = None) -> Path:
    """Write a snapshot to ``backup_dir`` named for ``target_date`` (default today UTC).

    Returns the snapshot path. The caller is responsible for choosing the
    timezone of ``target_date``; the scheduler passes the configured local
    timezone's date.
    """
    target_date = target_date or datetime.now(UTC).date()
    backup_dir.mkdir(parents=True, exist_ok=True)
    out_path = backup_dir / backup_filename_for(target_date)
    engine = get_engine()
    # VACUUM INTO cannot run inside a transaction; use raw_connection.
    raw = engine.raw_connection()
    try:
        cursor = raw.cursor()
        try:
            # SQLite parameter binding doesn't work for VACUUM INTO's target;
            # safe-quote the path. backup_dir comes from settings.data_dir,
            # not user input.
            quoted = str(out_path).replace("'", "''")
            cursor.execute(f"VACUUM INTO '{quoted}'")
        finally:
            cursor.close()
    finally:
        raw.close()
    _ = text  # imported for future migrations; silence unused warning here.
    logger.info("backup: snapshot written to %s", out_path)
    return out_path


def prune_old_snapshots(
    backup_dir: Path,
    today: date,
    daily_retain: int = DAILY_RETAIN,
    monthly_retain: int = MONTHLY_RETAIN,
) -> list[Path]:
    """Delete snapshots not protected by either retention rule.

    A snapshot is KEPT if:
    - It is within the most recent ``daily_retain`` daily files (counted from
      ``today`` backwards), OR
    - It is the first snapshot of its calendar month AND that month is one of
      the most recent ``monthly_retain`` months that have any snapshot.

    Returns the list of paths that were deleted.
    """
    snapshots = _list_snapshots(backup_dir)
    if not snapshots:
        return []

    # Daily keep window: snapshots with date >= today - (daily_retain - 1) days.
    daily_cutoff = today - timedelta(days=daily_retain - 1)
    keep_dates: set[date] = {d for d, _ in snapshots if d >= daily_cutoff}

    # Monthly: first-of-month snapshots, keep the most recent N months.
    first_of_month: dict[str, date] = {}
    for d, _ in snapshots:
        key = d.strftime("%Y-%m")
        if key not in first_of_month or d < first_of_month[key]:
            first_of_month[key] = d
    months_sorted = sorted(first_of_month.keys(), reverse=True)
    for key in months_sorted[:monthly_retain]:
        keep_dates.add(first_of_month[key])

    deleted: list[Path] = []
    for d, p in snapshots:
        if d not in keep_dates:
            p.unlink()
            deleted.append(p)
    if deleted:
        logger.info(
            "backup: pruned %d snapshot(s): %s",
            len(deleted),
            ", ".join(p.name for p in deleted),
        )
    return deleted


def run_snapshot(backup_dir: Path, timezone_name: str) -> Path:
    """The scheduled callable. Takes a snapshot, prunes, updates poller_state."""
    today_local = datetime.now(ZoneInfo(timezone_name)).date()
    path = snapshot(backup_dir, today_local)
    prune_old_snapshots(backup_dir, today_local)
    # Update poller_state.last_backup_at.
    SessionLocal = get_sessionmaker()  # noqa: N806
    with SessionLocal() as db:
        state = db.execute(select(PollerState).limit(1)).scalar_one_or_none()
        if state is not None:
            state.last_backup_at = datetime.now(UTC)
            db.commit()
    return path
