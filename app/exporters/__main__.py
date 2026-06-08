"""CLI: ``python -m app.exporters --fy 2025-26 --out /tmp/wfh-2025-26.xlsx``."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

from app.config import get_settings
from app.db import get_sessionmaker, init_engine
from app.exporters.csv import write_csv
from app.exporters.xlsx import write_xlsx

logger = logging.getLogger(__name__)


def _fy_bounds(fy: str) -> tuple[date, date]:
    start_year_str, end_short = fy.split("-")
    start_year = int(start_year_str)
    end_year = start_year + 1
    if int(end_short) != end_year % 100:
        raise SystemExit(f"bad FY label {fy!r}")
    return date(start_year, 7, 1), date(end_year, 6, 30)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.exporters",
        description="Export a financial year's logbook to XLSX (or CSV).",
    )
    parser.add_argument("--fy", required=True, help="AU financial year, e.g. 2025-26")
    parser.add_argument("--out", required=True, type=Path, help="output file path")
    parser.add_argument(
        "--format",
        choices=["xlsx", "csv"],
        default=None,
        help="defaults to inferring from the file extension",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )

    fmt = args.format or args.out.suffix.lstrip(".").lower()
    if fmt not in {"xlsx", "csv"}:
        parser.error(f"can't infer format from {args.out.name!r}; pass --format")

    settings = get_settings()
    init_engine(settings)
    SessionLocal = get_sessionmaker()  # noqa: N806

    fy_start, fy_end = _fy_bounds(args.fy)
    with SessionLocal() as db:
        if fmt == "xlsx":
            n = write_xlsx(db, fy_start, fy_end, args.fy, args.out)
        else:
            n = write_csv(db, fy_start, fy_end, args.out, settings.local_timezone)
    logger.info("wrote %d row(s) to %s", n, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
