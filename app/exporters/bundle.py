"""Audit bundle: one zip containing everything an auditor needs
(HANDOFF §6 Phase 8.B).

Contents for a financial year:

- ``wfh-logbook-{fy}.xlsx``       — the Phase 5 workbook.
- ``methodology.md``              — METHODOLOGY.md populated from live config.
- ``observations.csv``            — raw evidence rows in the FY window.
- ``sessions.csv``                — derived sessions.
- ``daily_summaries.csv``         — ALL versions (the full audit trail),
                                    not just the latest.
- ``manifest.json``               — generated-at, app version, rule_version,
                                    config snapshot, per-file row counts and
                                    SHA-256 hashes.

Stdlib only (zipfile, hashlib, csv, json) — no new dependencies.
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
from datetime import UTC, date, datetime
from io import BytesIO, StringIO
from typing import TYPE_CHECKING
from zipfile import ZIP_DEFLATED, ZipFile

from sqlalchemy import select

from app import __version__
from app.exporters.common import get_config, render_methodology_with_config
from app.exporters.xlsx import METHODOLOGY_MD_PATH, write_xlsx
from app.models import DailySummary, Observation, WorkSession
from app.sessions.builder import utc_buffer_for

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _csv_bytes(headers: list[str], rows: list[list[object]]) -> bytes:
    buf = StringIO()
    w = csv.writer(buf)
    w.writerow(headers)
    w.writerows(rows)
    return buf.getvalue().encode("utf-8")


def _observations_csv(db: Session, fy_start: date, fy_end: date, tz_name: str) -> bytes:
    """Raw evidence rows whose effective timestamp falls in the FY window.

    The window uses the same buffered UTC bounds as the sessioniser so
    midnight-crossing evidence is included.
    """
    lower, _ = utc_buffer_for(fy_start, tz_name)
    _, upper = utc_buffer_for(fy_end, tz_name)
    rows: list[list[object]] = []
    for r in db.execute(
        select(Observation).order_by(Observation.observed_at.asc(), Observation.id.asc())
    ).scalars():
        eff = r.controller_seen_at if r.controller_seen_at is not None else r.observed_at
        if eff.tzinfo is None:
            eff = eff.replace(tzinfo=UTC)
        if eff < lower or eff > upper:
            continue
        rows.append(
            [
                r.id,
                r.observed_at.isoformat(),
                r.controller_seen_at.isoformat() if r.controller_seen_at else "",
                r.mac,
                r.device_label,
                r.ssid,
                int(r.is_connected),
                r.signal_dbm if r.signal_dbm is not None else "",
            ]
        )
    return _csv_bytes(
        [
            "id",
            "observed_at_utc",
            "controller_seen_at_utc",
            "mac",
            "device_label",
            "ssid",
            "is_connected",
            "signal_dbm",
        ],
        rows,
    )


def _sessions_csv(db: Session, fy_start: date, fy_end: date) -> bytes:
    rows: list[list[object]] = []
    for s in db.execute(
        select(WorkSession)
        .where(WorkSession.local_date >= fy_start.isoformat())
        .where(WorkSession.local_date <= fy_end.isoformat())
        .order_by(WorkSession.local_date.asc(), WorkSession.started_at.asc())
    ).scalars():
        rows.append(
            [
                s.id,
                s.local_date,
                s.started_at.isoformat(),
                s.ended_at.isoformat(),
                s.duration_seconds,
                s.devices_seen,
                s.bridged_gaps_count,
                s.bridged_gaps_seconds,
                s.rule_version,
            ]
        )
    return _csv_bytes(
        [
            "id",
            "local_date",
            "started_at_utc",
            "ended_at_utc",
            "duration_seconds",
            "devices_seen",
            "bridged_gaps_count",
            "bridged_gaps_seconds",
            "rule_version",
        ],
        rows,
    )


def _summaries_csv(db: Session, fy_start: date, fy_end: date) -> bytes:
    """ALL versions — the never-overwritten audit trail, in full."""
    rows: list[list[object]] = []
    for r in db.execute(
        select(DailySummary)
        .where(DailySummary.local_date >= fy_start.isoformat())
        .where(DailySummary.local_date <= fy_end.isoformat())
        .order_by(DailySummary.local_date.asc(), DailySummary.version.asc())
    ).scalars():
        rows.append(
            [
                r.id,
                r.local_date,
                r.version,
                r.computed_seconds,
                r.adjustment_seconds,
                r.adjustment_reason or "",
                r.claimed_seconds,
                int(r.locked),
                r.locked_at.isoformat() if r.locked_at else "",
                r.created_at.isoformat(),
                r.created_by,
                r.rule_version,
            ]
        )
    return _csv_bytes(
        [
            "id",
            "local_date",
            "version",
            "computed_seconds",
            "adjustment_seconds",
            "adjustment_reason",
            "claimed_seconds",
            "locked",
            "locked_at_utc",
            "created_at_utc",
            "created_by",
            "rule_version",
        ],
        rows,
    )


def write_bundle(
    db: Session,
    fy_start: date,
    fy_end: date,
    fy_label: str,
    out: BytesIO,
) -> dict[str, object]:
    """Write the audit bundle zip into ``out``. Returns the manifest dict."""
    cfg = get_config(db)

    # XLSX via the existing Phase 5 exporter.
    xlsx_buf = BytesIO()
    write_xlsx(db, fy_start, fy_end, fy_label, xlsx_buf)
    xlsx_bytes = xlsx_buf.getvalue()

    # Methodology with config substitution (same path as the XLSX sheet).
    try:
        methodology_template = METHODOLOGY_MD_PATH.read_text(encoding="utf-8")
    except OSError:
        logger.warning("methodology template missing at %s", METHODOLOGY_MD_PATH)
        methodology_template = "(methodology template not available at export time)"
    methodology_bytes = render_methodology_with_config(methodology_template, cfg, fy_label).encode(
        "utf-8"
    )

    observations_bytes = _observations_csv(db, fy_start, fy_end, cfg.local_timezone)
    sessions_bytes = _sessions_csv(db, fy_start, fy_end)
    summaries_bytes = _summaries_csv(db, fy_start, fy_end)

    def _row_count(b: bytes) -> int:
        # header excluded
        return max(0, b.decode("utf-8").count("\n") - 1)

    files: dict[str, bytes] = {
        f"wfh-logbook-{fy_label}.xlsx": xlsx_bytes,
        "methodology.md": methodology_bytes,
        "observations.csv": observations_bytes,
        "sessions.csv": sessions_bytes,
        "daily_summaries.csv": summaries_bytes,
    }

    manifest: dict[str, object] = {
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "app_version": __version__,
        "financial_year": fy_label,
        "fy_start": fy_start.isoformat(),
        "fy_end": fy_end.isoformat(),
        "rule_version": cfg.rule_version,
        "config_snapshot": {
            "work_ssid": cfg.work_ssid,
            "gap_bridge_minutes": cfg.gap_bridge_minutes,
            "min_session_minutes": cfg.min_session_minutes,
            "daily_cap_hours": cfg.daily_cap_hours,
            "local_timezone": cfg.local_timezone,
        },
        "files": {
            name: {
                "sha256": _sha256(data),
                "bytes": len(data),
                **({"rows": _row_count(data)} if name.endswith(".csv") else {}),
            }
            for name, data in files.items()
        },
    }

    with ZipFile(out, "w", ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))

    logger.info(
        "bundle export: fy=%s files=%d total_bytes=%d",
        fy_label,
        len(files) + 1,
        sum(len(d) for d in files.values()),
    )
    return manifest
