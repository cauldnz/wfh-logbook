"""Alembic environment.

Reads the SQLite URL from `app.config.Settings` so migrations always target
the configured DB path (no duplication in alembic.ini).
"""

from __future__ import annotations

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context
from app.config import get_settings
from app.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.db_url())

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Generate SQL without a DBAPI connection."""
    context.configure(
        url=settings.db_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live database."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    cfg_dict = config.get_section(config.config_ini_section, {}) or {}
    cfg_dict["sqlalchemy.url"] = settings.db_url()
    connectable = engine_from_config(
        cfg_dict,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,  # SQLite-safe ALTER TABLE
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
