"""Backups API + restore procedure (HANDOFF §6 Phase 8.D)."""

from __future__ import annotations

import shutil
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app import db as db_mod
from app.config import get_settings
from app.models import Observation


def _add_observation(client_db) -> None:  # type: ignore[no-untyped-def]
    client_db.add(
        Observation(
            observed_at=datetime(2026, 6, 9, 9, 0, tzinfo=UTC),
            controller_seen_at=datetime(2026, 6, 9, 9, 0, tzinfo=UTC),
            mac="a",
            device_label="iPhone",
            ssid="WFH-TEST",
            is_connected=True,
            signal_dbm=-60,
            raw_json="{}",
        )
    )
    client_db.commit()


class TestBackupEndpoints:
    def test_backup_now_creates_listed_snapshot(self, client: TestClient, db_session) -> None:  # type: ignore[no-untyped-def]
        resp = client.post("/api/backup")
        assert resp.status_code == 200
        name = resp.json()["name"]
        assert name.startswith("wfh-logbook-") and name.endswith(".sqlite")

        listing = client.get("/api/backups")
        assert listing.status_code == 200
        names = [s["name"] for s in listing.json()["snapshots"]]
        assert name in names

    def test_download_snapshot(self, client: TestClient, db_session) -> None:  # type: ignore[no-untyped-def]
        name = client.post("/api/backup").json()["name"]
        resp = client.get(f"/api/backups/{name}")
        assert resp.status_code == 200
        assert resp.content.startswith(b"SQLite format 3")

    @pytest.mark.parametrize(
        "bad_name",
        [
            "../wfh-logbook.sqlite",
            "..%2Fwfh-logbook.sqlite",
            "wfh-logbook-20260101.sqlite.evil",
            "notasnapshot.sqlite",
            "wfh-logbook-2026.sqlite",
        ],
    )
    def test_traversal_and_malformed_names_rejected(
        self, client: TestClient, bad_name: str
    ) -> None:
        resp = client.get(f"/api/backups/{bad_name}")
        assert resp.status_code == 404

    def test_empty_dir_lists_nothing(self, client: TestClient) -> None:
        resp = client.get("/api/backups")
        assert resp.status_code == 200
        assert resp.json()["snapshots"] == []

    def test_system_page_renders_with_backup_button(self, client: TestClient, db_session) -> None:  # type: ignore[no-untyped-def]
        resp = client.get("/system")
        assert resp.status_code == 200
        assert "Back up now" in resp.text

    def test_system_backup_post_redirects(self, client: TestClient, db_session) -> None:  # type: ignore[no-untyped-def]
        resp = client.post("/system/backup", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/system"


class TestRestoreProcedure:
    """The documented restore drill (docs/DEPLOYMENT.md §5), automated.

    Snapshot a DB containing known data, destroy the live DB, restore from
    the snapshot, verify the data is back.
    """

    def test_restore_from_snapshot(self, client: TestClient, db_session) -> None:  # type: ignore[no-untyped-def]
        settings = get_settings()
        db_path = settings.db_path()

        # 1. Known data + snapshot.
        _add_observation(db_session)
        name = client.post("/api/backup").json()["name"]
        snapshot_path = settings.data_dir / "backups" / name

        # 2. Stop the app (dispose engine = container stop) and lose the DB.
        db_session.close()
        db_mod.reset_engine_cache()
        db_path.unlink()
        for sidecar in (f"{db_path}-wal", f"{db_path}-shm"):
            p = db_path.parent / sidecar.split("\\")[-1].split("/")[-1]
            if p.exists():
                p.unlink()

        # 3. Restore per the documented procedure.
        shutil.copyfile(snapshot_path, db_path)

        # 4. Start again and verify the evidence survived.
        engine = db_mod.init_engine(settings)
        with engine.connect() as conn:
            count = conn.execute(text("SELECT COUNT(*) FROM observations")).scalar_one()
            integrity = conn.execute(text("PRAGMA integrity_check")).scalar_one()
        assert count == 1
        assert integrity == "ok"
