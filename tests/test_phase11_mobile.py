"""Phase 11 mobile/iOS UI scaffolding (HANDOFF §6 Phase 11.B).

Structural assertions only — visual correctness is verified at mobile width /
on a real device, not here.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

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


class TestMobileScaffolding:
    def test_base_has_ios_meta(self, client: TestClient) -> None:
        resp = client.get("/")
        assert resp.status_code == 200
        assert "viewport-fit=cover" in resp.text
        assert 'name="apple-mobile-web-app-capable"' in resp.text
        assert 'name="theme-color"' in resp.text

    def test_review_queue_table_stacks(self, client: TestClient, db_session: Session) -> None:
        _summary(db_session, "2025-08-01", 5, locked=False)
        db_session.commit()
        resp = client.get("/review-queue")
        assert 'class="stack"' in resp.text
        assert 'data-label="Date"' in resp.text
        assert 'data-label="Claimed"' in resp.text

    def test_stylesheet_has_responsive_block(self, client: TestClient) -> None:
        resp = client.get("/static/styles.css")
        assert resp.status_code == 200
        assert "@media (max-width: 640px)" in resp.text
        assert "table.stack" in resp.text
        assert "env(safe-area-inset" in resp.text
