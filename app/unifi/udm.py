"""UDM-line controller adapter (Dream Machine family, UniFi OS).

Endpoints and field names verified against a real controller capture —
tests/fixtures/unifi_clients_active.json, fetched by
tools/fetch_unifi_sample.py. Do not add field references that are not
present in that fixture without re-capturing first (CLAUDE.md Real Data
First).

Auth model (observed live):
- POST /api/auth/login {username, password} → 200, session cookie set on
  the client, ``x-csrf-token`` response header.
- Subsequent requests carry the cookie; the CSRF token is required for
  mutating calls and harmless on reads — we send it always.
- Session expiry surfaces as 401; we re-login once and retry.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from app.unifi.base import ClientObservation, UniFiAuthError, UniFiError

logger = logging.getLogger(__name__)


def _normalise_client(c: dict[str, Any]) -> ClientObservation:
    """Map one raw UDM stat/sta client dict to a ClientObservation.

    Field provenance: see fixture. ``essid`` is absent on wired clients in
    principle, so we use .get() defensively, but the captured fixture shows
    every wireless client carries ``essid``, ``signal``, and ``last_seen``.
    """
    last_seen_epoch = c.get("last_seen")
    last_seen = (
        datetime.fromtimestamp(last_seen_epoch, tz=UTC)
        if isinstance(last_seen_epoch, int)
        else None
    )
    signal = c.get("signal")
    return ClientObservation(
        mac=str(c.get("mac", "")).lower(),
        ssid=c.get("essid"),
        last_seen=last_seen,
        signal_dbm=signal if isinstance(signal, int) else None,
        is_wired=bool(c.get("is_wired", False)),
        hostname=c.get("hostname"),
        raw=c,
    )


class UDMAdapter:
    """ControllerAdapter implementation for UniFi OS gateways."""

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        site: str = "default",
        verify_tls: bool = False,
        timeout: float = 15.0,
    ) -> None:
        self._host = host.rstrip("/")
        self._username = username
        self._password = password
        self._site = site
        self._csrf_token: str = ""
        self._client = httpx.Client(verify=verify_tls, timeout=timeout, follow_redirects=True)

    # ------------------------------------------------------------------ auth
    def login(self) -> None:
        url = f"{self._host}/api/auth/login"
        try:
            r = self._client.post(
                url, json={"username": self._username, "password": self._password}
            )
        except httpx.HTTPError as e:
            raise UniFiError(f"login request to {self._host} failed: {e!r}") from e
        if r.status_code != 200:
            # 401/403: bad credentials or SSO/MFA account. Do NOT log the body
            # at INFO — it may echo the username.
            raise UniFiAuthError(
                f"controller rejected login (HTTP {r.status_code}); "
                "check UNIFI_USERNAME/UNIFI_PASSWORD are a LOCAL admin account"
            )
        self._csrf_token = r.headers.get("x-csrf-token", "") or ""
        logger.info("unifi: login ok (%s)", self._host)

    # ---------------------------------------------------------------- fetch
    def _get(self, path: str) -> httpx.Response:
        headers = {"X-CSRF-Token": self._csrf_token} if self._csrf_token else {}
        try:
            return self._client.get(f"{self._host}{path}", headers=headers)
        except httpx.HTTPError as e:
            raise UniFiError(f"GET {path} failed: {e!r}") from e

    def list_active_clients(self) -> list[ClientObservation]:
        path = f"/proxy/network/api/s/{self._site}/stat/sta"
        r = self._get(path)
        if r.status_code == 401:
            # Session expired — re-login once and retry.
            logger.info("unifi: session expired, re-authenticating")
            self.login()
            r = self._get(path)
        if r.status_code != 200:
            raise UniFiError(f"stat/sta returned HTTP {r.status_code}")
        payload = r.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise UniFiError(
                "stat/sta response missing 'data' list; "
                "controller may have changed response shape — re-run "
                "tools/fetch_unifi_sample.py and compare with the committed fixture"
            )
        clients = [_normalise_client(c) for c in data if isinstance(c, dict)]
        logger.debug("unifi: %d active client(s)", len(clients))
        return clients

    def close(self) -> None:
        self._client.close()
