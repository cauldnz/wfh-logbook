"""Phase 10 web surfaces: backlog banner (10.D), bulk-lock button (10.B web),
export guard (10.E)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import time_machine
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.days_service import _latest_summary
from app.models import DailySummary


def _summary(db: Session, local_date: str, hours: float, *, locked: bool = False) -> None:
    db.add(
        DailySummary(
            local_date=local_date,
            version=1,
            computed_seconds=int(hours * 3600),
            adjustment_seconds=0,
            adjustment_reason=None,
            claimed_seconds=int(hours * 3600),
            locked=locked,
            locked_at=None,
            created_at=datetime(2026, 6, 1, tzinfo=UTC),
            created_by="sessioniser",
            rule_version="2026.1",
        )
    )


class TestBacklogBanner:  # 10.D
    def test_banner_shows_when_unlocked_backlog(
        self, client: TestClient, db_session: Session
    ) -> None:
        _summary(db_session, "2025-08-01", 5, locked=False)
        db_session.commit()
        resp = client.get("/")
        assert resp.status_code == 200
        assert "review &amp; lock" in resp.text

    def test_banner_hidden_when_no_backlog(self, client: TestClient, db_session: Session) -> None:
        _summary(db_session, "2025-08-01", 5, locked=True)
        db_session.commit()
        resp = client.get("/")
        assert "review &amp; lock" not in resp.text


class TestLockCleanButton:  # 10.B web
    def test_review_queue_has_lockall_button(self, client: TestClient, db_session: Session) -> None:
        _summary(db_session, "2026-06-15", 5, locked=False)
        db_session.commit()
        resp = client.get("/review-queue")
        assert "Lock all clean days" in resp.text

    def test_post_lock_clean_locks_and_redirects(
        self, client: TestClient, db_session: Session
    ) -> None:
        _summary(db_session, "2026-06-15", 5, locked=False)  # clean → lock
        _summary(db_session, "2026-06-16", 20, locked=False)  # anomalous → skip
        db_session.commit()
        with time_machine.travel(datetime(2026, 6, 25, 2, 0, tzinfo=UTC), tick=False):
            resp = client.post("/web/days/lock-clean", follow_redirects=False)
        assert resp.status_code == 303
        db_session.expire_all()
        clean = _latest_summary(db_session, date(2026, 6, 15))
        flagged = _latest_summary(db_session, date(2026, 6, 16))
        assert clean is not None and bool(clean.locked) is True
        assert flagged is not None and bool(flagged.locked) is False


class TestExportGuard:  # 10.E
    def test_xlsx_blocked_when_unlocked(self, client: TestClient, db_session: Session) -> None:
        _summary(db_session, "2025-08-01", 5, locked=False)
        db_session.commit()
        resp = client.get("/api/export.xlsx?fy=2025-26")
        assert resp.status_code == 409
        assert "unlocked" in resp.json()["detail"]

    def test_xlsx_allowed_with_flag(self, client: TestClient, db_session: Session) -> None:
        _summary(db_session, "2025-08-01", 5, locked=False)
        db_session.commit()
        resp = client.get("/api/export.xlsx?fy=2025-26&allow_unlocked=true")
        assert resp.status_code == 200
        assert "spreadsheet" in resp.headers["content-type"]

    def test_xlsx_ok_when_all_locked(self, client: TestClient, db_session: Session) -> None:
        _summary(db_session, "2025-08-01", 5, locked=True)
        db_session.commit()
        resp = client.get("/api/export.xlsx?fy=2025-26")
        assert resp.status_code == 200

    def test_bundle_blocked_when_unlocked(self, client: TestClient, db_session: Session) -> None:
        _summary(db_session, "2025-08-01", 5, locked=False)
        db_session.commit()
        resp = client.get("/api/export.bundle?fy=2025-26")
        assert resp.status_code == 409

    def test_year_page_shows_export_guard(self, client: TestClient, db_session: Session) -> None:
        _summary(db_session, "2025-08-01", 5, locked=False)
        db_session.commit()
        resp = client.get("/year/2025-26")
        assert "Export anyway" in resp.text
