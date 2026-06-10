"""Audit bundle export (HANDOFF §6 Phase 8.B).

Acceptance: the zip opens, every CSV row count matches the manifest, and
every SHA-256 in the manifest matches the file bytes.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from io import BytesIO
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.days_service import AdjustParams, adjust_day, lock_day
from app.exporters.bundle import write_bundle
from app.models import Observation
from app.sessions.persistence import sessionise_date
from app.sessions.rules import RuleSet


def utc(y: int, m: int, d: int, hh: int = 0, mm: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=UTC)


@pytest.fixture
def seeded_fy(db_session: Session) -> Session:
    """One day in FY 2025-26 with an adjustment + lock (two summary versions)."""
    from app.config import get_settings
    from app.main import seed_config_if_missing

    seed_config_if_missing(db_session, get_settings())
    db_session.commit()
    rules = RuleSet.from_db(db_session)

    # 09:00-12:00 AEST on 2025-08-15.
    for ts, conn in [(utc(2025, 8, 14, 23, 0), True), (utc(2025, 8, 15, 2, 0), False)]:
        db_session.add(
            Observation(
                observed_at=ts,
                controller_seen_at=ts,
                mac="a",
                device_label="iPhone",
                ssid="WFH-TEST",
                is_connected=conn,
                signal_dbm=-60,
                raw_json="{}",
            )
        )
    db_session.commit()
    sessionise_date(db_session, date(2025, 8, 15), rules)
    db_session.commit()
    adjust_day(
        db_session, date(2025, 8, 15), AdjustParams(adjustment_seconds=-30 * 60, reason="lunch")
    )
    db_session.commit()
    lock_day(db_session, date(2025, 8, 15))
    db_session.commit()
    return db_session


EXPECTED_NAMES = {
    "wfh-logbook-2025-26.xlsx",
    "methodology.md",
    "observations.csv",
    "sessions.csv",
    "daily_summaries.csv",
    "manifest.json",
}


class TestBundleContents:
    def test_zip_contains_expected_files(self, seeded_fy: Session) -> None:
        buf = BytesIO()
        write_bundle(seeded_fy, date(2025, 7, 1), date(2026, 6, 30), "2025-26", buf)
        buf.seek(0)
        with ZipFile(buf) as zf:
            assert set(zf.namelist()) == EXPECTED_NAMES

    def test_manifest_hashes_match_file_bytes(self, seeded_fy: Session) -> None:
        buf = BytesIO()
        write_bundle(seeded_fy, date(2025, 7, 1), date(2026, 6, 30), "2025-26", buf)
        buf.seek(0)
        with ZipFile(buf) as zf:
            manifest = json.loads(zf.read("manifest.json"))
            for name, meta in manifest["files"].items():
                data = zf.read(name)
                assert hashlib.sha256(data).hexdigest() == meta["sha256"], name
                assert len(data) == meta["bytes"], name

    def test_manifest_row_counts_match_csvs(self, seeded_fy: Session) -> None:
        buf = BytesIO()
        write_bundle(seeded_fy, date(2025, 7, 1), date(2026, 6, 30), "2025-26", buf)
        buf.seek(0)
        with ZipFile(buf) as zf:
            manifest = json.loads(zf.read("manifest.json"))
            for name in ("observations.csv", "sessions.csv", "daily_summaries.csv"):
                lines = zf.read(name).decode("utf-8").strip().splitlines()
                assert len(lines) - 1 == manifest["files"][name]["rows"], name

    def test_all_summary_versions_present(self, seeded_fy: Session) -> None:
        """The audit trail: v1 (sessioniser) AND v2 (adjustment) both in CSV."""
        buf = BytesIO()
        write_bundle(seeded_fy, date(2025, 7, 1), date(2026, 6, 30), "2025-26", buf)
        buf.seek(0)
        with ZipFile(buf) as zf:
            lines = zf.read("daily_summaries.csv").decode("utf-8").strip().splitlines()
        assert len(lines) == 3  # header + v1 + v2
        assert ",1," in lines[1] and ",2," in lines[2]

    def test_methodology_is_populated(self, seeded_fy: Session) -> None:
        buf = BytesIO()
        write_bundle(seeded_fy, date(2025, 7, 1), date(2026, 6, 30), "2025-26", buf)
        buf.seek(0)
        with ZipFile(buf) as zf:
            text = zf.read("methodology.md").decode("utf-8")
        assert "WFH-TEST" in text
        assert "[WORK_SSID]" not in text

    def test_manifest_carries_config_and_rule_version(self, seeded_fy: Session) -> None:
        buf = BytesIO()
        manifest = write_bundle(seeded_fy, date(2025, 7, 1), date(2026, 6, 30), "2025-26", buf)
        assert manifest["rule_version"] == "2026.1"
        snapshot = manifest["config_snapshot"]
        assert snapshot["work_ssid"] == "WFH-TEST"  # type: ignore[index]
        assert snapshot["gap_bridge_minutes"] == 10  # type: ignore[index]


class TestBundleEndpoint:
    def test_endpoint_returns_zip(self, seeded_fy: Session, client: TestClient) -> None:
        resp = client.get("/api/export.bundle", params={"fy": "2025-26"})
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/zip"
        assert resp.content[:2] == b"PK"
        with ZipFile(BytesIO(resp.content)) as zf:
            assert "manifest.json" in zf.namelist()

    def test_bad_fy_400(self, client: TestClient) -> None:
        resp = client.get("/api/export.bundle", params={"fy": "nope"})
        assert resp.status_code == 400
