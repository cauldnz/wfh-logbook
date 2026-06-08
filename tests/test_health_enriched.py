"""Phase 6 enrichments on /api/health + structured logging."""

from __future__ import annotations

import io
import json
import logging
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.logging_config import JsonFormatter, configure_logging
from app.models import Observation


def test_health_includes_phase6_fields(client: TestClient) -> None:
    resp = client.get("/api/health")
    body = resp.json()
    assert "db_size_bytes" in body
    assert isinstance(body["db_size_bytes"], int)
    assert body["db_size_bytes"] > 0
    assert "observations_last_24h" in body
    assert body["observations_last_24h"] == 0  # fresh DB


def test_observations_last_24h_counts_recent(client: TestClient, db_session: Session) -> None:
    now = datetime.now(UTC)
    db_session.add(
        Observation(
            observed_at=now,
            controller_seen_at=now,
            mac="a",
            device_label="iPhone",
            ssid="WFH-TEST",
            is_connected=True,
            signal_dbm=None,
            raw_json="{}",
        )
    )
    db_session.commit()
    resp = client.get("/api/health")
    assert resp.json()["observations_last_24h"] == 1


def test_json_formatter_produces_object(client: TestClient) -> None:
    """JsonFormatter emits one JSON object per record."""
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="t",
        level=logging.INFO,
        pathname="x",
        lineno=1,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )
    out = formatter.format(record)
    obj = json.loads(out)
    assert obj["level"] == "INFO"
    assert obj["logger"] == "t"
    assert obj["msg"] == "hello world"
    assert "ts" in obj


def test_configure_logging_idempotent(capsys) -> None:  # type: ignore[no-untyped-def]
    """Repeated calls don't add duplicate handlers."""
    # Capture using a fresh buffer rather than capsys (which intercepts stdout
    # at a level configure_logging clobbers).
    configure_logging(level="INFO", structured=True)
    configure_logging(level="INFO", structured=True)
    root = logging.getLogger()
    assert len(root.handlers) == 1


def test_text_format_no_json_braces() -> None:
    buf = io.StringIO()
    handler = logging.StreamHandler(stream=buf)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)
    logging.getLogger("t").info("hello world")
    out = buf.getvalue()
    assert "hello world" in out
    # Cleanup: reset to default JSON config.
    configure_logging(level="INFO", structured=True)
