"""Append-only enforcement for `observations` (and any future `bot_messages`).

Per CLAUDE.md "Immutability is testable": both the SQL trigger AND the
SQLAlchemy ORM event must block UPDATE/DELETE. We verify each independently.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.models import ImmutableTableError, Observation


def _make_observation() -> Observation:
    return Observation(
        observed_at=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
        controller_seen_at=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
        mac="aa:bb:cc:dd:ee:ff",
        device_label="iPhone",
        ssid="WFH-TEST",
        is_connected=True,
        signal_dbm=-58,
        raw_json=json.dumps({"mac": "aa:bb:cc:dd:ee:ff"}),
    )


def test_orm_update_observation_raises(db_session: Session) -> None:
    """Updating an Observation via the ORM raises ImmutableTableError."""
    obs = _make_observation()
    db_session.add(obs)
    db_session.commit()

    obs.device_label = "Renamed-by-typo"
    with pytest.raises(ImmutableTableError):
        db_session.commit()
    db_session.rollback()


def test_orm_delete_observation_raises(db_session: Session) -> None:
    """Deleting an Observation via the ORM raises ImmutableTableError."""
    obs = _make_observation()
    db_session.add(obs)
    db_session.commit()

    db_session.delete(obs)
    with pytest.raises(ImmutableTableError):
        db_session.commit()
    db_session.rollback()


def test_raw_sql_update_blocked_by_trigger(db_session: Session) -> None:
    """Bypass the ORM and try raw SQL — the SQL trigger blocks it."""
    obs = _make_observation()
    db_session.add(obs)
    db_session.commit()

    with pytest.raises((OperationalError, IntegrityError)) as excinfo:
        db_session.execute(
            text("UPDATE observations SET device_label = :l WHERE id = :id"),
            {"l": "raw-sql-tamper", "id": obs.id},
        )
        db_session.commit()
    assert "append-only" in str(excinfo.value).lower()
    db_session.rollback()


def test_raw_sql_delete_blocked_by_trigger(db_session: Session) -> None:
    """Bypass the ORM and try raw DELETE — the SQL trigger blocks it."""
    obs = _make_observation()
    db_session.add(obs)
    db_session.commit()

    with pytest.raises((OperationalError, IntegrityError)) as excinfo:
        db_session.execute(text("DELETE FROM observations WHERE id = :id"), {"id": obs.id})
        db_session.commit()
    assert "append-only" in str(excinfo.value).lower()
    db_session.rollback()


def test_inserts_are_allowed(db_session: Session) -> None:
    """Append-only means INSERTs continue to work."""
    for i in range(5):
        obs = _make_observation()
        obs.signal_dbm = -50 - i
        db_session.add(obs)
    db_session.commit()

    rows = db_session.execute(text("SELECT COUNT(*) FROM observations")).scalar_one()
    assert rows == 5
