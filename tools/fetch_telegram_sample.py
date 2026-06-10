"""Capture real Telegram Bot API payloads for Phase 7 fixtures.

Per CLAUDE.md "Real Data First": the notifier is built against payloads
captured from the real Bot API, not invented from documentation. Opt-in,
manual, never in CI.

Modes:

    python tools/fetch_telegram_sample.py                 # pull updates (getUpdates)
    python tools/fetch_telegram_sample.py --send-button   # send an inline-keyboard
                                                          # test message to the
                                                          # captured chat
    python tools/fetch_telegram_sample.py --sanitise      # produce committed fixtures

Raw captures land in tests/fixtures/telegram_updates.raw.json (GITIGNORED —
contains your user id, name, chat id). --sanitise writes
tests/fixtures/telegram_updates.json with identifying values replaced but
every field name and structure preserved.

The capture does NOT consume updates (no offset is sent), so re-running is
idempotent until the bot goes live.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from app.config import get_settings

logger = logging.getLogger("fetch_telegram_sample")

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
RAW_FILE = FIXTURES_DIR / "telegram_updates.raw.json"
SANITISED_FILE = FIXTURES_DIR / "telegram_updates.json"

API = "https://api.telegram.org"


def _call(token: str, method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    with httpx.Client(timeout=35.0) as c:
        r = c.post(f"{API}/bot{token}/{method}", json=payload or {})
    body: Any = r.json()
    if not isinstance(body, dict) or not body.get("ok"):
        raise SystemExit(f"{method} failed: HTTP {r.status_code} body={r.text[:300]}")
    return body


def run_fetch() -> None:
    settings = get_settings()
    if not settings.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set in .env")
    token = settings.telegram_bot_token

    me = _call(token, "getMe")
    print(f"bot: @{me['result'].get('username')} (id {me['result'].get('id')})")

    body = _call(token, "getUpdates", {"timeout": 5})
    updates = body.get("result", [])
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)

    # Merge with any previous capture so multiple rounds accumulate.
    existing: list[dict[str, Any]] = []
    if RAW_FILE.exists():
        existing = json.loads(RAW_FILE.read_text(encoding="utf-8")).get("result", [])
    seen_ids = {u.get("update_id") for u in existing}
    merged = existing + [u for u in updates if u.get("update_id") not in seen_ids]
    RAW_FILE.write_text(
        json.dumps({"ok": True, "result": merged}, indent=2), encoding="utf-8"
    )

    print(f"captured: {len(updates)} update(s) this pull, {len(merged)} total in {RAW_FILE.name}")
    kinds: dict[str, int] = {}
    user_ids: set[int] = set()
    for u in merged:
        for key in ("message", "callback_query", "edited_message", "my_chat_member"):
            if key in u:
                kinds[key] = kinds.get(key, 0) + 1
                from_obj = u[key].get("from", {})
                if isinstance(from_obj, dict) and "id" in from_obj:
                    user_ids.add(from_obj["id"])
    for k, n in sorted(kinds.items()):
        print(f"  {n:>3}  {k}")
    if user_ids:
        print(f"sender user id(s): {sorted(user_ids)}  <- TELEGRAM_ALLOWED_USER_IDS")
    if "callback_query" not in kinds:
        print()
        print("No callback_query captured yet. Run with --send-button, tap the")
        print("button on your phone, then run the fetch again.")


def run_send_button() -> None:
    """Send an inline-keyboard message to the chat seen in the raw capture."""
    settings = get_settings()
    token = settings.telegram_bot_token
    if not RAW_FILE.exists():
        raise SystemExit("no raw capture yet — run the plain fetch first")
    merged = json.loads(RAW_FILE.read_text(encoding="utf-8"))["result"]
    chat_ids = {
        u["message"]["chat"]["id"]
        for u in merged
        if "message" in u and "chat" in u["message"]
    }
    if not chat_ids:
        raise SystemExit("no chat id in capture — send the bot a message first")
    chat_id = sorted(chat_ids)[0]
    _call(
        token,
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": (
                "Fixture capture: tap the button below so the real "
                "callback_query payload shape can be recorded."
            ),
            "reply_markup": {
                "inline_keyboard": [
                    [{"text": "✅ Tap me", "callback_data": "confirm:2026-06-10"}]
                ]
            },
        },
    )
    print(f"button message sent to chat {chat_id}. Tap it, then re-run the fetch.")


# ----------------------------------------------------------------- sanitise

FAKE_USER_ID = 111111111
FAKE_CHAT_ID = 111111111
FAKE_BOT_ID = 222222222


def _sanitise_obj(obj: Any) -> Any:
    """Recursively replace identifying values, preserving structure/types."""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if k in ("id",) and isinstance(v, int):
                # user/chat ids are large ints; bots have is_bot sibling.
                out[k] = FAKE_BOT_ID if obj.get("is_bot") else FAKE_USER_ID
            elif k in ("first_name", "last_name"):
                out[k] = "Testuser"
            elif k == "username":
                out[k] = "testbot" if obj.get("is_bot") else "testuser"
            elif k == "language_code":
                out[k] = "en"
            else:
                out[k] = _sanitise_obj(v)
        return out
    if isinstance(obj, list):
        return [_sanitise_obj(v) for v in obj]
    return obj


def run_sanitise() -> None:
    if not RAW_FILE.exists():
        raise SystemExit("no raw capture to sanitise")
    raw = json.loads(RAW_FILE.read_text(encoding="utf-8"))
    sanitised = _sanitise_obj(raw)
    SANITISED_FILE.write_text(json.dumps(sanitised, indent=2), encoding="utf-8")
    print(f"wrote {SANITISED_FILE}")
    print("Review before committing: text fields are kept verbatim (the test")
    print("messages are deliberate); ids/names/usernames are replaced.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    g = parser.add_mutually_exclusive_group()
    g.add_argument("--send-button", action="store_true")
    g.add_argument("--sanitise", action="store_true")
    parser.add_argument("--log-level", default="WARNING")
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level)
    if args.send_button:
        run_send_button()
    elif args.sanitise:
        run_sanitise()
    else:
        run_fetch()
    return 0


if __name__ == "__main__":
    sys.exit(main())
