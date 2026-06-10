"""Service glue + webhook ingress + idempotency (HANDOFF 7.C/7.E/7.G).

The acceptance flow from HANDOFF Phase 7 runs here end-to-end against a
real (test) database with a fake transport: /day → tap Adjust → reply
"-45 lunch" → tap Lock, with created_by='telegram' on the resulting
daily_summaries versions.
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, date, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import BotChat, BotMessage, BotState, DailySummary, Observation
from app.notifier.base import OutgoingMessage, SentMessage
from app.notifier.service import process_update
from app.sessions.persistence import sessionise_date
from app.sessions.rules import RuleSet

USER_ID = 111111111  # matches the sanitised fixture's user id
CHAT_ID = 111111111
DAY = date(2026, 5, 20)


class FakeNotifier:
    """Captures outbound calls; returns deterministic message ids."""

    def __init__(self, fail_on_send: bool = False) -> None:
        self.sent: list[OutgoingMessage] = []
        self.edited: list[tuple[int, int, OutgoingMessage]] = []
        self.answered: list[tuple[str, str | None]] = []
        self._next_id = 1000
        self.fail_on_send = fail_on_send

    def send(self, message: OutgoingMessage) -> SentMessage:
        if self.fail_on_send:
            raise RuntimeError("transport down")
        self.sent.append(message)
        self._next_id += 1
        return SentMessage(chat_id=message.chat_id, message_id=self._next_id, raw={})

    def edit(self, chat_id: int, message_id: int, message: OutgoingMessage) -> None:
        self.edited.append((chat_id, message_id, message))

    def answer_callback(self, callback_query_id: str, text: str | None = None) -> None:
        self.answered.append((callback_query_id, text))


def bot_settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "telegram_bot_token": "0:TEST",
        "telegram_allowed_user_ids": str(USER_ID),
        "local_timezone": "Australia/Sydney",
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


# Raw updates mirror the committed fixture's shapes exactly (synthetic
# values, captured schema — per CLAUDE.md that's the allowed combination).
_UPDATE_SEQ = iter(range(500_000, 600_000))


def msg_update(text: str, *, user_id: int = USER_ID, command: bool = False) -> dict[str, Any]:
    u: dict[str, Any] = {
        "update_id": next(_UPDATE_SEQ),
        "message": {
            "message_id": next(_UPDATE_SEQ),
            "from": {"id": user_id, "is_bot": False, "first_name": "T"},
            "chat": {"id": user_id, "first_name": "T", "type": "private"},
            "date": 1781090763,
            "text": text,
        },
    }
    if command:
        token = text.split(" ", 1)[0]
        u["message"]["entities"] = [{"offset": 0, "length": len(token), "type": "bot_command"}]
    return u


def callback_update(data: str, *, user_id: int = USER_ID, message_id: int = 77) -> dict[str, Any]:
    return {
        "update_id": next(_UPDATE_SEQ),
        "callback_query": {
            "id": str(next(_UPDATE_SEQ)),
            "from": {"id": user_id, "is_bot": False, "first_name": "T"},
            "message": {
                "message_id": message_id,
                "from": {"id": 222, "is_bot": True, "first_name": "bot"},
                "chat": {"id": user_id, "first_name": "T", "type": "private"},
                "date": 1781091017,
                "text": "day view",
            },
            "chat_instance": "x",
            "data": data,
        },
    }


@pytest.fixture
def seeded_day(db_session: Session) -> date:
    from app.config import get_settings
    from app.main import seed_config_if_missing

    seed_config_if_missing(db_session, get_settings())
    db_session.commit()
    rules = RuleSet.from_db(db_session)
    for ts, conn in [
        (datetime(2026, 5, 20, 9, 0, tzinfo=UTC), True),
        (datetime(2026, 5, 20, 12, 0, tzinfo=UTC), False),
    ]:
        db_session.add(
            Observation(
                observed_at=ts,
                controller_seen_at=ts,
                mac="a",
                device_label="iPhone",
                ssid="WFH-TEST",
                is_connected=conn,
                signal_dbm=None,
                raw_json="{}",
            )
        )
    db_session.commit()
    sessionise_date(db_session, DAY, rules)
    db_session.commit()
    return DAY


class TestAcceptanceCycle:
    """HANDOFF Phase 7 acceptance: the full review loop via the bot."""

    def test_full_cycle_day_adjust_text_lock(self, db_session: Session, seeded_day: date) -> None:
        notifier = FakeNotifier()
        settings = bot_settings()

        # 1. /day 2026-05-20 → summary with Confirm/Adjust/Lock buttons.
        process_update(db_session, msg_update(f"/day {DAY}", command=True), notifier, settings)
        assert notifier.sent, "expected a day summary reply"
        labels = [b.text for row in notifier.sent[-1].buttons for b in row]
        assert labels == ["✓ Confirm", "✏ Adjust", "🔒 Lock"]

        # 2. Tap ✏ Adjust → awaiting state set, prompt sent.
        process_update(db_session, callback_update(f"adjust:{DAY}"), notifier, settings)
        state = db_session.get(BotState, CHAT_ID)
        assert state is not None and state.awaiting == "adjustment"
        assert state.awaiting_date == DAY.isoformat()

        # 3. Reply "-45 lunch" → v2 with created_by='telegram'.
        process_update(db_session, msg_update("-45 lunch"), notifier, settings)
        rows = (
            db_session.execute(
                select(DailySummary)
                .where(DailySummary.local_date == DAY.isoformat())
                .order_by(DailySummary.version)
            )
            .scalars()
            .all()
        )
        assert [r.version for r in rows] == [1, 2]
        assert rows[1].adjustment_seconds == -45 * 60
        assert rows[1].adjustment_reason == "lunch"
        assert rows[1].created_by == "telegram"
        # Awaiting state cleared; reply offers Lock + Re-adjust.
        state = db_session.get(BotState, CHAT_ID)
        assert state is not None and state.awaiting is None
        labels = [b.text for row in notifier.sent[-1].buttons for b in row]
        assert labels == ["🔒 Lock", "✏ Re-adjust"]

        # 4. Tap 🔒 Lock → latest version locked; message edited, no buttons.
        process_update(db_session, callback_update(f"lock:{DAY}"), notifier, settings)
        latest = (
            db_session.execute(
                select(DailySummary)
                .where(DailySummary.local_date == DAY.isoformat())
                .order_by(DailySummary.version.desc())
                .limit(1)
            )
            .scalars()
            .one()
        )
        assert bool(latest.locked) is True
        assert notifier.edited, "lock should edit the original message"
        _, _, edited_msg = notifier.edited[-1]
        assert edited_msg.buttons == ()  # Adjust/Lock removed per spec

        # Full audit trail: every inbound + outbound persisted.
        in_rows = (
            db_session.execute(select(BotMessage).where(BotMessage.direction == "in"))
            .scalars()
            .all()
        )
        out_rows = (
            db_session.execute(select(BotMessage).where(BotMessage.direction == "out"))
            .scalars()
            .all()
        )
        assert len(in_rows) == 4
        assert len(out_rows) >= 4

    def test_confirm_creates_zero_adjustment_version(
        self, db_session: Session, seeded_day: date
    ) -> None:
        notifier = FakeNotifier()
        process_update(db_session, callback_update(f"confirm:{DAY}"), notifier, bot_settings())
        rows = (
            db_session.execute(
                select(DailySummary)
                .where(DailySummary.local_date == DAY.isoformat())
                .order_by(DailySummary.version)
            )
            .scalars()
            .all()
        )
        assert len(rows) == 2
        assert rows[1].adjustment_seconds == 0
        assert rows[1].adjustment_reason == "Confirmed via Telegram"
        assert rows[1].created_by == "telegram"
        # Message edited in place to reflect the confirmation.
        assert notifier.edited


class TestIdempotency:
    """HANDOFF 7.C: replaying an update_id is a no-op."""

    def test_same_update_twice_one_row_one_side_effect(
        self, db_session: Session, seeded_day: date
    ) -> None:
        notifier = FakeNotifier()
        settings = bot_settings()
        update = msg_update("/start", command=True)

        process_update(db_session, update, notifier, settings)
        process_update(db_session, update, notifier, settings)

        in_rows = (
            db_session.execute(select(BotMessage).where(BotMessage.direction == "in"))
            .scalars()
            .all()
        )
        assert len(in_rows) == 1  # single evidence row
        assert len(notifier.sent) == 1  # single side effect


class TestAuthorisationPersistence:
    """HANDOFF 7.E with restart-durable rejection state."""

    def test_unauthorised_rejected_once_then_silent(
        self, db_session: Session, seeded_day: date
    ) -> None:
        notifier = FakeNotifier()
        settings = bot_settings()
        intruder = 999_999

        process_update(
            db_session, msg_update("/start", user_id=intruder, command=True), notifier, settings
        )
        assert len(notifier.sent) == 1
        assert notifier.sent[0].text == "This bot is private."
        chat = db_session.get(BotChat, intruder)
        assert chat is not None and bool(chat.rejection_sent) is True

        process_update(db_session, msg_update("hello again", user_id=intruder), notifier, settings)
        assert len(notifier.sent) == 1  # still just the one rejection

    def test_authorisation_refreshed_per_contact(
        self, db_session: Session, seeded_day: date
    ) -> None:
        """A user added to the allowlist becomes authorised on next contact."""
        notifier = FakeNotifier()
        before = bot_settings(telegram_allowed_user_ids="")  # nobody allowed
        process_update(db_session, msg_update("/start", command=True), notifier, before)
        assert notifier.sent[-1].text == "This bot is private."

        after = bot_settings()  # USER_ID allowed now
        process_update(db_session, msg_update("/help", command=True), notifier, after)
        assert "commands" in notifier.sent[-1].text


class TestWebhookIngress:
    """HANDOFF 7.C dual-secret verification."""

    @pytest.fixture
    def webhook_client(
        self, migrated_db: None, monkeypatch: pytest.MonkeyPatch
    ) -> Generator[TestClient, None, None]:
        from app import config as config_mod
        from app.main import create_app

        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "0:TEST")
        monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "s3cret")
        # webhook mode WITHOUT PUBLIC_BASE_URL: the lifespan initialises the
        # client and the route but performs NO outbound call (no setWebhook,
        # no polling loop) — tests stay hermetic.
        monkeypatch.setenv("TELEGRAM_MODE", "webhook")
        monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
        monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", str(USER_ID))
        monkeypatch.setattr(config_mod, "_settings", None)
        app = create_app()
        with TestClient(app, raise_server_exceptions=False) as client:
            # Replace the transport with the fake AFTER startup.
            app.state.telegram_client = FakeNotifier()
            yield client
        # The lifespan cached a Settings carrying the webhook env; reset so
        # later tests re-read their own env (the engine cache is handled by
        # the migrated_db fixture teardown).
        config_mod.reset_settings_cache()

    def test_wrong_path_secret_401(self, webhook_client: TestClient) -> None:
        r = webhook_client.post(
            "/webhook/telegram/wrong",
            json=msg_update("/start", command=True),
            headers={"X-Telegram-Bot-Api-Secret-Token": "s3cret"},
        )
        assert r.status_code == 401

    def test_wrong_header_secret_401(self, webhook_client: TestClient) -> None:
        r = webhook_client.post(
            "/webhook/telegram/s3cret",
            json=msg_update("/start", command=True),
            headers={"X-Telegram-Bot-Api-Secret-Token": "nope"},
        )
        assert r.status_code == 401

    def test_missing_header_401(self, webhook_client: TestClient) -> None:
        r = webhook_client.post("/webhook/telegram/s3cret", json=msg_update("/start", command=True))
        assert r.status_code == 401

    def test_both_secrets_ok_processes_update(self, webhook_client: TestClient) -> None:
        r = webhook_client.post(
            "/webhook/telegram/s3cret",
            json=msg_update("/help", command=True),
            headers={"X-Telegram-Bot-Api-Secret-Token": "s3cret"},
        )
        assert r.status_code == 200
        fake = webhook_client.app.state.telegram_client  # type: ignore[attr-defined]
        assert fake.sent and "commands" in fake.sent[-1].text

    def test_evidence_survives_processing_crash(
        self, webhook_client: TestClient, db_session: Session
    ) -> None:
        """7.C: raw update persisted BEFORE processing; a transport crash
        afterwards still returns 500 but the evidence row exists."""
        webhook_client.app.state.telegram_client = FakeNotifier(fail_on_send=True)  # type: ignore[attr-defined]
        update = msg_update("/help", command=True)
        r = webhook_client.post(
            "/webhook/telegram/s3cret",
            json=update,
            headers={"X-Telegram-Bot-Api-Secret-Token": "s3cret"},
        )
        assert r.status_code == 500
        row = db_session.execute(
            select(BotMessage).where(BotMessage.telegram_update_id == update["update_id"])
        ).scalar_one_or_none()
        assert row is not None
        assert row.direction == "in"
