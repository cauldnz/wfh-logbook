"""SQLAlchemy engine + session factory.

Pattern: a single engine per process, a session-per-request via ``get_session``
(FastAPI dependency) for API code, and short-lived sessions for background jobs.

Immutability of ``observations`` and ``bot_messages`` is enforced at TWO levels
(belt-and-braces, per CLAUDE.md "Immutability is testable"):

1. SQL triggers installed at engine ``connect`` time (catch raw SQL).
2. SQLAlchemy ORM events on the mapper (catch ORM operations early with a
   clearer error message).
"""

from __future__ import annotations

import logging
from collections.abc import Generator
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


# Tables that are append-only forever. These names are duplicated here from
# models.py to avoid an import cycle; tests verify they match.
IMMUTABLE_TABLES = ("observations", "bot_messages")


def _install_immutability_triggers(conn: Connection) -> None:
    """Create BEFORE UPDATE / BEFORE DELETE triggers on append-only tables.

    Idempotent (CREATE TRIGGER IF NOT EXISTS).
    """
    for table in IMMUTABLE_TABLES:
        # Skip if the table doesn't exist yet (initial migration not run).
        exists = conn.exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).first()
        if not exists:
            continue
        for op in ("UPDATE", "DELETE"):
            conn.exec_driver_sql(
                f"""
                CREATE TRIGGER IF NOT EXISTS trg_{table}_no_{op.lower()}
                BEFORE {op} ON {table}
                BEGIN
                    SELECT RAISE(ABORT, '{table} is append-only');
                END;
                """
            )


def _on_connect(dbapi_conn: Any, _connection_record: Any) -> None:
    """Per-connection SQLite pragmas.

    - ``foreign_keys=ON``: SQLite is OFF by default; we rely on FKs.
    - ``journal_mode=WAL``: better concurrency for our read-heavy workload.
    """
    cursor = dbapi_conn.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
    finally:
        cursor.close()


def init_engine(settings: Settings | None = None) -> Engine:
    """Create (or return cached) engine and session factory."""
    global _engine, _SessionLocal
    if _engine is not None:
        return _engine
    s = settings or get_settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    _engine = create_engine(
        s.db_url(),
        future=True,
        echo=False,
        connect_args={"check_same_thread": False},
    )
    event.listen(_engine, "connect", _on_connect)
    # Triggers are created by the initial migration; ``install_triggers_now()``
    # is called from the app lifespan as belt-and-braces (idempotent).
    _SessionLocal = sessionmaker(
        bind=_engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        class_=Session,
    )
    logger.info("Database engine initialised at %s", s.db_path())
    return _engine


def get_engine() -> Engine:
    if _engine is None:
        return init_engine()
    return _engine


def get_sessionmaker() -> sessionmaker[Session]:
    if _SessionLocal is None:
        init_engine()
    assert _SessionLocal is not None
    return _SessionLocal


def get_session() -> Generator[Session, None, None]:
    """FastAPI dependency: yields a session, commits on success, rolls back on error."""
    SessionLocal = get_sessionmaker()  # noqa: N806 (SQLAlchemy convention)
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def install_triggers_now() -> None:
    """Install immutability triggers immediately (used post-migration)."""
    engine = get_engine()
    with engine.begin() as conn:
        _install_immutability_triggers(conn)


def reset_engine_cache() -> None:
    """Test helper: drop the cached engine so the next init reads new settings."""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None
