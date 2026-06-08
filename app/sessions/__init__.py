"""Sessionisation: turn raw observations into daily summaries.

This package is the audit-defence core. Coverage discipline (CLAUDE.md):
effective 100% branch coverage. Every methodology rule in §4 of
docs/METHODOLOGY.md has at least one named test.

Public surface:

- ``RuleSet`` (rules.py): the immutable parameter bundle.
- ``build_sessions_for_date`` (builder.py): pure function, no DB.
- ``sessionise_date`` (persistence.py): DB-touching wrapper that replaces
  ``sessions`` rows and creates a new ``daily_summaries`` version when needed.
- ``register_scheduler_jobs`` (scheduler.py): nightly APScheduler job.
"""

from __future__ import annotations

from app.sessions.builder import ComputedSession, ObservationRecord, build_sessions_for_date
from app.sessions.persistence import sessionise_date
from app.sessions.rules import CURRENT_RULE_VERSION, RuleSet

__all__ = [
    "CURRENT_RULE_VERSION",
    "ComputedSession",
    "ObservationRecord",
    "RuleSet",
    "build_sessions_for_date",
    "sessionise_date",
]
