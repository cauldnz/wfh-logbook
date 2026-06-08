"""Pure sessionisation algorithm.

Per HANDOFF §6 Phase 3 and ARCHITECTURE §5.2-§5.3:

1. Group observations by MAC, build per-MAC connected intervals from the
   (is_connected=True ... is_connected=False) state machine.
2. Union intervals across MACs into a merged timeline (any device connected ⇒
   open interval).
3. Gap-bridge: merge adjacent intervals separated by ≤ ``gap_bridge_minutes``.
4. Drop intervals shorter than ``min_session_minutes``.
5. Attribute to ``local_date`` based on the session's *start* (midnight-
   crossing rule, METHODOLOGY §4.4).
6. Return only sessions whose attributed date matches ``target_date``.

This module touches NO database. It operates on inputs and returns outputs.
Determinism: same inputs + same ``RuleSet`` → byte-identical output.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from app.sessions.rules import RuleSet


# How far either side of the local day we look for observations, so we can
# correctly construct sessions that begin near midnight on either side.
# 12 hours is generous and adds negligible CPU since the per-MAC scan is O(n).
BUFFER_HOURS = 12


@dataclass(frozen=True, slots=True)
class ObservationRecord:
    """Pure data view of an ``observations`` row used by the builder.

    ``timestamp`` is the EFFECTIVE moment for sessionisation, defined as
    ``controller_seen_at if controller_seen_at else observed_at`` (per
    ARCHITECTURE §5.2 step 1). Caller is responsible for coalescing.
    """

    mac: str
    device_label: str
    timestamp: datetime  # tz-aware UTC
    is_connected: bool


@dataclass(frozen=True, slots=True)
class ComputedSession:
    """A computed work session prior to persistence."""

    started_at: datetime  # tz-aware UTC
    ended_at: datetime  # tz-aware UTC
    duration_seconds: int
    devices_seen: tuple[str, ...]  # device labels, sorted, deduplicated
    bridged_gaps_count: int
    bridged_gaps_seconds: int
    local_date: date  # attribution date (start-date rule)


# ----------------------------------------------------------- internal helpers


@dataclass(frozen=True, slots=True)
class _Interval:
    start: datetime
    end: datetime
    macs: frozenset[str]
    labels: frozenset[str]
    bridged_count: int = 0
    bridged_seconds: int = 0


def _per_mac_intervals(
    observations: Sequence[ObservationRecord],
) -> list[_Interval]:
    """Build per-MAC contiguous connected intervals.

    State machine per MAC:

    - Observation with ``is_connected=True`` opens an interval (if none open)
      and updates the "last-true" pointer.
    - Observation with ``is_connected=False`` closes any open interval at the
      observation's timestamp.
    - At the end, any still-open interval closes at the last-true pointer
      (the session continues beyond the data we have; we'll capture more on
      the next sessioniser run).
    """
    by_mac: dict[str, list[ObservationRecord]] = {}
    for obs in observations:
        by_mac.setdefault(obs.mac, []).append(obs)

    intervals: list[_Interval] = []
    for mac, obs_list in by_mac.items():
        obs_list_sorted = sorted(obs_list, key=lambda o: o.timestamp)
        open_start: datetime | None = None
        last_true: datetime | None = None
        label: str = obs_list_sorted[0].device_label
        for obs in obs_list_sorted:
            # Prefer the most recently seen label (handles label rename mid-stream).
            label = obs.device_label
            if obs.is_connected:
                if open_start is None:
                    open_start = obs.timestamp
                last_true = obs.timestamp
            else:
                if open_start is not None:
                    intervals.append(
                        _Interval(
                            start=open_start,
                            end=obs.timestamp,
                            macs=frozenset({mac}),
                            labels=frozenset({label}),
                        )
                    )
                open_start = None
                last_true = None
        if open_start is not None and last_true is not None:
            intervals.append(
                _Interval(
                    start=open_start,
                    end=last_true,
                    macs=frozenset({mac}),
                    labels=frozenset({label}),
                )
            )
    return intervals


def _union(intervals: Iterable[_Interval]) -> list[_Interval]:
    """Sweep-line union across per-MAC intervals.

    Two intervals "touching" at the same instant (one ends exactly when the
    next starts) are merged into one. Start events sort before end events at
    the same instant so this works.
    """
    events: list[tuple[datetime, int, _Interval]] = []
    for ivl in intervals:
        events.append((ivl.start, 0, ivl))  # 0 = start
        events.append((ivl.end, 1, ivl))  # 1 = end
    events.sort(key=lambda e: (e[0], e[1]))

    out: list[_Interval] = []
    open_count = 0
    cur_start: datetime | None = None
    cur_macs: set[str] = set()
    cur_labels: set[str] = set()

    for t, kind, ivl in events:
        if kind == 0:  # start
            if open_count == 0:
                cur_start = t
                cur_macs = set()
                cur_labels = set()
            open_count += 1
            cur_macs |= set(ivl.macs)
            cur_labels |= set(ivl.labels)
        else:  # end
            open_count -= 1
            if open_count == 0 and cur_start is not None:
                out.append(
                    _Interval(
                        start=cur_start,
                        end=t,
                        macs=frozenset(cur_macs),
                        labels=frozenset(cur_labels),
                    )
                )
                cur_start = None
    return out


def _gap_bridge(intervals: list[_Interval], gap_bridge_minutes: int) -> list[_Interval]:
    """Merge adjacent intervals separated by ≤ ``gap_bridge_minutes``.

    Increments ``bridged_count`` and ``bridged_seconds`` on the resulting
    session (METHODOLOGY §4.2).
    """
    if not intervals:
        return []
    threshold = timedelta(minutes=gap_bridge_minutes)
    out: list[_Interval] = []
    cur = intervals[0]
    for nxt in intervals[1:]:
        gap = nxt.start - cur.end
        if gap <= threshold:
            cur = _Interval(
                start=cur.start,
                end=nxt.end,
                macs=cur.macs | nxt.macs,
                labels=cur.labels | nxt.labels,
                bridged_count=cur.bridged_count + 1,
                bridged_seconds=cur.bridged_seconds + int(gap.total_seconds()),
            )
        else:
            out.append(cur)
            cur = nxt
    out.append(cur)
    return out


def _filter_too_short(intervals: list[_Interval], min_session_minutes: int) -> list[_Interval]:
    """Drop intervals shorter than ``min_session_minutes`` (METHODOLOGY §4.3)."""
    threshold = timedelta(minutes=min_session_minutes)
    return [ivl for ivl in intervals if (ivl.end - ivl.start) >= threshold]


def _attribute_local_date(started_at_utc: datetime, tz_name: str) -> date:
    """The local-calendar date on which the session began (METHODOLOGY §4.4)."""
    tz = ZoneInfo(tz_name)
    return started_at_utc.astimezone(tz).date()


# ----------------------------------------------------------------- public API


def build_sessions_for_date(
    target_date: date,
    observations: Sequence[ObservationRecord],
    rules: RuleSet,
) -> list[ComputedSession]:
    """Compute sessions attributed to ``target_date``.

    ``observations`` may include rows from before/after ``target_date`` — the
    builder filters by attribution at the end. Caller is responsible for
    loading observations across a sufficient buffer (see ``utc_buffer_for``).

    The output is deterministic and reproducible from ``(observations, rules)``
    alone.
    """
    per_mac = _per_mac_intervals(observations)
    merged = _union(per_mac)
    merged.sort(key=lambda i: i.start)
    bridged = _gap_bridge(merged, rules.gap_bridge_minutes)
    long_enough = _filter_too_short(bridged, rules.min_session_minutes)

    sessions: list[ComputedSession] = []
    for ivl in long_enough:
        attributed = _attribute_local_date(ivl.start, rules.local_timezone)
        if attributed != target_date:
            continue
        duration = int((ivl.end - ivl.start).total_seconds())
        sessions.append(
            ComputedSession(
                started_at=ivl.start,
                ended_at=ivl.end,
                duration_seconds=duration,
                devices_seen=tuple(sorted(ivl.labels)),
                bridged_gaps_count=ivl.bridged_count,
                bridged_gaps_seconds=ivl.bridged_seconds,
                local_date=attributed,
            )
        )
    # Stable ordering for deterministic persistence.
    sessions.sort(key=lambda s: s.started_at)
    return sessions


def utc_buffer_for(target_date: date, tz_name: str) -> tuple[datetime, datetime]:
    """The UTC bounds to load observations for ``target_date`` with buffer.

    Returns ``(utc_lower_bound, utc_upper_bound)`` inclusive of buffer hours
    on either side. See ``BUFFER_HOURS``.
    """
    tz = ZoneInfo(tz_name)
    local_start = datetime.combine(target_date, time.min, tzinfo=tz)
    local_end = local_start + timedelta(days=1)
    buf = timedelta(hours=BUFFER_HOURS)
    return (local_start.astimezone(UTC) - buf, local_end.astimezone(UTC) + buf)


def computed_seconds_total(sessions: Iterable[ComputedSession]) -> int:
    """Sum of session durations in seconds."""
    return sum(s.duration_seconds for s in sessions)
