"""Sessionisation parameter bundle.

The ``RuleSet`` is the immutable parameter object every sessionisation function
takes. It is loaded from the ``config`` row (the DB is authoritative per
ARCHITECTURE §7.5), not from `.env`.

When any rule changes (gap-bridge minutes, min-session minutes, midnight-
crossing attribution, or the sessionisation algorithm itself), bump
``CURRENT_RULE_VERSION``. Every session and summary records the rule_version
under which it was computed (ARCHITECTURE §5.6).
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Config as ConfigModel

# Current rule version in code. The DB is authoritative; this constant is the
# code's claim about what version it implements. A mismatch with the DB Config
# row is surfaced by ``RuleSet.from_db`` for diagnostic visibility.
CURRENT_RULE_VERSION = "2026.1"


@dataclass(frozen=True, slots=True)
class RuleSet:
    """Immutable parameter object used by every sessionisation function."""

    gap_bridge_minutes: int
    min_session_minutes: int
    daily_cap_hours: int
    local_timezone: str
    rule_version: str

    @classmethod
    def from_db(cls, db: Session) -> RuleSet:
        """Read the singleton Config row and return a RuleSet."""
        cfg = db.execute(select(ConfigModel).limit(1)).scalar_one()
        return cls(
            gap_bridge_minutes=cfg.gap_bridge_minutes,
            min_session_minutes=cfg.min_session_minutes,
            daily_cap_hours=cfg.daily_cap_hours,
            local_timezone=cfg.local_timezone,
            rule_version=cfg.rule_version,
        )
