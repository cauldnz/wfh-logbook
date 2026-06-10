"""Thin Telegram Bot API adapter (HANDOFF 7.B).

Raw httpx — deliberately no framework library. Two responsibilities:

1. Outbound: implement the ``Notifier`` protocol (send / edit /
   answer_callback) plus webhook + polling plumbing calls.
2. Inbound: ``parse_update`` normalises a raw update dict into an
   ``IncomingEvent``.

Every inbound field reference is verified against the committed real
capture (tests/fixtures/telegram_updates.json). Facts encoded from it:

- message ``date`` is a unix-epoch int; ``entities`` lists carry
  ``{offset, length, type}`` with ``type == "bot_command"`` for commands.
- ``callback_query.id`` is a STRING.
- callback_query has NO tap timestamp — only the embedded (button)
  message's ``date``. Callers supply ``now`` for event ordering.
- Human senders may lack ``username`` (only first/last name); nothing
  here requires it.

The bot token appears only in the URL path; it is never logged (CLAUDE.md
secrets rule), and HTTP errors are reported with the method name only.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from app.notifier.base import IncomingEvent, OutgoingMessage, SentMessage

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"


class TelegramError(RuntimeError):
    """A Bot API call failed. Message excludes the token by construction."""


def _buttons_to_inline_keyboard(
    buttons: tuple[tuple[Any, ...], ...],
) -> dict[str, list[list[dict[str, str]]]]:
    return {
        "inline_keyboard": [
            [{"text": b.text, "callback_data": b.callback_data} for b in row] for row in buttons
        ]
    }


class TelegramClient:
    """Synchronous Bot API client implementing the Notifier protocol."""

    def __init__(self, token: str, timeout: float = 35.0) -> None:
        self._token = token
        self._client = httpx.Client(timeout=timeout)

    # ------------------------------------------------------------ plumbing
    def _call(self, method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            r = self._client.post(f"{API_BASE}/bot{self._token}/{method}", json=payload or {})
        except httpx.HTTPError as e:
            # repr(e) cannot contain the token: httpx errors stringify the
            # exception class + message, and we never put the URL in it.
            raise TelegramError(f"{method}: transport error {type(e).__name__}") from e
        body: Any = r.json()
        if not isinstance(body, dict) or not body.get("ok"):
            description = body.get("description") if isinstance(body, dict) else r.text[:200]
            raise TelegramError(f"{method}: HTTP {r.status_code} — {description}")
        result: Any = body.get("result")
        return result if isinstance(result, dict) else {"result": result}

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------- Notifier protocol
    def send(self, message: OutgoingMessage) -> SentMessage:
        payload: dict[str, Any] = {"chat_id": message.chat_id, "text": message.text}
        if message.buttons:
            payload["reply_markup"] = _buttons_to_inline_keyboard(message.buttons)
        result = self._call("sendMessage", payload)
        return SentMessage(
            chat_id=message.chat_id,
            message_id=int(result.get("message_id", 0)),
            raw=result,
        )

    def edit(self, chat_id: int, message_id: int, message: OutgoingMessage) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": message.text,
        }
        if message.buttons:
            payload["reply_markup"] = _buttons_to_inline_keyboard(message.buttons)
        self._call("editMessageText", payload)

    def answer_callback(self, callback_query_id: str, text: str | None = None) -> None:
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        self._call("answerCallbackQuery", payload)

    # -------------------------------------------------- webhook / polling
    def set_webhook(self, url: str, secret_token: str) -> None:
        self._call("setWebhook", {"url": url, "secret_token": secret_token})
        logger.info("telegram: webhook registered")

    def delete_webhook(self) -> None:
        self._call("deleteWebhook")
        logger.info("telegram: webhook deleted")

    def get_updates(self, offset: int | None = None, timeout: int = 25) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            payload["offset"] = offset
        result = self._call("getUpdates", payload)
        updates = result.get("result", [])
        return updates if isinstance(updates, list) else []


# ------------------------------------------------------------------ inbound


def parse_update(update: dict[str, Any], now: datetime | None = None) -> IncomingEvent | None:
    """Raw update dict → IncomingEvent, or None for kinds we don't handle.

    Authorisation fields are left at their defaults — the service resolves
    them against the allowlist before dispatching to the conversation.
    """
    update_id = update.get("update_id")

    message = update.get("message")
    if isinstance(message, dict):
        from_obj = message.get("from") or {}
        chat_obj = message.get("chat") or {}
        text = message.get("text")
        if not isinstance(text, str):
            logger.debug("telegram: ignoring non-text message update %s", update_id)
            return None
        date_epoch = message.get("date")
        occurred = (
            datetime.fromtimestamp(date_epoch, tz=UTC)
            if isinstance(date_epoch, int)
            else (now or datetime.now(UTC))
        )
        entities = message.get("entities") or []
        is_command = any(
            isinstance(e, dict) and e.get("type") == "bot_command" and e.get("offset") == 0
            for e in entities
        )
        if is_command:
            token, _, args = text.partition(" ")
            # Strip a trailing @BotName so group-style commands still match.
            command = token.split("@", 1)[0].lower()
            return IncomingEvent(
                kind="command",
                chat_id=int(chat_obj.get("id", 0)),
                user_id=int(from_obj.get("id", 0)),
                occurred_at=occurred,
                update_id=update_id,
                command=command,
                args=args.strip(),
                message_id=message.get("message_id"),
            )
        return IncomingEvent(
            kind="text",
            chat_id=int(chat_obj.get("id", 0)),
            user_id=int(from_obj.get("id", 0)),
            occurred_at=occurred,
            update_id=update_id,
            text=text,
            message_id=message.get("message_id"),
        )

    callback = update.get("callback_query")
    if isinstance(callback, dict):
        from_obj = callback.get("from") or {}
        embedded = callback.get("message") or {}
        chat_obj = embedded.get("chat") or {}
        # No tap timestamp exists on callback_query (verified in fixture);
        # use the caller-supplied clock.
        occurred = now or datetime.now(UTC)
        return IncomingEvent(
            kind="callback",
            chat_id=int(chat_obj.get("id", 0)),
            user_id=int(from_obj.get("id", 0)),
            occurred_at=occurred,
            update_id=update_id,
            callback_data=callback.get("data"),
            callback_query_id=str(callback.get("id", "")),
            message_id=embedded.get("message_id"),
        )

    logger.debug("telegram: ignoring unhandled update kind (id %s)", update_id)
    return None
