"""One real poll cycle against the live controller (opt-in, manual).

Read-only against the controller. With ``--write``, also runs one
``poll_once`` against the configured live database (running migrations
first if needed) so you can verify a real observation row lands end-to-end.

    .venv/Scripts/python tools/live_poll_check.py            # report only
    .venv/Scripts/python tools/live_poll_check.py --write    # + one DB write
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings

logger = logging.getLogger("live_poll_check")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write",
        action="store_true",
        help="run migrations + one poll_once against the live DB",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(levelname)s %(name)s %(message)s")

    settings = get_settings()
    from app.unifi.client import create_adapter

    adapter = create_adapter(settings)
    adapter.login()
    clients = adapter.list_active_clients()

    tracked = dict(settings.parsed_device_macs())
    work = [c for c in clients if c.ssid == settings.work_ssid and not c.is_wired]
    work_tracked = [c for c in work if c.mac in tracked]

    print()
    print(f"Active clients:               {len(clients)}")
    print(f"On work SSID ({settings.work_ssid!r}): {len(work)}")
    print(f"  ...and tracked:             {len(work_tracked)}")
    for c in work_tracked:
        print(
            f"    {tracked[c.mac]:10s} signal={c.signal_dbm} dBm "
            f"last_seen={c.last_seen.isoformat() if c.last_seen else '?'}"
        )
    untracked_work = [c for c in work if c.mac not in tracked]
    for c in untracked_work:
        print(f"    UNTRACKED on work SSID: {c.mac} ({c.hostname}) — add to WORK_DEVICE_MACS?")

    if args.write:
        from alembic.config import Config as AlembicConfig

        from alembic import command
        from app.db import get_sessionmaker, init_engine
        from app.main import seed_config_if_missing, seed_devices_if_missing
        from app.unifi.poller import poll_once

        repo_root = Path(__file__).resolve().parents[1]
        alembic_cfg = AlembicConfig(str(repo_root / "alembic.ini"))
        alembic_cfg.set_main_option("script_location", str(repo_root / "alembic"))
        command.upgrade(alembic_cfg, "head")

        init_engine(settings)
        SessionLocal = get_sessionmaker()  # noqa: N806
        with SessionLocal() as db:
            seed_config_if_missing(db, settings)
            seed_devices_if_missing(db, settings)
            db.commit()
            result = poll_once(db, adapter, settings.work_ssid)
            db.commit()
        print()
        print(
            f"poll_once: connected={result.connected_count} "
            f"transitions={result.disconnect_transitions} "
            f"duration={result.duration_seconds:.2f}s"
        )
        print(f"DB: {settings.db_path()}")

    adapter.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
