"""Adjustment grammar (HANDOFF 7.F) — audit-defence coverage.

Every example in the spec table appears here verbatim, plus the
conservative-rejection paths the parser documents as TODO(spec).
"""

from __future__ import annotations

import pytest

from app.notifier.grammar import (
    MAX_REASON_CHARS,
    Adjustment,
    ParseError,
    parse_adjustment,
)

# ------------------------------------------------------- spec: must parse


@pytest.mark.parametrize(
    ("text", "minutes", "reason"),
    [
        ("-45 lunch", -45, "lunch"),
        ("+30 poller outage 9-11", 30, "poller outage 9-11"),
        ("-1h15m doctor's appointment", -75, "doctor's appointment"),
        ("-1:30 GP visit", -90, "GP visit"),
        ("+2h corroborated by Teams", 120, "corroborated by Teams"),
        # Additional formats from the DURATION grammar:
        ("-45m walking the dog", -45, "walking the dog"),
        ("+1h0m rounding fix", 60, "rounding fix"),
        ("-0:05 stepped away", -5, "stepped away"),
        # Whitespace tolerance:
        ("  -45 lunch  ", -45, "lunch"),
        ("-45  double  spaced  reason", -45, "double  spaced  reason"),
    ],
)
def test_valid_adjustments_parse(text: str, minutes: int, reason: str) -> None:
    result = parse_adjustment(text)
    assert isinstance(result, Adjustment), getattr(result, "message", "")
    assert result.minutes == minutes
    assert result.reason == reason


# ----------------------------------------------------- spec: must reject


@pytest.mark.parametrize(
    ("text", "hint_fragment"),
    [
        # The four spec examples:
        ("confirm but I left at 4", "duration"),
        ("-45", "reason"),
        ("lunch -45", "duration"),
        ("−45 lunch", "minus"),  # U+2212 MINUS SIGN, the spec example
        # Lookalike family:
        ("–45 lunch", "dash"),  # U+2013 EN DASH
        ("—45 lunch", "dash"),  # U+2014 EM DASH
        # Conservative rejections (TODO(spec) documented in grammar.py):
        ("45 lunch", "sign"),  # unsigned
        ("-0 lunch", "Confirm"),  # zero magnitude
        ("+0h whatever", "Confirm"),
        ("-1441 marathon", "24 hours"),  # > 24h
        ("-25h too long", "24 hours"),
        # Malformed durations:
        ("-1h75m typo", "duration"),  # minutes component must be 0-59
        ("-1:99 typo", "duration"),
        ("-1:5 short-minutes", "duration"),
        ("-h lunch", "duration"),
        ("-1hh lunch", "duration"),
        ("- 45 lunch", "duration"),  # space between sign and duration
        ("", "Empty"),
        ("   ", "Empty"),
    ],
)
def test_invalid_adjustments_reject_helpfully(text: str, hint_fragment: str) -> None:
    result = parse_adjustment(text)
    assert isinstance(result, ParseError), f"{text!r} should have been rejected"
    assert hint_fragment.lower() in result.message.lower(), (
        f"{text!r} → {result.message!r} (expected hint {hint_fragment!r})"
    )


def test_minutes_component_at_59_boundary_valid() -> None:
    """1h59m parses; 1h60m+ is rejected (minutes component range check)."""
    ok = parse_adjustment("-1h59m boundary")
    assert isinstance(ok, Adjustment)
    assert ok.minutes == -119
    bad = parse_adjustment("-1h60m boundary")
    assert isinstance(bad, ParseError)


def test_reason_max_length_boundary() -> None:
    ok = parse_adjustment(f"-45 {'x' * MAX_REASON_CHARS}")
    assert isinstance(ok, Adjustment)
    too_long = parse_adjustment(f"-45 {'x' * (MAX_REASON_CHARS + 1)}")
    assert isinstance(too_long, ParseError)
    assert "too long" in too_long.message.lower()


def test_reason_preserves_inner_punctuation() -> None:
    result = parse_adjustment("+30 poller outage 09:00-11:00; corroborated by Teams")
    assert isinstance(result, Adjustment)
    assert result.reason == "poller outage 09:00-11:00; corroborated by Teams"


def test_minutes_arithmetic_h_m_combo() -> None:
    result = parse_adjustment("-2h45m long appointment")
    assert isinstance(result, Adjustment)
    assert result.minutes == -(2 * 60 + 45)


def test_exactly_24h_allowed_but_over_rejected() -> None:
    ok = parse_adjustment("-24h whole day correction")
    assert isinstance(ok, Adjustment)
    assert ok.minutes == -1440
    over = parse_adjustment("-1441 over the cap")
    assert isinstance(over, ParseError)
