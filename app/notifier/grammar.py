"""Adjustment-string parser (HANDOFF 7.F grammar).

    ADJUSTMENT := SIGN DURATION WS REASON
    SIGN       := '+' | '-'
    DURATION   := minutes | 'NNm' | 'Hh' | 'HhMMm' | 'H:MM'
    REASON     := non-empty free text, max 200 chars

Pure function; no I/O. This parser converts free text into claimed hours,
so it is deliberately strict: anything ambiguous is rejected with a
helpful message rather than guessed at.

Conservative choices beyond the literal spec (each called out):

- TODO(spec): the spec says an unsigned duration defaults to '-' "if it
  looks negative-ish, otherwise reject". "Negative-ish" is undefined, so
  unsigned input is ALWAYS rejected with a hint to add an explicit sign.
  Silent sign-guessing on a tax record is the wrong place to be clever.
- TODO(spec): zero-magnitude adjustments are rejected — confirming a day
  unchanged is what the Confirm button is for, and it records a proper
  zero-adjustment version through that path.
- TODO(spec): durations over 24 hours are rejected as implausible for a
  single-day adjustment.
- Typographic minus lookalikes (U+2212 MINUS SIGN, U+2013 EN DASH,
  U+2014 EM DASH) are rejected by name, per the spec's explicit example —
  phones autocorrect '-' into these, and silently accepting them would
  make the rejection rule untestable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_REASON_CHARS = 200
MAX_MINUTES = 24 * 60

# Characters phones substitute for ASCII hyphen-minus. Rejected by name.
# Written as escapes so the lookalikes are explicit (and lint-friendly).
_TYPOGRAPHIC_MINUS = {
    "−": "minus sign (U+2212)",
    "–": "en-dash (U+2013)",
    "—": "em-dash (U+2014)",
}

_DURATION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^(\d+)h(\d{1,2})m$"), "h_m"),  # 1h15m
    (re.compile(r"^(\d+):(\d{2})$"), "h_colon_m"),  # 1:30
    (re.compile(r"^(\d+)h$"), "h"),  # 2h
    (re.compile(r"^(\d+)m$"), "m"),  # 45m
    (re.compile(r"^(\d+)$"), "bare"),  # 45
]


@dataclass(frozen=True, slots=True)
class Adjustment:
    """A successfully parsed adjustment."""

    minutes: int  # signed
    reason: str


@dataclass(frozen=True, slots=True)
class ParseError:
    """A rejection, with a message suitable for sending straight back."""

    message: str


def _parse_duration_token(token: str) -> int | None:
    """Token (sign stripped) → unsigned minutes, or None if not a duration.

    In the combined forms (HhMMm, H:MM) the minutes component must be
    0-59 — `1h75m` is far more likely a typo than 135 deliberate minutes,
    and a tax-record parser guesses in nobody's favour.
    """
    for pattern, kind in _DURATION_PATTERNS:
        m = pattern.match(token)
        if not m:
            continue
        if kind in ("h_m", "h_colon_m"):
            minute_part = int(m.group(2))
            if minute_part > 59:
                return None
            return int(m.group(1)) * 60 + minute_part
        if kind == "h":
            return int(m.group(1)) * 60
        # "m" and "bare" are both plain minutes.
        return int(m.group(1))
    return None


def parse_adjustment(text: str) -> Adjustment | ParseError:
    """Parse an adjustment string per the HANDOFF 7.F grammar."""
    stripped = text.strip()
    if not stripped:
        return ParseError("Empty message. Send e.g. -45 lunch  or  +1h30m poller outage.")

    # Reject typographic minus lookalikes loudly (spec example: U+2212 '45 lunch').
    first_char = stripped[0]
    if first_char in _TYPOGRAPHIC_MINUS:
        return ParseError(
            f"That leading character is an {_TYPOGRAPHIC_MINUS[first_char]}, "
            "not a minus. Phones often autocorrect this — please retype "
            "using a plain '-' (or '+'), e.g. -45 lunch."
        )

    head, _, tail = stripped.partition(" ")
    reason = tail.strip()

    if first_char in "+-":
        sign = 1 if first_char == "+" else -1
        duration_token = head[1:]
    else:
        # Unsigned: the spec's "default '-' if negative-ish" is undefined;
        # conservative parser demands an explicit sign. TODO(spec).
        if _parse_duration_token(head) is not None:
            return ParseError(
                "Missing sign. Use -45 lunch to deduct or +45 reason to add — "
                "an explicit + or - is required."
            )
        return ParseError(
            "I couldn't find a duration at the start. Format: "
            "-<duration> <reason>, e.g. -45 lunch, -1h15m appointment, "
            "+2h corroborated by Teams."
        )

    minutes = _parse_duration_token(duration_token)
    if minutes is None:
        return ParseError(
            f"{head!r} isn't a duration I understand. Use minutes (-45), "
            "-45m, -2h, -1h15m, or -1:30, followed by a reason."
        )

    if minutes == 0:
        return ParseError(
            "Zero-length adjustment. If the day is correct as computed, "
            "use the Confirm button instead — that records the confirmation "
            "properly."
        )
    if minutes > MAX_MINUTES:
        return ParseError(
            f"{minutes} minutes is more than 24 hours — that can't be right "
            "for a single day. Check the duration."
        )

    if not reason:
        return ParseError(
            "A reason is required (it goes in the audit record). E.g. -45 lunch with the kids."
        )
    if len(reason) > MAX_REASON_CHARS:
        return ParseError(
            f"Reason is too long ({len(reason)} chars; max {MAX_REASON_CHARS}). "
            "A sentence is plenty."
        )

    return Adjustment(minutes=sign * minutes, reason=reason)
