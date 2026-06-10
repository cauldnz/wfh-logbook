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

from app.models import BotMessage, ImmutableTableError, Observation


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


# ----------------------------------------------------- bot_messages (Phase 7)


def _make_bot_message() -> BotMessage:
    return BotMessage(
        chat_id=111,
        direction="in",
        telegram_update_id=42,
        telegram_message_id=None,
        text="/start",
        raw_json=json.dumps({"update_id": 42}),
        created_at=datetime(2026, 6, 10, 12, 0, tzinfo=UTC),
    )


def test_orm_update_bot_message_raises(db_session: Session) -> None:
    msg = _make_bot_message()
    db_session.add(msg)
    db_session.commit()

    msg.text = "tampered"
    with pytest.raises(ImmutableTableError):
        db_session.commit()
    db_session.rollback()


def test_orm_delete_bot_message_raises(db_session: Session) -> None:
    msg = _make_bot_message()
    db_session.add(msg)
    db_session.commit()

    db_session.delete(msg)
    with pytest.raises(ImmutableTableError):
        db_session.commit()
    db_session.rollback()


def test_raw_sql_update_bot_message_blocked_by_trigger(db_session: Session) -> None:
    msg = _make_bot_message()
    db_session.add(msg)
    db_session.commit()

    with pytest.raises((OperationalError, IntegrityError)) as excinfo:
        db_session.execute(
            text("UPDATE bot_messages SET text = 'x' WHERE id = :id"), {"id": msg.id}
        )
        db_session.commit()
    assert "append-only" in str(excinfo.value).lower()
    db_session.rollback()


def test_duplicate_update_id_rejected(db_session: Session) -> None:
    """The partial unique index enforces inbound idempotency (HANDOFF 7.C)."""
    db_session.add(_make_bot_message())
    db_session.commit()
    db_session.add(_make_bot_message())  # same telegram_update_id=42
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_null_update_ids_do_not_collide(db_session: Session) -> None:
    """Outbound rows (update_id NULL) are exempt from the unique index."""
    from app.models import BotMessage

    for i in range(3):
        db_session.add(
            BotMessage(
                chat_id=111,
                direction="out",
                telegram_update_id=None,
                telegram_message_id=100 + i,
                text="hi",
                raw_json="{}",
                created_at=datetime(2026, 6, 10, 12, 0, tzinfo=UTC),
            )
        )
    db_session.commit()
    n = db_session.execute(
        text("SELECT COUNT(*) FROM bot_messages WHERE direction='out'")
    ).scalar_one()
    assert n == 3
