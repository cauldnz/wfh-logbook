"""ORM models per docs/ARCHITECTURE.md §4.

Immutability of ``observations`` (and later ``bot_messages``) is enforced both
by SQL triggers (see db.py) and by SQLAlchemy mapper events here; either layer
will block a stray UPDATE/DELETE.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy import (
    text as text_sql,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Mapper,
    mapped_column,
)


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


# ----------------------------------------------------------- observations
class Observation(Base):
    """One row per poll-cycle observation of a tracked device.

    Append-only. See ARCHITECTURE §4.1.
    """

    __tablename__ = "observations"

    id: Mapped[int] = mapped_column(primary_key=True)
    # ISO-8601 UTC. Stored timezone-aware; renderers convert to local.
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    controller_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    mac: Mapped[str] = mapped_column(String(32), nullable=False)
    device_label: Mapped[str] = mapped_column(String(128), nullable=False)
    ssid: Mapped[str] = mapped_column(String(64), nullable=False)
    # SQLAlchemy stores Python bool → SQLite INTEGER 0/1 transparently.
    is_connected: Mapped[bool] = mapped_column(Integer, nullable=False)
    signal_dbm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("ix_observations_mac_observed_at", "mac", "observed_at"),
        Index("ix_observations_observed_at", "observed_at"),
        CheckConstraint("is_connected IN (0, 1)", name="ck_observations_is_connected_bool"),
    )


# ---------------------------------------------------------------- sessions
class WorkSession(Base):
    """A contiguous work period after sessionisation. See ARCHITECTURE §4.2.

    Regeneratable from observations. The sessioniser deletes and rewrites rows
    for a given ``local_date`` on each run.
    """

    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    local_date: Mapped[str] = mapped_column(String(10), nullable=False)  # YYYY-MM-DD
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    devices_seen: Mapped[str] = mapped_column(String(512), nullable=False)
    bridged_gaps_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    bridged_gaps_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    rule_version: Mapped[str] = mapped_column(String(32), nullable=False)

    __table_args__ = (Index("ix_sessions_local_date", "local_date"),)


# --------------------------------------------------------- daily_summaries
class DailySummary(Base):
    """Per-day per-version summary; the most recent unlocked row is "current".

    See ARCHITECTURE §4.3, §5.5.
    """

    __tablename__ = "daily_summaries"

    id: Mapped[int] = mapped_column(primary_key=True)
    local_date: Mapped[str] = mapped_column(String(10), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    computed_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    adjustment_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    adjustment_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    claimed_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    locked: Mapped[bool] = mapped_column(Integer, nullable=False, default=0)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[str] = mapped_column(String(32), nullable=False)
    rule_version: Mapped[str] = mapped_column(String(32), nullable=False)

    __table_args__ = (
        UniqueConstraint("local_date", "version", name="ux_daily_summaries_date_version"),
        Index("ix_daily_summaries_local_date", "local_date"),
        CheckConstraint("claimed_seconds >= 0", name="ck_daily_summaries_claimed_nonneg"),
        CheckConstraint("locked IN (0, 1)", name="ck_daily_summaries_locked_bool"),
        CheckConstraint(
            "created_by IN ('sessioniser', 'web', 'telegram')",
            name="ck_daily_summaries_created_by",
        ),
    )


# ----------------------------------------------------------------- config
class Config(Base):
    """Single-row table holding canonical sessionisation parameters.

    See ARCHITECTURE §4.4. Initial values are seeded from `.env` on first start;
    thereafter the DB is the source of truth.
    """

    __tablename__ = "config"

    id: Mapped[int] = mapped_column(primary_key=True)
    work_ssid: Mapped[str] = mapped_column(String(64), nullable=False)
    gap_bridge_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    min_session_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    daily_cap_hours: Mapped[int] = mapped_column(Integer, nullable=False)
    local_timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    rule_version: Mapped[str] = mapped_column(String(32), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# ----------------------------------------------------------------- devices
class Device(Base):
    """Tracked devices. iOS per-SSID MAC may rotate rarely; rotate via
    end-dating + insert. See ARCHITECTURE §4.4 (penultimate paragraph).
    """

    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(primary_key=True)
    mac: Mapped[str] = mapped_column(String(32), nullable=False)
    label: Mapped[str] = mapped_column(String(128), nullable=False)
    active_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    active_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("ix_devices_mac", "mac"),)


# ----------------------------------------------------- meta: poller health
class PollerState(Base):
    """Singleton row tracking poller telemetry surfaced by /api/health."""

    __tablename__ = "poller_state"

    id: Mapped[int] = mapped_column(primary_key=True)
    last_poll_attempted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_poll_succeeded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_sessioniser_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_backup_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# --------------------------------------------------------- bot (Phase 7)
class BotChat(Base):
    """One row per Telegram chat the bot has interacted with (ARCH §4.5)."""

    __tablename__ = "bot_chats"

    chat_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    telegram_user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    authorised: Mapped[bool] = mapped_column(Integer, nullable=False, default=0)
    # Whether the one-time polite rejection has been sent (HANDOFF 7.E).
    rejection_sent: Mapped[bool] = mapped_column(Integer, nullable=False, default=0)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class BotState(Base):
    """Per-chat conversation state (ARCH §4.6). Small, best-effort."""

    __tablename__ = "bot_state"

    chat_id: Mapped[int] = mapped_column(
        ForeignKey("bot_chats.chat_id"), primary_key=True, autoincrement=False
    )
    awaiting: Mapped[str | None] = mapped_column(String(32), nullable=True)
    awaiting_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class BotMessage(Base):
    """Append-only audit log of all bot traffic (ARCH §4.7).

    Same immutability rule as observations: no UPDATE, no DELETE, ever.
    """

    __tablename__ = "bot_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(Integer, nullable=False)
    direction: Mapped[str] = mapped_column(String(3), nullable=False)
    telegram_update_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    telegram_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("direction IN ('in','out')", name="ck_bot_messages_direction"),
        Index(
            "ux_bot_messages_update_id",
            "telegram_update_id",
            unique=True,
            sqlite_where=text_sql("telegram_update_id IS NOT NULL"),
        ),
    )


# --------------------------------------------------- ORM-level immutability
class ImmutableTableError(RuntimeError):
    """Raised when an ORM operation would mutate an append-only table."""


def _block_mutation(_mapper: Mapper[Any], _connection: Any, target: Any) -> None:
    raise ImmutableTableError(
        f"{type(target).__name__} rows are append-only; "
        "see ARCHITECTURE.md §4.1 and CLAUDE.md 'What Not To Do'."
    )


# Wire ORM-level guards to the append-only models. The SQL triggers in db.py
# are belt-and-braces in case raw SQL bypasses the ORM.
event.listen(Observation, "before_update", _block_mutation, propagate=False)
event.listen(Observation, "before_delete", _block_mutation, propagate=False)
event.listen(BotMessage, "before_update", _block_mutation, propagate=False)
event.listen(BotMessage, "before_delete", _block_mutation, propagate=False)
