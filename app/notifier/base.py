"""Notifier abstraction (HANDOFF 7.A).

Channel-agnostic types. ``conversation.py`` consumes ``IncomingEvent`` and
returns ``OutgoingAction``s; a transport adapter (Telegram today, maybe
Signal/WhatsApp later) turns actions into API calls. Nothing in this module
imports HTTP, the DB, or anything Telegram-specific.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal, Protocol

# --------------------------------------------------------------- incoming


@dataclass(frozen=True, slots=True)
class IncomingEvent:
    """One user interaction, normalised away from transport specifics."""

    kind: Literal["command", "text", "callback"]
    chat_id: int
    user_id: int
    occurred_at: datetime  # tz-aware UTC
    update_id: int | None = None
    # kind == "command"
    command: str | None = None  # e.g. "/yesterday" (lowercase, no @botname)
    args: str = ""  # remainder after the command token
    # kind == "text"
    text: str | None = None
    # kind == "callback"
    callback_data: str | None = None
    callback_query_id: str | None = None
    message_id: int | None = None  # message carrying the tapped button
    # authorisation, resolved by the service before dispatch
    authorised: bool = False
    rejection_already_sent: bool = False


# --------------------------------------------------------------- outgoing


@dataclass(frozen=True, slots=True)
class Button:
    text: str
    callback_data: str


@dataclass(frozen=True, slots=True)
class OutgoingMessage:
    chat_id: int
    text: str
    buttons: tuple[tuple[Button, ...], ...] = ()  # rows of buttons


@dataclass(frozen=True, slots=True)
class SentMessage:
    """What the transport reports back after sending."""

    chat_id: int
    message_id: int
    raw: dict[str, object] = field(repr=False, default_factory=dict)


# Actions the conversation can request. The service executes them in order.


@dataclass(frozen=True, slots=True)
class SendMessage:
    text: str
    buttons: tuple[tuple[Button, ...], ...] = ()


@dataclass(frozen=True, slots=True)
class EditMessage:
    message_id: int
    text: str
    buttons: tuple[tuple[Button, ...], ...] = ()


@dataclass(frozen=True, slots=True)
class AnswerCallback:
    callback_query_id: str
    text: str | None = None


@dataclass(frozen=True, slots=True)
class SetAwaiting:
    """Remember that the next free-text message is an adjustment for a date."""

    awaiting_date: date


@dataclass(frozen=True, slots=True)
class ClearAwaiting:
    pass


@dataclass(frozen=True, slots=True)
class ApplyAdjustment:
    """Execute via the same internal API as the web UI (HANDOFF 7.G).

    ``render_to`` controls how the result is presented: a fresh reply
    (after free-text adjustment) or an edit of the message whose button
    started the flow.
    """

    target_date: date
    minutes: int
    reason: str
    render_to: Literal["reply", "edit"] = "reply"
    message_id: int | None = None


@dataclass(frozen=True, slots=True)
class ApplyConfirm:
    """Zero-magnitude versioned confirmation (ARCH §5.7 rule 3)."""

    target_date: date
    message_id: int | None = None


@dataclass(frozen=True, slots=True)
class ApplyLock:
    target_date: date
    message_id: int | None = None


@dataclass(frozen=True, slots=True)
class ApplyRebuild:
    """Force a sessioniser run for a date (HANDOFF 9.C) — same internal
    path as the web UI's resessionise. Reply carries the resulting view.

    ``announce=False`` suppresses the "(rebuilt: ...)" suffix — used by
    /today, where the rebuild is an implementation detail, not the ask.
    """

    target_date: date
    announce: bool = True


OutgoingAction = (
    SendMessage
    | EditMessage
    | AnswerCallback
    | SetAwaiting
    | ClearAwaiting
    | ApplyAdjustment
    | ApplyConfirm
    | ApplyLock
    | ApplyRebuild
)


# ------------------------------------------------------------- read models


@dataclass(frozen=True, slots=True)
class SessionView:
    """Session pre-rendered for display (times already local)."""

    start_hhmm: str
    end_hhmm: str
    duration_seconds: int


@dataclass(frozen=True, slots=True)
class DayView:
    local_date: date
    computed_seconds: int
    adjustment_seconds: int
    adjustment_reason: str | None
    claimed_seconds: int
    version: int
    locked: bool
    anomalous: bool
    sessions: tuple[SessionView, ...] = ()


@dataclass(frozen=True, slots=True)
class WeekDayTotal:
    local_date: date
    claimed_seconds: int


@dataclass(frozen=True, slots=True)
class YearView:
    fy_label: str
    total_claimed_seconds: int
    locked_days: int
    unlocked_days: int


@dataclass(frozen=True, slots=True)
class StatusView:
    last_poll_succeeded_at: str | None  # pre-rendered local time or None
    last_sessioniser_run_at: str | None
    last_backup_at: str | None
    consecutive_failures: int


class DbReader(Protocol):
    """Read-only data access the conversation needs. No writes here —
    writes travel as ApplyX actions and run through days_service."""

    def today(self) -> date: ...

    def day_view(self, target_date: date) -> DayView | None: ...

    def week_totals(self) -> list[WeekDayTotal]: ...

    def year_view(self) -> YearView: ...

    def status_view(self) -> StatusView: ...


class Notifier(Protocol):
    """What a transport adapter must provide (HANDOFF 7.A)."""

    def send(self, message: OutgoingMessage) -> SentMessage: ...

    def edit(self, chat_id: int, message_id: int, message: OutgoingMessage) -> None: ...

    def answer_callback(self, callback_query_id: str, text: str | None = None) -> None: ...
