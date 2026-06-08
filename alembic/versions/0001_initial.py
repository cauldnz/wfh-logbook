"""Initial schema (HANDOFF Phase 1, ARCHITECTURE §4.1-§4.4).

Creates: observations, sessions, daily_summaries, config, devices, poller_state.
Bot tables (§4.5-§4.7) are added in a later Phase 7 migration.

Revision ID: 0001_initial
Revises:
Create Date: 2026-06-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # observations (append-only) -----------------------------------------
    op.create_table(
        "observations",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("controller_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("mac", sa.String(32), nullable=False),
        sa.Column("device_label", sa.String(128), nullable=False),
        sa.Column("ssid", sa.String(64), nullable=False),
        sa.Column("is_connected", sa.Integer, nullable=False),
        sa.Column("signal_dbm", sa.Integer, nullable=True),
        sa.Column("raw_json", sa.Text, nullable=False),
        sa.CheckConstraint("is_connected IN (0, 1)", name="ck_observations_is_connected_bool"),
    )
    op.create_index("ix_observations_mac_observed_at", "observations", ["mac", "observed_at"])
    op.create_index("ix_observations_observed_at", "observations", ["observed_at"])

    # sessions -----------------------------------------------------------
    op.create_table(
        "sessions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("local_date", sa.String(10), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_seconds", sa.Integer, nullable=False),
        sa.Column("devices_seen", sa.String(512), nullable=False),
        sa.Column("bridged_gaps_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("bridged_gaps_seconds", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rule_version", sa.String(32), nullable=False),
    )
    op.create_index("ix_sessions_local_date", "sessions", ["local_date"])

    # daily_summaries ----------------------------------------------------
    op.create_table(
        "daily_summaries",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("local_date", sa.String(10), nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("computed_seconds", sa.Integer, nullable=False),
        sa.Column("adjustment_seconds", sa.Integer, nullable=False, server_default="0"),
        sa.Column("adjustment_reason", sa.Text, nullable=True),
        sa.Column("claimed_seconds", sa.Integer, nullable=False),
        sa.Column("locked", sa.Integer, nullable=False, server_default="0"),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(32), nullable=False),
        sa.Column("rule_version", sa.String(32), nullable=False),
        sa.UniqueConstraint("local_date", "version", name="ux_daily_summaries_date_version"),
        sa.CheckConstraint("claimed_seconds >= 0", name="ck_daily_summaries_claimed_nonneg"),
        sa.CheckConstraint("locked IN (0, 1)", name="ck_daily_summaries_locked_bool"),
        sa.CheckConstraint(
            "created_by IN ('sessioniser', 'web', 'telegram')",
            name="ck_daily_summaries_created_by",
        ),
    )
    op.create_index("ix_daily_summaries_local_date", "daily_summaries", ["local_date"])

    # config (singleton) -------------------------------------------------
    op.create_table(
        "config",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("work_ssid", sa.String(64), nullable=False),
        sa.Column("gap_bridge_minutes", sa.Integer, nullable=False),
        sa.Column("min_session_minutes", sa.Integer, nullable=False),
        sa.Column("daily_cap_hours", sa.Integer, nullable=False),
        sa.Column("local_timezone", sa.String(64), nullable=False),
        sa.Column("rule_version", sa.String(32), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    # devices ------------------------------------------------------------
    op.create_table(
        "devices",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("mac", sa.String(32), nullable=False),
        sa.Column("label", sa.String(128), nullable=False),
        sa.Column("active_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("active_to", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_devices_mac", "devices", ["mac"])

    # poller_state (singleton) -------------------------------------------
    op.create_table(
        "poller_state",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("last_poll_attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_poll_succeeded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consecutive_failures", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_sessioniser_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_backup_at", sa.DateTime(timezone=True), nullable=True),
    )

    # Append-only triggers (belt-and-braces with ORM hooks) --------------
    op.execute(
        """
        CREATE TRIGGER trg_observations_no_update
        BEFORE UPDATE ON observations
        BEGIN SELECT RAISE(ABORT, 'observations are append-only'); END;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_observations_no_delete
        BEFORE DELETE ON observations
        BEGIN SELECT RAISE(ABORT, 'observations are append-only'); END;
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_observations_no_update")
    op.execute("DROP TRIGGER IF EXISTS trg_observations_no_delete")
    op.drop_table("poller_state")
    op.drop_index("ix_devices_mac", table_name="devices")
    op.drop_table("devices")
    op.drop_table("config")
    op.drop_index("ix_daily_summaries_local_date", table_name="daily_summaries")
    op.drop_table("daily_summaries")
    op.drop_index("ix_sessions_local_date", table_name="sessions")
    op.drop_table("sessions")
    op.drop_index("ix_observations_observed_at", table_name="observations")
    op.drop_index("ix_observations_mac_observed_at", table_name="observations")
    op.drop_table("observations")
