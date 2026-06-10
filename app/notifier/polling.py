"""Polling mode: getUpdates long-poll loop (HANDOFF 7.B).

Used for local development and tunnel-outage resilience. Runs as an
asyncio background task; the synchronous Telegram client and DB work are
pushed to worker threads with ``asyncio.to_thread`` so the event loop
stays free.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from app.config import Settings
from app.db import get_sessionmaker
from app.notifier.service import process_update
from app.notifier.telegram import TelegramClient, TelegramError

logger = logging.getLogger(__name__)


async def polling_loop(
    client: TelegramClient,
    settings: Settings,
    stop: asyncio.Event,
) -> None:
    """Long-poll until ``stop`` is set. Failures back off and retry."""
    offset: int | None = None
    logger.info("telegram: polling loop started")
    while not stop.is_set():
        try:
            updates = await asyncio.to_thread(client.get_updates, offset, 25)
        except TelegramError:
            logger.warning("telegram: getUpdates failed; retrying in 5s", exc_info=True)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=5)
            continue
        for raw in updates:
            update_id = raw.get("update_id")
            try:
                await asyncio.to_thread(_process_one, raw, client, settings)
            except Exception:
                # Evidence row is already persisted by process_update before
                # the crash point; log and move on so one bad update cannot
                # wedge the loop.
                logger.exception("telegram: failed processing update %s", update_id)
            if isinstance(update_id, int):
                offset = update_id + 1
    logger.info("telegram: polling loop stopped")


def _process_one(raw: dict[str, object], client: TelegramClient, settings: Settings) -> None:
    SessionLocal = get_sessionmaker()  # noqa: N806 (SQLAlchemy convention)
    with SessionLocal() as db:
        process_update(db, raw, client, settings)
