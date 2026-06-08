"""HTTP-level tests for /api/days/* and the web UI.

The service-layer logic is covered in test_versioning + test_sessionisation;
here we verify the routes plumb the same logic through FastAPI correctly,
return the expected JSON shapes / HTML, and reject malformed requests.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Observation
from app.sessions.persistence import sessionise_date
from app.sessions.rules import RuleSet


def utc(y: int, m: int, d: int, hh: int = 0, mm: int = 0, ss: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, ss, tzinfo=UTC)


@pytest.fixture
def seeded_client(client: TestClient, db_session: Session) -> TestClient:
    """A client with a day-of-observations and sessioniser run for 2026-05-20."""
    rules = RuleSet.from_db(db_session)
    for ts, conn in [
        (utc(2026, 5, 20, 9, 0), True),
        (utc(2026, 5, 20, 12, 0), False),
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
    sessionise_date(db_session, date(2026, 5, 20), rules)
    db_session.commit()
    return client


class TestGetDays:
    def test_range_returns_per_date_items(self, seeded_client: TestClient) -> None:
        resp = seeded_client.get("/api/days", params={"from": "2026-05-18", "to": "2026-05-22"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["from"] == "2026-05-18"
        assert body["to"] == "2026-05-22"
        assert len(body["days"]) == 5
        seeded = next(d for d in body["days"] if d["local_date"] == "2026-05-20")
        assert seeded["latest"]["computed_seconds"] == 3 * 3600
        unseeded = next(d for d in body["days"] if d["local_date"] == "2026-05-19")
        assert unseeded["latest"] is None

    def test_bad_range_returns_400(self, seeded_client: TestClient) -> None:
        resp = seeded_client.get("/api/days", params={"from": "2026-05-22", "to": "2026-05-18"})
        assert resp.status_code == 400


class TestGetDayDetail:
    def test_returns_versions_and_sessions(self, seeded_client: TestClient) -> None:
        resp = seeded_client.get("/api/days/2026-05-20")
        assert resp.status_code == 200
        body = resp.json()
        assert body["local_date"] == "2026-05-20"
        assert body["latest"]["version"] == 1
        assert len(body["versions"]) == 1
        assert len(body["sessions"]) == 1
        assert body["sessions"][0]["duration_seconds"] == 3 * 3600


class TestAdjustEndpoint:
    def test_adjust_creates_version_2(self, seeded_client: TestClient) -> None:
        resp = seeded_client.post(
            "/api/days/2026-05-20/adjust",
            json={"adjustment_seconds": -45 * 60, "reason": "lunch"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["latest"]["version"] == 2
        assert body["latest"]["adjustment_seconds"] == -45 * 60
        assert body["latest"]["claimed_seconds"] == 3 * 3600 - 45 * 60
        assert body["latest"]["locked"] is False

    def test_missing_reason_rejected(self, seeded_client: TestClient) -> None:
        resp = seeded_client.post(
            "/api/days/2026-05-20/adjust",
            json={"adjustment_seconds": -30 * 60, "reason": ""},
        )
        assert resp.status_code in (400, 422)


class TestLockEndpoint:
    def test_lock_sets_locked(self, seeded_client: TestClient) -> None:
        resp = seeded_client.post("/api/days/2026-05-20/lock")
        assert resp.status_code == 200
        assert resp.json()["latest"]["locked"] is True

    def test_lock_then_adjust_creates_unlocked_v2(self, seeded_client: TestClient) -> None:
        seeded_client.post("/api/days/2026-05-20/lock")
        resp = seeded_client.post(
            "/api/days/2026-05-20/adjust",
            json={"adjustment_seconds": -30 * 60, "reason": "correction"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["latest"]["version"] == 2
        assert body["latest"]["locked"] is False


class TestResessioniseEndpoint:
    def test_idempotent_returns_changed_false(self, seeded_client: TestClient) -> None:
        resp = seeded_client.post("/api/days/2026-05-20/resessionise")
        assert resp.status_code == 200
        body = resp.json()
        assert body["sessions_built"] == 1
        assert body["daily_summary_changed"] is False


class TestWebRoutes:
    def test_review_page_renders(self, seeded_client: TestClient) -> None:
        resp = seeded_client.get("/")
        assert resp.status_code == 200
        # base.html includes header and our vendored static asset.
        assert "WFH Logbook" in resp.text
        assert "htmx.min.js" in resp.text

    def test_calendar_page_renders(self, seeded_client: TestClient) -> None:
        resp = seeded_client.get("/calendar")
        assert resp.status_code == 200
        assert "calendar" in resp.text.lower()

    def test_year_page_renders(self, seeded_client: TestClient) -> None:
        resp = seeded_client.get("/year/2025-26")
        assert resp.status_code == 200
        assert "2025-26" in resp.text

    def test_year_bad_label_400(self, seeded_client: TestClient) -> None:
        resp = seeded_client.get("/year/not-a-fy")
        assert resp.status_code == 400

    def test_htmx_adjust_returns_card_fragment(self, seeded_client: TestClient) -> None:
        # Form-encoded body, like an HTMX submit.
        resp = seeded_client.post(
            "/web/days/2026-05-20/adjust",
            data={"minutes": "-45", "reason": "lunch"},
        )
        assert resp.status_code == 200
        # Fragment, not a full page (no <html> tag).
        assert "<!DOCTYPE" not in resp.text
        assert 'id="day-2026-05-20"' in resp.text

    def test_static_htmx_served(self, seeded_client: TestClient) -> None:
        resp = seeded_client.get("/static/htmx.min.js")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/javascript") or resp.headers[
            "content-type"
        ].startswith("text/javascript")
        assert len(resp.content) > 10_000  # real vendored HTMX, not a placeholder.
