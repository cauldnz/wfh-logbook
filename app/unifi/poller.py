"""Poll the controller and write observations (ARCHITECTURE §5.1).

Every cycle:

1. ``list_active_clients()`` from the adapter.
2. Filter to wireless clients on the work SSID whose MAC matches an active
   row in ``devices``.
3. Write an ``observations`` row with ``is_connected=1`` per such client.
4. Disconnect transitions: any tracked MAC whose **latest stored
   observation** says connected but which is absent from this poll gets an
   ``is_connected=0`` row. Deriving the previous state from the DB (rather
   than in-process memory) means a restart cannot swallow a disconnect.
5. Update the ``poller_state`` singleton for /api/health.

Failures are logged and counted; the next cycle proceeds (HANDOFF Phase 2:
transient HTTP failures must not crash the process).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import get_sessionmaker
from app.models import Device, Observation, PollerState
from app.unifi.base import ClientObservation, ControllerAdapter, UniFiAuthError, UniFiError

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PollResult:
    connected_count: int
    disconnect_transitions: int
    duration_seconds: float


def _utcnow() -> datetime:
    return datetime.now(UTC)


def active_device_labels(db: Session) -> dict[str, str]:
    """Mapping of active tracked MAC (lower) → label."""
    rows = db.execute(select(Device).where(Device.active_to.is_(None))).scalars()
    return {d.mac.lower(): d.label for d in rows}


def _latest_connection_state(db: Session, macs: list[str]) -> dict[str, Observation]:
    """Latest observation row per MAC (any is_connected value)."""
    out: dict[str, Observation] = {}
    for mac in macs:
        row = db.execute(
            select(Observation)
            .where(Observation.mac == mac)
            .order_by(Observation.observed_at.desc(), Observation.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        if row is not None:
            out[mac] = row
    return out


def poll_once(
    db: Session,
    adapter: ControllerAdapter,
    work_ssid: str,
    now: datetime | None = None,
) -> PollResult:
    """One poll cycle. Caller commits."""
    started = time.monotonic()
    now = now or _utcnow()

    devices = active_device_labels(db)
    if not devices:
        logger.warning("poller: no active devices configured; nothing to track")

    clients = adapter.list_active_clients()
    work_clients: dict[str, ClientObservation] = {
        c.mac: c for c in clients if not c.is_wired and c.ssid == work_ssid and c.mac in devices
    }

    # Previous state BEFORE inserting this cycle's rows.
    previous = _latest_connection_state(db, list(devices.keys()))

    for mac, c in sorted(work_clients.items()):
        db.add(
            Observation(
                observed_at=now,
                controller_seen_at=c.last_seen,
                mac=mac,
                device_label=devices[mac],
                ssid=work_ssid,
                is_connected=True,
                signal_dbm=c.signal_dbm,
                raw_json=json.dumps(c.raw, separators=(",", ":")),
            )
        )

    transitions = 0
    for mac, prev in previous.items():
        if bool(prev.is_connected) and mac not in work_clients:
            # Vanished since the last stored state → disconnect transition.
            # controller_seen_at carries the last moment the controller
            # actually saw the device (ARCHITECTURE §5.1 step 4).
            db.add(
                Observation(
                    observed_at=now,
                    controller_seen_at=prev.controller_seen_at,
                    mac=mac,
                    device_label=devices[mac],
                    ssid=work_ssid,
                    is_connected=False,
                    signal_dbm=None,
                    raw_json=json.dumps(
                        {"transition": "disconnect", "derived_from_observation_id": prev.id},
                        separators=(",", ":"),
                    ),
                )
            )
            transitions += 1

    duration = time.monotonic() - started
    return PollResult(
        connected_count=len(work_clients),
        disconnect_transitions=transitions,
        duration_seconds=duration,
    )


def _update_poller_state(db: Session, *, attempted: datetime, succeeded: bool) -> None:
    state = db.execute(select(PollerState).limit(1)).scalar_one_or_none()
    if state is None:
        state = PollerState(consecutive_failures=0)
        db.add(state)
    state.last_poll_attempted_at = attempted
    if succeeded:
        state.last_poll_succeeded_at = attempted
        state.consecutive_failures = 0
    else:
        state.consecutive_failures += 1


class Poller:
    """Holds the adapter (and its HTTP session) across cycles."""

    def __init__(self, adapter: ControllerAdapter, work_ssid: str) -> None:
        self._adapter = adapter
        self._work_ssid = work_ssid
        self._logged_in = False

    def run_cycle(self) -> None:
        """The scheduled callable. Never raises (logs instead)."""
        attempted = _utcnow()
        SessionLocal = get_sessionmaker()  # noqa: N806 (SQLAlchemy convention)
        with SessionLocal() as db:
            try:
                if not self._logged_in:
                    self._adapter.login()
                    self._logged_in = True
                result = poll_once(db, self._adapter, self._work_ssid, now=attempted)
                _update_poller_state(db, attempted=attempted, succeeded=True)
                db.commit()
                logger.info(
                    "poller: ok connected=%d transitions=%d duration=%.2fs",
                    result.connected_count,
                    result.disconnect_transitions,
                    result.duration_seconds,
                )
            except UniFiAuthError:
                db.rollback()
                self._logged_in = False  # force fresh login next cycle
                _update_poller_state(db, attempted=attempted, succeeded=False)
                db.commit()
                logger.exception("poller: authentication failed")
            except UniFiError:
                db.rollback()
                _update_poller_state(db, attempted=attempted, succeeded=False)
                db.commit()
                logger.warning("poller: cycle failed; next cycle will retry", exc_info=True)
            except Exception:
                db.rollback()
                _update_poller_state(db, attempted=attempted, succeeded=False)
                db.commit()
                logger.exception("poller: unexpected error")


def register_poller_job(
    scheduler: BackgroundScheduler,
    adapter: ControllerAdapter,
    settings: Settings,
    work_ssid: str,
) -> Poller:
    poller = Poller(adapter, work_ssid)
    scheduler.add_job(
        poller.run_cycle,
        trigger=IntervalTrigger(seconds=settings.poll_interval_seconds),
        id="unifi_poller",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    logger.info(
        "scheduler: unifi_poller registered (every %ds, ssid=%s)",
        settings.poll_interval_seconds,
        work_ssid,
    )
    return poller
