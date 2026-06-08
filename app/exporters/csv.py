"""CSV exporter — Summary-sheet content only, no methodology.

Suitable for ad-hoc analysis. The XLSX is the canonical tax-filing artefact.
"""

from __future__ import annotations

import csv
import logging
from datetime import date
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING

from app.exporters.common import build_summary_rows

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

CSV_HEADERS = [
    "date",
    "day_of_week",
    "computed_hours",
    "adjustment_hours",
    "adjustment_reason",
    "claimed_hours",
    "version",
    "locked",
    "locked_at",
    "rule_version",
    "recorded_via",
]


def write_csv(
    db: Session,
    from_date: date,
    to_date: date,
    out: Path | StringIO,
    tz_name: str,
) -> int:
    """Write a CSV. Returns the number of data rows written."""
    rows = build_summary_rows(db, from_date, to_date, tz_name)

    def _do_write(stream: object) -> None:
        # csv.writer expects a text-mode file-like. The caller provides one.
        w = csv.writer(stream)  # type: ignore[arg-type]
        w.writerow(CSV_HEADERS)
        for r in rows:
            w.writerow(
                [
                    r.local_date.isoformat(),
                    r.day_of_week,
                    f"{r.computed_hours:.4f}",
                    f"{r.adjustment_hours:.4f}",
                    r.adjustment_reason,
                    f"{r.claimed_hours:.4f}",
                    r.version,
                    "Yes" if r.locked else "No",
                    r.locked_at.isoformat() if r.locked_at else "",
                    r.rule_version,
                    r.created_by,
                ]
            )

    if isinstance(out, StringIO):
        _do_write(out)
    else:
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", newline="", encoding="utf-8") as f:
            _do_write(f)
    logger.info(
        "csv export: from=%s to=%s rows=%d out=%s",
        from_date,
        to_date,
        len(rows),
        out,
    )
    return len(rows)
