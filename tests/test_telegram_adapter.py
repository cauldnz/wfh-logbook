"""Telegram adapter (HANDOFF 7.B): inbound parsing against the REAL captured
fixture; outbound payload shapes via respx.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from app.notifier.base import Button, OutgoingMessage
from app.notifier.telegram import TelegramClient, TelegramError, parse_update

FIXTURE = Path(__file__).parent / "fixtures" / "telegram_updates.json"
TOKEN = "0000000000:TESTTOKENTESTTOKEN"
NOW = datetime(2026, 6, 11, 9, 0, tzinfo=UTC)


@pytest.fixture
def updates() -> list[dict]:  # type: ignore[type-arg]
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["result"]


# ----------------------------------------------------------- inbound parsing


class TestParseUpdate:
    """Every assertion mirrors the committed real capture."""

    def test_plain_text_message(self, updates: list[dict]) -> None:  # type: ignore[type-arg]
        u = next(u for u in updates if u.get("message", {}).get("text") == "Ping")
        ev = parse_update(u, now=NOW)
        assert ev is not None
        assert ev.kind == "text"
        assert ev.text == "Ping"
        assert ev.chat_id == 111111111
        assert ev.user_id == 111111111
        assert ev.update_id == u["update_id"]
        # date comes from the message's epoch int, not `now`.
        assert ev.occurred_at == datetime.fromtimestamp(u["message"]["date"], tz=UTC)
        # Authorisation untouched by the parser.
        assert ev.authorised is False

    def test_start_command_with_entities(self, updates: list[dict]) -> None:  # type: ignore[type-arg]
        u = next(u for u in updates if u.get("message", {}).get("text") == "/start")
        ev = parse_update(u, now=NOW)
        assert ev is not None
        assert ev.kind == "command"
        assert ev.command == "/start"
        assert ev.args == ""

    def test_yesterday_command(self, updates: list[dict]) -> None:  # type: ignore[type-arg]
        u = next(u for u in updates if u.get("message", {}).get("text") == "/yesterday")
        ev = parse_update(u, now=NOW)
        assert ev is not None
        assert ev.kind == "command"
        assert ev.command == "/yesterday"

    def test_adjustment_text_is_plain_text_not_command(self, updates: list[dict]) -> None:  # type: ignore[type-arg]
        u = next(u for u in updates if u.get("message", {}).get("text") == "-45 lunch")
        ev = parse_update(u, now=NOW)
        assert ev is not None
        assert ev.kind == "text"
        assert ev.text == "-45 lunch"

    def test_callback_query(self, updates: list[dict]) -> None:  # type: ignore[type-arg]
        u = next(u for u in updates if "callback_query" in u)
        ev = parse_update(u, now=NOW)
        assert ev is not None
        assert ev.kind == "callback"
        assert ev.callback_data == "confirm:2026-06-10"
        # callback_query.id is a STRING in the real payload.
        assert ev.callback_query_id == u["callback_query"]["id"]
        assert isinstance(ev.callback_query_id, str)
        # message_id of the bot message that carried the button.
        assert ev.message_id == u["callback_query"]["message"]["message_id"]
        # No tap timestamp exists → caller-supplied now.
        assert ev.occurred_at == NOW

    def test_every_fixture_update_parses(self, updates: list[dict]) -> None:  # type: ignore[type-arg]
        events = [parse_update(u, now=NOW) for u in updates]
        assert all(e is not None for e in events)

    def test_unknown_update_kind_returns_none(self) -> None:
        assert parse_update({"update_id": 1, "my_chat_member": {}}, now=NOW) is None

    def test_command_with_botname_suffix_normalised(self) -> None:
        u = {
            "update_id": 2,
            "message": {
                "message_id": 9,
                "from": {"id": 5},
                "chat": {"id": 5},
                "date": 1781091000,
                "text": "/yesterday@SomeBot extra args",
                "entities": [{"offset": 0, "length": 18, "type": "bot_command"}],
            },
        }
        ev = parse_update(u, now=NOW)
        assert ev is not None
        assert ev.command == "/yesterday"
        assert ev.args == "extra args"

    def test_non_text_message_ignored(self) -> None:
        u = {
            "update_id": 3,
            "message": {
                "message_id": 10,
                "from": {"id": 5},
                "chat": {"id": 5},
                "date": 1781091000,
                "photo": [{"file_id": "x"}],
            },
        }
        assert parse_update(u, now=NOW) is None


# ------------------------------------------------------------ outbound calls


class TestOutbound:
    @respx.mock
    def test_send_message_payload_shape(self) -> None:
        route = respx.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 55}})
        )
        client = TelegramClient(TOKEN)
        sent = client.send(
            OutgoingMessage(
                chat_id=111,
                text="hello",
                buttons=(
                    (
                        Button("✓ Confirm", "confirm:2026-06-10"),
                        Button("🔒 Lock", "lock:2026-06-10"),
                    ),
                ),
            )
        )
        client.close()
        assert sent.message_id == 55
        body = json.loads(route.calls.last.request.content)
        assert body["chat_id"] == 111
        assert body["text"] == "hello"
        kb = body["reply_markup"]["inline_keyboard"]
        assert kb == [
            [
                {"text": "✓ Confirm", "callback_data": "confirm:2026-06-10"},
                {"text": "🔒 Lock", "callback_data": "lock:2026-06-10"},
            ]
        ]

    @respx.mock
    def test_send_without_buttons_omits_reply_markup(self) -> None:
        route = respx.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
        )
        client = TelegramClient(TOKEN)
        client.send(OutgoingMessage(chat_id=111, text="plain"))
        client.close()
        assert "reply_markup" not in json.loads(route.calls.last.request.content)

    @respx.mock
    def test_edit_message_payload(self) -> None:
        route = respx.post(f"https://api.telegram.org/bot{TOKEN}/editMessageText").mock(
            return_value=httpx.Response(200, json={"ok": True, "result": {}})
        )
        client = TelegramClient(TOKEN)
        client.edit(111, 55, OutgoingMessage(chat_id=111, text="updated"))
        client.close()
        body = json.loads(route.calls.last.request.content)
        assert body == {"chat_id": 111, "message_id": 55, "text": "updated"}

    @respx.mock
    def test_answer_callback(self) -> None:
        route = respx.post(f"https://api.telegram.org/bot{TOKEN}/answerCallbackQuery").mock(
            return_value=httpx.Response(200, json={"ok": True, "result": True})
        )
        client = TelegramClient(TOKEN)
        client.answer_callback("12345", "Locked.")
        client.close()
        body = json.loads(route.calls.last.request.content)
        assert body == {"callback_query_id": "12345", "text": "Locked."}

    @respx.mock
    def test_set_webhook_sends_secret_token(self) -> None:
        route = respx.post(f"https://api.telegram.org/bot{TOKEN}/setWebhook").mock(
            return_value=httpx.Response(200, json={"ok": True, "result": True})
        )
        client = TelegramClient(TOKEN)
        client.set_webhook("https://wfh.example.com/webhook/telegram/s3cret", "s3cret")
        client.close()
        body = json.loads(route.calls.last.request.content)
        assert body["secret_token"] == "s3cret"

    @respx.mock
    def test_get_updates_with_offset(self) -> None:
        route = respx.post(f"https://api.telegram.org/bot{TOKEN}/getUpdates").mock(
            return_value=httpx.Response(200, json={"ok": True, "result": [{"update_id": 7}]})
        )
        client = TelegramClient(TOKEN)
        updates = client.get_updates(offset=8)
        client.close()
        assert updates == [{"update_id": 7}]
        assert json.loads(route.calls.last.request.content)["offset"] == 8

    @respx.mock
    def test_api_error_raises_without_token_in_message(self) -> None:
        respx.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage").mock(
            return_value=httpx.Response(
                400, json={"ok": False, "description": "Bad Request: chat not found"}
            )
        )
        client = TelegramClient(TOKEN)
        with pytest.raises(TelegramError) as exc:
            client.send(OutgoingMessage(chat_id=999, text="x"))
        client.close()
        assert TOKEN not in str(exc.value)
        assert "chat not found" in str(exc.value)
