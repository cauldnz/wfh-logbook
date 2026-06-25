"""Pure conversation logic: (event, state, reader) → actions (HANDOFF 7.A).

No HTTP. No database writes. No Telegram types. The same coverage
discipline as the sessioniser applies — adjustments born here are claimed
hours (CLAUDE.md).

State machine (ARCHITECTURE §5.7):

- ``awaiting`` is best-effort: it clears on any new command, on successful
  adjustment parse, and on the 30-minute idle timeout. Clearing it can
  never corrupt data — at worst a free-text message gets the gentle hint.
- Confirmation, adjustment, and locking are returned as ApplyX actions and
  executed by the service through the SAME internal functions as the web
  UI (HANDOFF 7.G: do not bypass the internal API).
- Authorisation policy (7.E): unauthorised users get exactly ONE polite
  rejection, decided here from event.rejection_already_sent; subsequent
  events produce no actions at all.

Button-visibility rules (documented deviation): the spec's lock-callback
edit removes Adjust + Lock. A *fresh* view of an already-locked day keeps
``[✏ Adjust]`` (post-lock adjustments are spec'd versioning behaviour and
the web UI offers the same); Confirm and Lock are dropped once locked.
"""

from __future__ import annotations

from datetime import date, timedelta

from app.notifier.base import (
    AnswerCallback,
    ApplyAdjustment,
    ApplyBulkLock,
    ApplyConfirm,
    ApplyLock,
    ApplyRebuild,
    Button,
    ClearAwaiting,
    DayView,
    DbReader,
    IncomingEvent,
    OutgoingAction,
    SendMessage,
    SetAwaiting,
)
from app.notifier.grammar import Adjustment, parse_adjustment

AWAITING_TIMEOUT = timedelta(minutes=30)

REJECTION_TEXT = "This bot is private."

HELP_TEXT = (
    "WFH Logbook commands:\n"
    "/today — running total so far today\n"
    "/yesterday — review yesterday\n"
    "/day YYYY-MM-DD — review a specific day\n"
    "/week — last 7 days\n"
    "/year — financial-year total\n"
    "/status — poller / sessioniser / backup health\n"
    "/rebuild [YYYY-MM-DD|today|yesterday] — force a sessioniser run\n"
    "/lockall — lock all clean (un-flagged) backlog days\n"
    "\n"
    "Adjustments (after tapping ✏ Adjust): send a signed duration and a "
    "reason, e.g.\n"
    "  -45 lunch\n"
    "  -1h15m doctor's appointment\n"
    "  +2h poller outage, corroborated by Teams"
)

FREE_TEXT_HINT = (
    "I wasn't expecting free text. To adjust a day, open it first "
    "(/yesterday or /day YYYY-MM-DD) and tap ✏ Adjust. /help lists "
    "everything I understand."
)


def _hours(seconds: int) -> str:
    sign = "-" if seconds < 0 else ""
    seconds = abs(seconds)
    h, rem = divmod(seconds, 3600)
    m = rem // 60
    return f"{sign}{h}:{m:02d}"


def render_day_text(view: DayView, *, today: bool = False) -> str:
    """Human summary of a day. Shared by fresh views and post-apply edits."""
    label = "Today so far" if today else view.local_date.strftime("%A %d %b %Y")
    lines = [f"{label}: {_hours(view.claimed_seconds)} h claimed (v{view.version})"]
    if view.adjustment_seconds:
        lines.append(
            f"  computed {_hours(view.computed_seconds)}, "
            f"adjusted {view.adjustment_seconds // 60:+d} min "
            f"({view.adjustment_reason})"
        )
    if view.sessions:
        for s in view.sessions:
            lines.append(f"  {s.start_hhmm}-{s.end_hhmm}  {_hours(s.duration_seconds)}")
    else:
        lines.append("  no sessions recorded")
    if view.anomalous:
        lines.append("⚠ over the daily cap — worth a close look")
    if view.locked:
        lines.append("🔒 locked")
    return "\n".join(lines)


def day_buttons(view: DayView, *, today: bool = False) -> tuple[tuple[Button, ...], ...]:
    """Button rows for a day view (HANDOFF 7.F table)."""
    d = view.local_date.isoformat()
    if today:
        # The day isn't done: no Confirm, no Lock.
        return ((Button("✏ Adjust", f"adjust:{d}"),),)
    if view.locked:
        return ((Button("✏ Adjust", f"adjust:{d}"),),)
    return (
        (
            Button("✓ Confirm", f"confirm:{d}"),
            Button("✏ Adjust", f"adjust:{d}"),
            Button("🔒 Lock", f"lock:{d}"),
        ),
    )


def _show_day(reader: DbReader, target_date: date, *, today: bool = False) -> SendMessage:
    view = reader.day_view(target_date)
    if view is None:
        return SendMessage(
            text=(
                f"No data for {target_date.isoformat()} yet. The sessioniser "
                "runs nightly; /status shows pipeline health."
            )
        )
    return SendMessage(
        text=render_day_text(view, today=today), buttons=day_buttons(view, today=today)
    )


# ------------------------------------------------------------- dispatchers


def _handle_command(event: IncomingEvent, reader: DbReader) -> list[OutgoingAction]:
    cmd = (event.command or "").lower()
    actions: list[OutgoingAction] = [ClearAwaiting()]  # any command resets state

    if cmd in ("/start", "/help"):
        actions.append(SendMessage(text=HELP_TEXT))
        return actions

    if cmd == "/today":
        # Today is in flux: always rebuild before rendering so the reply is
        # current-to-the-minute, not as-of-last-night (HANDOFF 9.C amendment).
        # Idempotent; announce=False keeps the rebuild out of the reply text.
        actions.append(ApplyRebuild(target_date=reader.today(), announce=False))
        return actions

    if cmd == "/yesterday":
        actions.append(_show_day(reader, reader.today() - timedelta(days=1)))
        return actions

    if cmd == "/day":
        try:
            target = date.fromisoformat(event.args.strip())
        except ValueError:
            actions.append(SendMessage(text="Usage: /day YYYY-MM-DD, e.g. /day 2026-06-09"))
            return actions
        actions.append(_show_day(reader, target))
        return actions

    if cmd == "/week":
        totals = reader.week_totals()
        if not totals:
            actions.append(SendMessage(text="No recorded days in the last week."))
            return actions
        lines = ["Last 7 days:"]
        week_sum = 0
        for t in totals:
            lines.append(f"  {t.local_date.strftime('%a %d %b')}  {_hours(t.claimed_seconds)}")
            week_sum += t.claimed_seconds
        lines.append(f"  total  {_hours(week_sum)}")
        actions.append(SendMessage(text="\n".join(lines)))
        return actions

    if cmd == "/year":
        y = reader.year_view()
        actions.append(
            SendMessage(
                text=(
                    f"FY {y.fy_label}: {_hours(y.total_claimed_seconds)} h claimed\n"
                    f"  {y.locked_days} day(s) locked, {y.unlocked_days} unlocked"
                )
            )
        )
        return actions

    if cmd == "/rebuild":
        arg = event.args.strip().lower()
        if not arg or arg == "yesterday":
            target = reader.today() - timedelta(days=1)
        elif arg == "today":
            target = reader.today()
        else:
            try:
                target = date.fromisoformat(arg)
            except ValueError:
                actions.append(
                    SendMessage(
                        text="Usage: /rebuild [YYYY-MM-DD|today|yesterday] (defaults to yesterday)"
                    )
                )
                return actions
        actions.append(ApplyRebuild(target_date=target))
        return actions

    if cmd == "/status":
        s = reader.status_view()
        fail_note = (
            f" ({s.consecutive_failures} consecutive failures)" if s.consecutive_failures else ""
        )
        actions.append(
            SendMessage(
                text=(
                    f"Last poll: {s.last_poll_succeeded_at or 'never'}{fail_note}\n"
                    f"Last sessioniser run: {s.last_sessioniser_run_at or 'never'}\n"
                    f"Last backup: {s.last_backup_at or 'never'}"
                )
            )
        )
        return actions

    if cmd == "/lockall":
        actions.append(ApplyBulkLock())
        return actions

    actions.append(SendMessage(text=f"Unknown command {cmd}. /help lists what I understand."))
    return actions


def _handle_callback(event: IncomingEvent) -> list[OutgoingAction]:
    data = event.callback_data or ""
    qid = event.callback_query_id or ""
    verb, _, date_str = data.partition(":")
    try:
        target = date.fromisoformat(date_str)
    except ValueError:
        return [AnswerCallback(qid, "Unknown action.")]

    if verb == "confirm":
        return [
            AnswerCallback(qid, "Confirmed."),
            ApplyConfirm(target_date=target, message_id=event.message_id),
            ClearAwaiting(),
        ]
    if verb == "adjust":
        return [
            AnswerCallback(qid),
            SetAwaiting(awaiting_date=target),
            SendMessage(
                text=(
                    f"Adjusting {target.isoformat()}. Send a signed duration "
                    "and reason, e.g.\n"
                    "  -45 lunch\n"
                    "  -1h15m doctor's appointment\n"
                    "  +2h poller outage, corroborated by Teams"
                )
            ),
        ]
    if verb == "lock":
        return [
            AnswerCallback(qid, "Locked."),
            ApplyLock(target_date=target, message_id=event.message_id),
            ClearAwaiting(),
        ]
    return [AnswerCallback(qid, "Unknown action.")]


def _handle_text(
    event: IncomingEvent,
    awaiting_date: date | None,
) -> list[OutgoingAction]:
    if awaiting_date is None:
        return [SendMessage(text=FREE_TEXT_HINT)]

    result = parse_adjustment(event.text or "")
    if isinstance(result, Adjustment):
        return [
            ClearAwaiting(),
            ApplyAdjustment(
                target_date=awaiting_date,
                minutes=result.minutes,
                reason=result.reason,
                render_to="reply",
            ),
        ]
    # Parse error: stay in awaiting state so the user can just try again.
    return [SendMessage(text=result.message)]


def handle_event(
    event: IncomingEvent,
    awaiting: str | None,
    awaiting_date: date | None,
    awaiting_age: timedelta | None,
    reader: DbReader,
) -> list[OutgoingAction]:
    """The single entry point. Pure: same inputs → same actions."""
    # 7.E: one polite rejection, then silence.
    if not event.authorised:
        if event.rejection_already_sent:
            return []
        return [SendMessage(text=REJECTION_TEXT)]

    # 30-minute awaiting timeout (ARCH §4.6): stale state is cleared and the
    # event handled as if idle.
    effective_awaiting_date = awaiting_date
    timed_out = (
        awaiting == "adjustment" and awaiting_age is not None and awaiting_age > AWAITING_TIMEOUT
    )
    if awaiting != "adjustment" or timed_out:
        effective_awaiting_date = None

    if event.kind == "command":
        return _handle_command(event, reader)
    if event.kind == "callback":
        return _handle_callback(event)
    actions = _handle_text(event, effective_awaiting_date)
    if timed_out:
        actions.insert(0, ClearAwaiting())
    return actions
