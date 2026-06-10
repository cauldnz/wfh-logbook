"""Coverage for the nightly scheduler path, the sessioniser CLI, and config
parsing helpers (HANDOFF §7 coverage targets for app/sessions/).
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
import time_machine
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import DailySummary, Observation
from app.sessions.persistence import dates_needing_resessionisation, sessionise_date
from app.sessions.rules import RuleSet
from app.sessions.scheduler import run_nightly_sessioniser


def utc(y: int, m: int, d: int, hh: int = 0, mm: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=UTC)


@pytest.fixture
def rules(db_session: Session) -> RuleSet:
    from app.config import get_settings
    from app.main import seed_config_if_missing

    seed_config_if_missing(db_session, get_settings())
    db_session.commit()
    return RuleSet.from_db(db_session)


def _add_observations(db: Session, day: tuple[int, int, int], hours: tuple[int, int]) -> None:
    for ts, conn in [
        (utc(*day, hours[0], 0), True),
        (utc(*day, hours[1], 0), False),
    ]:
        db.add(
            Observation(
                observed_at=ts,
                controller_seen_at=ts,
                mac="a",
                device_label="iPhone",
                ssid="WFH-TEST",
                is_connected=conn,
                signal_dbm=None,
                raw_json="{}",
            )
        )
    db.commit()


class TestDatesNeedingResessionisation:
    """The nightly window: yesterday + non-locked dates in trailing 7 days."""

    def test_no_summaries_returns_just_yesterday(self, db_session: Session) -> None:
        today = date(2026, 6, 10)
        assert dates_needing_resessionisation(db_session, today) == [date(2026, 6, 9)]

    def test_unlocked_recent_date_included(self, db_session: Session, rules: RuleSet) -> None:
        _add_observations(db_session, (2026, 6, 5), (9, 12))
        sessionise_date(db_session, date(2026, 6, 5), rules)
        db_session.commit()
        out = dates_needing_resessionisation(db_session, date(2026, 6, 10))
        assert date(2026, 6, 5) in out
        assert date(2026, 6, 9) in out

    def test_locked_date_excluded(self, db_session: Session, rules: RuleSet) -> None:
        _add_observations(db_session, (2026, 6, 5), (9, 12))
        sessionise_date(db_session, date(2026, 6, 5), rules)
        row = db_session.execute(
            select(DailySummary).where(DailySummary.local_date == "2026-06-05")
        ).scalar_one()
        row.locked = True
        row.locked_at = utc(2026, 6, 6)
        db_session.commit()
        out = dates_needing_resessionisation(db_session, date(2026, 6, 10))
        assert date(2026, 6, 5) not in out
        assert out == [date(2026, 6, 9)]

    def test_date_outside_7_day_window_excluded(self, db_session: Session, rules: RuleSet) -> None:
        _add_observations(db_session, (2026, 5, 20), (9, 12))
        sessionise_date(db_session, date(2026, 5, 20), rules)
        db_session.commit()
        out = dates_needing_resessionisation(db_session, date(2026, 6, 10))
        assert date(2026, 5, 20) not in out


class TestNightlyJob:
    """run_nightly_sessioniser end-to-end with frozen wall-clock."""

    @time_machine.travel("2026-06-10 02:00 +0000")
    def test_nightly_run_sessionises_yesterday(self, db_session: Session, rules: RuleSet) -> None:
        # 2026-06-10 02:00 UTC = 2026-06-10 12:00 Sydney → "yesterday" is 06-09.
        # Observations at 2026-06-08 23:00 UTC = 06-09 09:00 Sydney.
        for ts, conn in [
            (utc(2026, 6, 8, 23, 0), True),
            (utc(2026, 6, 9, 2, 0), False),
        ]:
            db_session.add(
                Observation(
                    observed_at=ts,
                    controller_seen_at=ts,
                    mac="a",
                    device_label="iPhone",
                    ssid="WFH-TEST",
                    is_connected=conn,
                    signal_dbm=None,
                    raw_json="{}",
                )
            )
        db_session.commit()

        run_nightly_sessioniser("Australia/Sydney")

        db_session.expire_all()
        row = db_session.execute(
            select(DailySummary).where(DailySummary.local_date == "2026-06-09")
        ).scalar_one()
        assert row.computed_seconds == 3 * 3600
        assert row.created_by == "sessioniser"


class TestSessioniserCLI:
    """python -m app.sessions — single date and --dry-run paths."""

    def test_single_date_commits(self, db_session: Session, rules: RuleSet) -> None:
        from app.sessions.__main__ import main

        _add_observations(db_session, (2026, 6, 1), (9, 13))
        rc = main(["--date", "2026-06-01"])
        assert rc == 0
        db_session.expire_all()
        row = db_session.execute(
            select(DailySummary).where(DailySummary.local_date == "2026-06-01")
        ).scalar_one()
        assert row.computed_seconds == 4 * 3600

    def test_dry_run_rolls_back(self, db_session: Session, rules: RuleSet) -> None:
        from app.sessions.__main__ import main

        _add_observations(db_session, (2026, 6, 2), (9, 13))
        rc = main(["--date", "2026-06-02", "--dry-run"])
        assert rc == 0
        db_session.expire_all()
        rows = (
            db_session.execute(select(DailySummary).where(DailySummary.local_date == "2026-06-02"))
            .scalars()
            .all()
        )
        assert rows == []  # rolled back, nothing persisted

    @time_machine.travel("2026-06-10 02:00 +0000")
    def test_nightly_window_flag(self, db_session: Session, rules: RuleSet) -> None:
        from app.sessions.__main__ import main

        rc = main(["--nightly-window"])
        assert rc == 0  # empty DB: yesterday processed, zero sessions, no crash


class TestConfigParsing:
    """Settings helpers (config.py coverage)."""

    def test_parsed_device_macs_happy_path(self) -> None:
        s = Settings(work_device_macs="AA:BB:CC:DD:EE:FF=iPhone, 11:22:33:44:55:66=Laptop")
        assert s.parsed_device_macs() == [
            ("aa:bb:cc:dd:ee:ff", "iPhone"),
            ("11:22:33:44:55:66", "Laptop"),
        ]

    def test_parsed_device_macs_empty(self) -> None:
        assert Settings(work_device_macs="  ").parsed_device_macs() == []

    def test_parsed_device_macs_missing_label_raises(self) -> None:
        with pytest.raises(ValueError, match="MAC=Label"):
            Settings(work_device_macs="aa:bb:cc:dd:ee:ff").parsed_device_macs()

    def test_parsed_device_macs_skips_blank_chunks(self) -> None:
        s = Settings(work_device_macs="aa:bb:cc:dd:ee:ff=iPhone,,")
        assert s.parsed_device_macs() == [("aa:bb:cc:dd:ee:ff", "iPhone")]

    def test_parsed_allowed_user_ids(self) -> None:
        s = Settings(telegram_allowed_user_ids="123, 456,,789")
        assert s.parsed_allowed_user_ids() == [123, 456, 789]

    def test_parsed_allowed_user_ids_empty(self) -> None:
        assert Settings(telegram_allowed_user_ids="").parsed_allowed_user_ids() == []

    def test_db_url_points_into_data_dir(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        s = Settings(data_dir=str(tmp_path))
        assert s.db_url().startswith("sqlite:///")
        assert "wfh-logbook.sqlite" in s.db_url()


class TestExporterCLI:
    """python -m app.exporters — format inference + output."""

    def test_xlsx_export_via_cli(self, db_session: Session, rules: RuleSet, tmp_path) -> None:  # type: ignore[no-untyped-def]
        from app.exporters.__main__ import main

        _add_observations(db_session, (2025, 8, 15), (9, 12))
        sessionise_date(db_session, date(2025, 8, 15), rules)
        db_session.commit()

        out = tmp_path / "fy.xlsx"
        rc = main(["--fy", "2025-26", "--out", str(out)])
        assert rc == 0
        assert out.exists()
        assert out.read_bytes()[:2] == b"PK"

    def test_csv_export_via_cli(self, db_session: Session, rules: RuleSet, tmp_path) -> None:  # type: ignore[no-untyped-def]
        from app.exporters.__main__ import main

        out = tmp_path / "fy.csv"
        rc = main(["--fy", "2025-26", "--out", str(out)])
        assert rc == 0
        assert out.read_text(encoding="utf-8").startswith("date,")

    def test_bad_fy_label_exits(self, db_session: Session, rules: RuleSet, tmp_path) -> None:  # type: ignore[no-untyped-def]
        from app.exporters.__main__ import main

        with pytest.raises(SystemExit):
            main(["--fy", "2025-99", "--out", str(tmp_path / "x.xlsx")])
