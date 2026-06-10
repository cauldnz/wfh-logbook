"""Webhook ingress: POST /webhook/telegram/{secret} (HANDOFF 7.C).

Two independent checks, both required (ARCH §8.5):

1. The path segment must equal TELEGRAM_WEBHOOK_SECRET.
2. The X-Telegram-Bot-Api-Secret-Token header must equal the same secret
   (Telegram echoes the value given to setWebhook).

Either failing → 401 with no body detail. The raw update is persisted to
``bot_messages`` before processing (inside process_update), and replays of
the same update_id are no-ops via the unique index.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_session
from app.notifier.service import process_update

logger = logging.getLogger(__name__)

router = APIRouter(tags=["telegram"])


@router.post("/webhook/telegram/{secret}")
async def telegram_webhook(
    secret: str,
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
    db: Session = Depends(get_session),  # noqa: B008
) -> Response:
    settings = get_settings()
    expected = settings.telegram_webhook_secret
    if not expected or not settings.telegram_bot_token:
        # Bot not configured: this route should not exist publicly.
        raise HTTPException(status_code=401)
    if secret != expected or x_telegram_bot_api_secret_token != expected:
        logger.warning("telegram: webhook auth failure")  # no secrets logged
        raise HTTPException(status_code=401)

    raw: dict[str, Any] = await request.json()
    notifier = getattr(request.app.state, "telegram_client", None)
    if notifier is None:
        raise HTTPException(status_code=503, detail="bot not initialised")
    process_update(db, raw, notifier, settings)
    return Response(status_code=200)
