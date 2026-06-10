"""Bot tables (HANDOFF Phase 7.D, ARCHITECTURE §4.5-§4.7).

Creates: bot_chats, bot_state, bot_messages (+ unique partial index on
telegram_update_id and append-only triggers mirroring observations).

`rejection_sent` on bot_chats is an addition over the original 7.D table
sketch: 7.E requires exactly ONE polite rejection for unauthorised users,
which needs durable state to survive restarts. Spec updated alongside.

Revision ID: 0002_bot_tables
Revises: 0001_initial
Create Date: 2026-06-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_bot_tables"
down_revision: str | Sequence[str] | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "bot_chats",
        sa.Column("chat_id", sa.Integer, primary_key=True, autoincrement=False),
        sa.Column("telegram_user_id", sa.Integer, nullable=False),
        sa.Column("authorised", sa.Integer, nullable=False, server_default="0"),
        sa.Column("rejection_sent", sa.Integer, nullable=False, server_default="0"),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "bot_state",
        sa.Column(
            "chat_id",
            sa.Integer,
            sa.ForeignKey("bot_chats.chat_id"),
            primary_key=True,
            autoincrement=False,
        ),
        sa.Column("awaiting", sa.String(32), nullable=True),
        sa.Column("awaiting_date", sa.String(10), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "bot_messages",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("chat_id", sa.Integer, nullable=False),
        sa.Column("direction", sa.String(3), nullable=False),
        sa.Column("telegram_update_id", sa.Integer, nullable=True),
        sa.Column("telegram_message_id", sa.Integer, nullable=True),
        sa.Column("text", sa.Text, nullable=True),
        sa.Column("raw_json", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("direction IN ('in','out')", name="ck_bot_messages_direction"),
    )
    # Partial unique index: idempotency on inbound update ids (HANDOFF 7.C).
    op.execute(
        """
        CREATE UNIQUE INDEX ux_bot_messages_update_id
        ON bot_messages(telegram_update_id)
        WHERE telegram_update_id IS NOT NULL
        """
    )

    # Append-only triggers, mirroring observations (ARCHITECTURE §4.7).
    op.execute(
        """
        CREATE TRIGGER trg_bot_messages_no_update
        BEFORE UPDATE ON bot_messages
        BEGIN SELECT RAISE(ABORT, 'bot_messages is append-only'); END;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_bot_messages_no_delete
        BEFORE DELETE ON bot_messages
        BEGIN SELECT RAISE(ABORT, 'bot_messages is append-only'); END;
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_bot_messages_no_update")
    op.execute("DROP TRIGGER IF EXISTS trg_bot_messages_no_delete")
    op.execute("DROP INDEX IF EXISTS ux_bot_messages_update_id")
    op.drop_table("bot_messages")
    op.drop_table("bot_state")
    op.drop_table("bot_chats")
