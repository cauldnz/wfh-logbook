"""On-demand backups API (HANDOFF §6 Phase 8.D).

- ``POST /api/backup``          — take a snapshot now (and prune per retention).
- ``GET  /api/backups``         — list snapshots, newest first.
- ``GET  /api/backups/{name}``  — download one snapshot.

Download names are validated against the exact snapshot filename pattern —
anything else is a 404, so no path traversal is possible. Off-box copies
remain the user's responsibility (ARCHITECTURE §7.1); this just makes them
easy to take.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.backup.snapshot import SNAPSHOT_FILENAME_RE, run_snapshot
from app.config import get_settings
from app.db import get_session
from app.models import Config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["backups"])


class SnapshotInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    bytes: int
    modified_at: datetime


class SnapshotList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backups_dir: str
    snapshots: list[SnapshotInfo]


class BackupResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    bytes: int


@router.get("/backups", response_model=SnapshotList)
def list_backups() -> SnapshotList:
    backup_dir = get_settings().data_dir / "backups"
    snapshots: list[SnapshotInfo] = []
    if backup_dir.exists():
        for p in backup_dir.iterdir():
            if not p.is_file() or not SNAPSHOT_FILENAME_RE.search(p.name):
                continue
            stat = p.stat()
            snapshots.append(
                SnapshotInfo(
                    name=p.name,
                    bytes=stat.st_size,
                    modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
                )
            )
    snapshots.sort(key=lambda s: s.name, reverse=True)
    return SnapshotList(backups_dir=str(backup_dir), snapshots=snapshots)


@router.post("/backup", response_model=BackupResult)
def backup_now(db: Session = Depends(get_session)) -> BackupResult:  # noqa: B008
    cfg = db.execute(select(Config).limit(1)).scalar_one_or_none()
    tz_name = cfg.local_timezone if cfg else "UTC"
    backup_dir = get_settings().data_dir / "backups"
    path = run_snapshot(backup_dir, tz_name)
    logger.info("backup: on-demand snapshot %s", path.name)
    return BackupResult(name=path.name, bytes=path.stat().st_size)


@router.get("/backups/{name}")
def download_backup(name: str) -> FileResponse:
    # Strict pattern match — rejects traversal, weird names, everything
    # that isn't exactly a snapshot filename.
    if not SNAPSHOT_FILENAME_RE.fullmatch(name):
        raise HTTPException(status_code=404, detail="no such snapshot")
    path = get_settings().data_dir / "backups" / name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="no such snapshot")
    return FileResponse(
        path,
        media_type="application/vnd.sqlite3",
        filename=name,
    )
