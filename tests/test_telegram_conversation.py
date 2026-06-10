"""Conversation state machine (HANDOFF 7.A / 7.F) — pure, no I/O.

Each test constructs an IncomingEvent, awaiting-state inputs, and a stub
DbReader, then asserts on the returned action list — exactly the testing
shape the spec prescribes.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from app.notifier.base import (
    AnswerCallback,
    ApplyAdjustment,
    ApplyConfirm,
    ApplyLock,
    ClearAwaiting,
    DayView,
    IncomingEvent,
    SendMessage,
    SessionView,
    SetAwaiting,
    StatusView,
    WeekDayTotal,
    YearView,
)
from app.notifier.conversation import (
    FREE_TEXT_HINT,
    HELP_TEXT,
    REJECTION_TEXT,
    handle_event,
)

TODAY = date(2026, 6, 11)
YESTERDAY = date(2026, 6, 10)
NOW = datetime(2026, 6, 11, 8, 0, tzinfo=UTC)


class StubReader:
    """Programmable DbReader."""

    def __init__(self, days: dict[date, DayView] | None = None) -> None:
        self.days = days or {}

    def today(self) -> date:
        return TODAY

    def day_view(self, target_date: date) -> DayView | None:
        return self.days.get(target_date)

    def week_totals(self) -> list[WeekDayTotal]:
        return [
            WeekDayTotal(local_date=TODAY - timedelta(days=i + 1), claimed_seconds=3600 * 8)
            for i in range(2)
        ]

    def year_view(self) -> YearView:
        return YearView(
            fy_label="2025-26",
            total_claimed_seconds=int(3600 * 412.5),
            locked_days=200,
            unlocked_days=3,
        )

    def status_view(self) -> StatusView:
        return StatusView(
            last_poll_succeeded_at="2026-06-11 07:59",
            last_sessioniser_run_at="2026-06-11 01:15",
            last_backup_at="2026-06-11 02:00",
            consecutive_failures=0,
        )


def day_view(
    d: date = YESTERDAY,
    *,
    locked: bool = False,
    claimed: int = 4 * 3600,
    adjustment: int = 0,
) -> DayView:
    return DayView(
        local_date=d,
        computed_seconds=claimed - adjustment,
        adjustment_seconds=adjustment,
        adjustment_reason="lunch" if adjustment else None,
        claimed_seconds=claimed,
        version=2 if adjustment else 1,
        locked=locked,
        anomalous=False,
        sessions=(SessionView("09:00", "13:00", 4 * 3600),),
    )


def command(cmd: str, args: str = "", **kw: object) -> IncomingEvent:
    return IncomingEvent(
        kind="command",
        chat_id=1,
        user_id=42,
        occurred_at=NOW,
        command=cmd,
        args=args,
        authorised=True,
        **kw,  # type: ignore[arg-type]
    )


def text_msg(text: str, **kw: object) -> IncomingEvent:
    return IncomingEvent(
        kind="text",
        chat_id=1,
        user_id=42,
        occurred_at=NOW,
        text=text,
        authorised=True,
        **kw,  # type: ignore[arg-type]
    )


def callback(data: str, **kw: object) -> IncomingEvent:
    return IncomingEvent(
        kind="callback",
        chat_id=1,
        user_id=42,
        occurred_at=NOW,
        callback_data=data,
        callback_query_id="q1",
        message_id=77,
        authorised=True,
        **kw,  # type: ignore[arg-type]
    )


def dispatch(
    event: IncomingEvent,
    *,
    awaiting: str | None = None,
    awaiting_date: date | None = None,
    awaiting_age: timedelta | None = None,
    reader: StubReader | None = None,
) -> list[object]:
    return list(handle_event(event, awaiting, awaiting_date, awaiting_age, reader or StubReader()))


# ------------------------------------------------------------ authorisation


class TestAuthorisation:
    """HANDOFF 7.E: exactly one rejection, then silence."""

    def test_unauthorised_first_contact_gets_one_rejection(self) -> None:
        ev = IncomingEvent(
            kind="command",
            chat_id=9,
            user_id=666,
            occurred_at=NOW,
            command="/start",
            authorised=False,
            rejection_already_sent=False,
        )
        actions = dispatch(ev)
        assert actions == [SendMessage(text=REJECTION_TEXT)]

    def test_unauthorised_subsequent_contact_is_silent(self) -> None:
        ev = IncomingEvent(
            kind="text",
            chat_id=9,
            user_id=666,
            occurred_at=NOW,
            text="hello?",
            authorised=False,
            rejection_already_sent=True,
        )
        assert dispatch(ev) == []


# ----------------------------------------------------------------- commands


class TestCommands:
    def test_start_sends_help(self) -> None:
        actions = dispatch(command("/start"))
        assert ClearAwaiting() in actions
        sends = [a for a in actions if isinstance(a, SendMessage)]
        assert len(sends) == 1 and sends[0].text == HELP_TEXT

    def test_yesterday_first_time_shows_summary_with_three_buttons(self) -> None:
        reader = StubReader({YESTERDAY: day_view()})
        actions = dispatch(command("/yesterday"), reader=reader)
        send = next(a for a in actions if isinstance(a, SendMessage))
        assert "4:00 h claimed" in send.text
        labels = [b.text for row in send.buttons for b in row]
        assert labels == ["✓ Confirm", "✏ Adjust", "🔒 Lock"]
        datas = [b.callback_data for row in send.buttons for b in row]
        assert datas == [
            f"confirm:{YESTERDAY}",
            f"adjust:{YESTERDAY}",
            f"lock:{YESTERDAY}",
        ]

    def test_yesterday_after_lock_shows_locked_and_only_adjust(self) -> None:
        reader = StubReader({YESTERDAY: day_view(locked=True)})
        actions = dispatch(command("/yesterday"), reader=reader)
        send = next(a for a in actions if isinstance(a, SendMessage))
        assert "🔒 locked" in send.text
        labels = [b.text for row in send.buttons for b in row]
        assert labels == ["✏ Adjust"]

    def test_today_has_adjust_but_never_lock(self) -> None:
        reader = StubReader({TODAY: day_view(TODAY)})
        actions = dispatch(command("/today"), reader=reader)
        send = next(a for a in actions if isinstance(a, SendMessage))
        assert "Today so far" in send.text
        labels = [b.text for row in send.buttons for b in row]
        assert labels == ["✏ Adjust"]

    def test_day_with_bad_date_gets_usage_hint(self) -> None:
        actions = dispatch(command("/day", args="not-a-date"))
        send = next(a for a in actions if isinstance(a, SendMessage))
        assert "YYYY-MM-DD" in send.text

    def test_day_without_data_explains(self) -> None:
        actions = dispatch(command("/day", args="2026-01-01"))
        send = next(a for a in actions if isinstance(a, SendMessage))
        assert "No data" in send.text
        assert send.buttons == ()

    def test_week_lists_days_and_total(self) -> None:
        actions = dispatch(command("/week"))
        send = next(a for a in actions if isinstance(a, SendMessage))
        assert "total" in send.text
        assert send.buttons == ()

    def test_year_totals(self) -> None:
        actions = dispatch(command("/year"))
        send = next(a for a in actions if isinstance(a, SendMessage))
        assert "2025-26" in send.text and "412:30" in send.text

    def test_status_lines(self) -> None:
        actions = dispatch(command("/status"))
        send = next(a for a in actions if isinstance(a, SendMessage))
        assert "Last poll" in send.text

    def test_unknown_command(self) -> None:
        actions = dispatch(command("/frobnicate"))
        send = next(a for a in actions if isinstance(a, SendMessage))
        assert "/help" in send.text

    def test_any_command_clears_awaiting(self) -> None:
        actions = dispatch(
            command("/week"),
            awaiting="adjustment",
            awaiting_date=YESTERDAY,
            awaiting_age=timedelta(minutes=5),
        )
        assert ClearAwaiting() in actions


# ---------------------------------------------------------------- callbacks


class TestCallbacks:
    def test_adjust_callback_sets_awaiting_and_prompts(self) -> None:
        actions = dispatch(callback(f"adjust:{YESTERDAY}"))
        assert AnswerCallback("q1") in actions
        assert SetAwaiting(awaiting_date=YESTERDAY) in actions
        send = next(a for a in actions if isinstance(a, SendMessage))
        assert "-45 lunch" in send.text

    def test_confirm_callback_applies_zero_adjustment(self) -> None:
        actions = dispatch(callback(f"confirm:{YESTERDAY}"))
        assert ApplyConfirm(target_date=YESTERDAY, message_id=77) in actions

    def test_lock_callback_applies_lock(self) -> None:
        actions = dispatch(callback(f"lock:{YESTERDAY}"))
        assert ApplyLock(target_date=YESTERDAY, message_id=77) in actions

    def test_malformed_callback_answers_unknown(self) -> None:
        actions = dispatch(callback("garbage-data"))
        assert actions == [AnswerCallback("q1", "Unknown action.")]


# ---------------------------------------------------------------- free text


class TestFreeText:
    def test_adjustment_while_awaiting_applies_and_clears(self) -> None:
        actions = dispatch(
            text_msg("-45 lunch"),
            awaiting="adjustment",
            awaiting_date=YESTERDAY,
            awaiting_age=timedelta(minutes=2),
        )
        assert ClearAwaiting() in actions
        apply = next(a for a in actions if isinstance(a, ApplyAdjustment))
        assert apply.target_date == YESTERDAY
        assert apply.minutes == -45
        assert apply.reason == "lunch"

    def test_bad_adjustment_while_awaiting_keeps_state_and_replies_error(self) -> None:
        actions = dispatch(
            text_msg("-45"),
            awaiting="adjustment",
            awaiting_date=YESTERDAY,
            awaiting_age=timedelta(minutes=2),
        )
        # No ClearAwaiting: the user can simply try again.
        assert ClearAwaiting() not in actions
        send = next(a for a in actions if isinstance(a, SendMessage))
        assert "reason" in send.text.lower()

    def test_text_while_not_awaiting_gets_hint(self) -> None:
        actions = dispatch(text_msg("did some work today"))
        assert actions == [SendMessage(text=FREE_TEXT_HINT)]

    def test_awaiting_times_out_after_30_minutes(self) -> None:
        actions = dispatch(
            text_msg("-45 lunch"),
            awaiting="adjustment",
            awaiting_date=YESTERDAY,
            awaiting_age=timedelta(minutes=31),
        )
        # Stale state: cleared, and the text treated as idle chatter.
        assert ClearAwaiting() in actions
        assert not any(isinstance(a, ApplyAdjustment) for a in actions)
        send = next(a for a in actions if isinstance(a, SendMessage))
        assert send.text == FREE_TEXT_HINT

    def test_awaiting_just_under_timeout_still_applies(self) -> None:
        actions = dispatch(
            text_msg("-45 lunch"),
            awaiting="adjustment",
            awaiting_date=YESTERDAY,
            awaiting_age=timedelta(minutes=29),
        )
        assert any(isinstance(a, ApplyAdjustment) for a in actions)
