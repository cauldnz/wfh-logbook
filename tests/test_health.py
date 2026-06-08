"""Phase 1 acceptance: /api/health returns 200 on a fresh DB."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_health_returns_200(client: TestClient) -> None:
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["db_ok"] is True
    # rule_version is seeded from .env on first startup.
    assert body["rule_version"] == "2026.1"
    assert body["consecutive_failures"] == 0
    # Phase 1 hasn't polled yet.
    assert body["last_poll_succeeded_at"] is None
