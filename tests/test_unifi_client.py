"""UniFi adapter + poller tests (HANDOFF §6 Phase 2).

Hermetic: HTTP is mocked with respx; the response bodies come from the
committed sanitised fixture captured from a real UDM controller
(tests/fixtures/unifi_clients_active.json). Field references in assertions
deliberately mirror that fixture — if the fixture is recaptured and the
schema moved, these tests should break loudly.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Device, Observation, PollerState
from app.unifi.base import (
    ClientObservation,
    UniFiAuthError,
    UniFiError,
    UnsupportedControllerError,
)
from app.unifi.client import create_adapter
from app.unifi.poller import Poller, poll_once
from app.unifi.udm import UDMAdapter, _normalise_client

FIXTURE = Path(__file__).parent / "fixtures" / "unifi_clients_active.json"
HOST = "https://192.168.1.1"

# Values from the committed fixture (sanitised capture of a real controller).
WORK_MAC = "aa:bb:cc:00:00:0a"
WORK_SSID = "WFH"
WORK_LAST_SEEN_EPOCH = 1781085568
WORK_SIGNAL = -84


@pytest.fixture
def fixture_payload() -> dict:  # type: ignore[type-arg]
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def make_settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "unifi_host": HOST,
        "unifi_site": "default",
        "unifi_username": "u",
        "unifi_password": "p",
        "unifi_verify_tls": False,
        "unifi_api_flavour": "auto",
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


# =========================================================== normalisation


class TestNormalisation:
    """Field mapping against the REAL captured shape — the Phase 2 core."""

    def test_work_client_normalises_with_fixture_values(self, fixture_payload: dict) -> None:  # type: ignore[type-arg]
        rows = [_normalise_client(c) for c in fixture_payload["data"]]
        work = [r for r in rows if r.ssid == WORK_SSID]
        assert len(work) == 1
        c = work[0]
        assert c.mac == WORK_MAC
        assert c.last_seen == datetime.fromtimestamp(WORK_LAST_SEEN_EPOCH, tz=UTC)
        assert c.signal_dbm == WORK_SIGNAL
        assert c.is_wired is False
        assert c.hostname == "device-10"
        # Raw payload preserved verbatim for the observations raw_json column.
        assert c.raw["essid"] == WORK_SSID

    def test_all_fixture_clients_normalise_without_error(self, fixture_payload: dict) -> None:  # type: ignore[type-arg]
        rows = [_normalise_client(c) for c in fixture_payload["data"]]
        assert len(rows) == 22
        # Every wireless client in the capture carries essid + last_seen.
        assert all(r.ssid is not None for r in rows)
        assert all(r.last_seen is not None for r in rows)
        assert all(r.last_seen.tzinfo is not None for r in rows if r.last_seen)

    def test_missing_optional_fields_tolerated(self) -> None:
        # A minimal dict (defensive path) — only mac present.
        c = _normalise_client({"mac": "AA:BB:CC:DD:EE:FF"})
        assert c.mac == "aa:bb:cc:dd:ee:ff"  # lower-cased
        assert c.ssid is None
        assert c.last_seen is None
        assert c.signal_dbm is None
        assert c.is_wired is False


# =============================================================== adapter


class TestUDMAdapter:
    @respx.mock
    def test_login_then_list(self, fixture_payload: dict) -> None:  # type: ignore[type-arg]
        respx.post(f"{HOST}/api/auth/login").mock(
            return_value=httpx.Response(200, headers={"x-csrf-token": "tok123"})
        )
        route = respx.get(f"{HOST}/proxy/network/api/s/default/stat/sta").mock(
            return_value=httpx.Response(200, json=fixture_payload)
        )
        adapter = UDMAdapter(HOST, "u", "p")
        adapter.login()
        clients = adapter.list_active_clients()
        assert len(clients) == 22
        # CSRF token from login is echoed on the read.
        assert route.calls.last.request.headers["x-csrf-token"] == "tok123"
        adapter.close()

    @respx.mock
    def test_login_rejected_raises_auth_error(self) -> None:
        respx.post(f"{HOST}/api/auth/login").mock(return_value=httpx.Response(401))
        adapter = UDMAdapter(HOST, "u", "wrong")
        with pytest.raises(UniFiAuthError):
            adapter.login()
        adapter.close()

    @respx.mock
    def test_session_expiry_triggers_relogin_and_retry(self, fixture_payload: dict) -> None:  # type: ignore[type-arg]
        login_route = respx.post(f"{HOST}/api/auth/login").mock(
            return_value=httpx.Response(200, headers={"x-csrf-token": "tok"})
        )
        sta = respx.get(f"{HOST}/proxy/network/api/s/default/stat/sta").mock(
            side_effect=[
                httpx.Response(401),
                httpx.Response(200, json=fixture_payload),
            ]
        )
        adapter = UDMAdapter(HOST, "u", "p")
        adapter.login()
        clients = adapter.list_active_clients()
        assert len(clients) == 22
        assert login_route.call_count == 2  # initial + re-login
        assert sta.call_count == 2
        adapter.close()

    @respx.mock
    def test_unexpected_shape_raises_with_guidance(self) -> None:
        respx.post(f"{HOST}/api/auth/login").mock(
            return_value=httpx.Response(200, headers={"x-csrf-token": "tok"})
        )
        respx.get(f"{HOST}/proxy/network/api/s/default/stat/sta").mock(
            return_value=httpx.Response(200, json={"surprise": True})
        )
        adapter = UDMAdapter(HOST, "u", "p")
        adapter.login()
        with pytest.raises(UniFiError, match="fetch_unifi_sample"):
            adapter.list_active_clients()
        adapter.close()


# ================================================================ factory


class TestAdapterFactory:
    @respx.mock
    def test_auto_detects_udm(self) -> None:
        respx.post(f"{HOST}/api/auth/login").mock(return_value=httpx.Response(401))
        adapter = create_adapter(make_settings(unifi_api_flavour="auto"))
        assert isinstance(adapter, UDMAdapter)
        adapter.close()

    @respx.mock
    def test_auto_detects_classic_and_rejects_with_guidance(self) -> None:
        respx.post(f"{HOST}/api/auth/login").mock(return_value=httpx.Response(404))
        respx.post(f"{HOST}/api/login").mock(return_value=httpx.Response(400))
        with pytest.raises(UnsupportedControllerError, match="fetch_unifi_sample"):
            create_adapter(make_settings(unifi_api_flavour="auto"))

    def test_flavour_classic_rejects_without_probing(self) -> None:
        with pytest.raises(UnsupportedControllerError):
            create_adapter(make_settings(unifi_api_flavour="classic"))

    def test_flavour_udm_skips_probe(self) -> None:
        # No respx mock active — any HTTP call would error. udm flavour
        # must not probe.
        adapter = create_adapter(make_settings(unifi_api_flavour="udm"))
        assert isinstance(adapter, UDMAdapter)
        adapter.close()

    @respx.mock
    def test_auto_neither_endpoint_raises(self) -> None:
        respx.post(f"{HOST}/api/auth/login").mock(return_value=httpx.Response(404))
        respx.post(f"{HOST}/api/login").mock(return_value=httpx.Response(404))
        with pytest.raises(UniFiError, match="neither"):
            create_adapter(make_settings(unifi_api_flavour="auto"))

    def test_host_scheme_autofixed(self) -> None:
        adapter = create_adapter(make_settings(unifi_host="192.168.1.1", unifi_api_flavour="udm"))
        assert isinstance(adapter, UDMAdapter)
        assert adapter._host == "https://192.168.1.1"
        adapter.close()


# ============================================== poller (fake controller)


class StubAdapter:
    """In-memory ControllerAdapter for poller integration tests."""

    def __init__(self, clients: list[ClientObservation] | Exception) -> None:
        self._clients = clients
        self.login_calls = 0

    def login(self) -> None:
        self.login_calls += 1

    def list_active_clients(self) -> list[ClientObservation]:
        if isinstance(self._clients, Exception):
            raise self._clients
        return self._clients

    def close(self) -> None:
        pass

    def set_clients(self, clients: list[ClientObservation] | Exception) -> None:
        self._clients = clients


def work_client(
    mac: str = WORK_MAC,
    ssid: str = WORK_SSID,
    last_seen: datetime | None = None,
    is_wired: bool = False,
) -> ClientObservation:
    return ClientObservation(
        mac=mac,
        ssid=ssid,
        last_seen=last_seen or datetime(2026, 6, 9, 9, 0, tzinfo=UTC),
        signal_dbm=-60,
        is_wired=is_wired,
        hostname="iPhone",
        raw={"essid": ssid, "mac": mac},
    )


@pytest.fixture
def tracked_device(db_session: Session) -> Device:
    d = Device(
        mac=WORK_MAC,
        label="iPhone",
        active_from=datetime(2026, 1, 1, tzinfo=UTC),
        active_to=None,
    )
    db_session.add(d)
    db_session.commit()
    return d


class TestPollOnce:
    def test_connected_client_writes_observation(
        self, db_session: Session, tracked_device: Device
    ) -> None:
        adapter = StubAdapter([work_client()])
        now = datetime(2026, 6, 9, 9, 1, tzinfo=UTC)
        result = poll_once(db_session, adapter, WORK_SSID, now=now)
        db_session.commit()

        assert result.connected_count == 1
        assert result.disconnect_transitions == 0
        rows = db_session.execute(select(Observation)).scalars().all()
        assert len(rows) == 1
        obs = rows[0]
        assert obs.mac == WORK_MAC
        assert obs.device_label == "iPhone"
        assert obs.ssid == WORK_SSID
        assert bool(obs.is_connected) is True
        assert obs.signal_dbm == -60
        # Raw payload persisted for forensic use.
        assert json.loads(obs.raw_json)["essid"] == WORK_SSID

    def test_untracked_mac_ignored(self, db_session: Session, tracked_device: Device) -> None:
        adapter = StubAdapter([work_client(mac="ff:ff:ff:ff:ff:ff")])
        result = poll_once(db_session, adapter, WORK_SSID)
        db_session.commit()
        assert result.connected_count == 0
        assert db_session.execute(select(Observation)).scalars().all() == []

    def test_other_ssid_ignored(self, db_session: Session, tracked_device: Device) -> None:
        adapter = StubAdapter([work_client(ssid="OTHER-SSID")])
        result = poll_once(db_session, adapter, WORK_SSID)
        db_session.commit()
        assert result.connected_count == 0

    def test_wired_client_ignored(self, db_session: Session, tracked_device: Device) -> None:
        adapter = StubAdapter([work_client(is_wired=True)])
        result = poll_once(db_session, adapter, WORK_SSID)
        db_session.commit()
        assert result.connected_count == 0

    def test_disconnect_transition_row_written(
        self, db_session: Session, tracked_device: Device
    ) -> None:
        """ARCHITECTURE §5.1 step 4: present last poll, absent now → 0-row."""
        seen = datetime(2026, 6, 9, 9, 0, tzinfo=UTC)
        adapter = StubAdapter([work_client(last_seen=seen)])
        poll_once(db_session, adapter, WORK_SSID, now=seen + timedelta(minutes=1))
        db_session.commit()

        adapter.set_clients([])  # device vanished
        result = poll_once(db_session, adapter, WORK_SSID, now=seen + timedelta(minutes=2))
        db_session.commit()

        assert result.disconnect_transitions == 1
        rows = db_session.execute(select(Observation).order_by(Observation.id)).scalars().all()
        assert len(rows) == 2
        transition = rows[1]
        assert bool(transition.is_connected) is False
        # controller_seen_at carries the LAST moment the controller saw it.
        assert transition.controller_seen_at is not None
        raw = json.loads(transition.raw_json)
        assert raw["transition"] == "disconnect"
        assert raw["derived_from_observation_id"] == rows[0].id

    def test_no_duplicate_disconnect_rows(
        self, db_session: Session, tracked_device: Device
    ) -> None:
        """A device that stays gone produces exactly ONE transition row."""
        now = datetime(2026, 6, 9, 9, 0, tzinfo=UTC)
        adapter = StubAdapter([work_client()])
        poll_once(db_session, adapter, WORK_SSID, now=now)
        db_session.commit()
        adapter.set_clients([])
        poll_once(db_session, adapter, WORK_SSID, now=now + timedelta(minutes=1))
        db_session.commit()
        result3 = poll_once(db_session, adapter, WORK_SSID, now=now + timedelta(minutes=2))
        db_session.commit()
        assert result3.disconnect_transitions == 0
        rows = db_session.execute(select(Observation)).scalars().all()
        assert len(rows) == 2  # one connect, one disconnect — no more

    def test_reconnect_after_disconnect(self, db_session: Session, tracked_device: Device) -> None:
        now = datetime(2026, 6, 9, 9, 0, tzinfo=UTC)
        adapter = StubAdapter([work_client()])
        # Commit between cycles, as Poller.run_cycle does in production —
        # the session has autoflush=False, so state queries only see
        # committed rows.
        poll_once(db_session, adapter, WORK_SSID, now=now)
        db_session.commit()
        adapter.set_clients([])
        poll_once(db_session, adapter, WORK_SSID, now=now + timedelta(minutes=1))
        db_session.commit()
        adapter.set_clients([work_client(last_seen=now + timedelta(minutes=5))])
        result = poll_once(db_session, adapter, WORK_SSID, now=now + timedelta(minutes=5))
        db_session.commit()
        assert result.connected_count == 1
        rows = db_session.execute(select(Observation).order_by(Observation.id)).scalars().all()
        assert [bool(r.is_connected) for r in rows] == [True, False, True]


class TestPollerCycle:
    """Poller.run_cycle: state tracking + failure tolerance."""

    def test_success_updates_poller_state(
        self, db_session: Session, tracked_device: Device
    ) -> None:
        poller = Poller(StubAdapter([work_client()]), WORK_SSID)
        poller.run_cycle()
        state = db_session.execute(select(PollerState)).scalar_one()
        assert state.last_poll_attempted_at is not None
        assert state.last_poll_succeeded_at is not None
        assert state.consecutive_failures == 0

    def test_failure_increments_consecutive_failures_and_does_not_crash(
        self, db_session: Session, tracked_device: Device
    ) -> None:
        adapter = StubAdapter(UniFiError("controller offline"))
        poller = Poller(adapter, WORK_SSID)
        poller.run_cycle()  # must not raise (HANDOFF Phase 2 acceptance)
        poller.run_cycle()
        db_session.expire_all()
        state = db_session.execute(select(PollerState)).scalar_one()
        assert state.consecutive_failures == 2
        assert state.last_poll_succeeded_at is None

    def test_recovery_resets_failure_count(
        self, db_session: Session, tracked_device: Device
    ) -> None:
        adapter = StubAdapter(UniFiError("blip"))
        poller = Poller(adapter, WORK_SSID)
        poller.run_cycle()
        adapter.set_clients([work_client()])
        poller.run_cycle()
        db_session.expire_all()
        state = db_session.execute(select(PollerState)).scalar_one()
        assert state.consecutive_failures == 0
        assert state.last_poll_succeeded_at is not None

    def test_auth_failure_forces_relogin_next_cycle(
        self, db_session: Session, tracked_device: Device
    ) -> None:
        adapter = StubAdapter(UniFiAuthError("session dead"))
        poller = Poller(adapter, WORK_SSID)
        poller.run_cycle()  # login (1) then list fails with auth error
        adapter.set_clients([work_client()])
        poller.run_cycle()  # must login again (2)
        assert adapter.login_calls == 2
