"""Adapter factory with controller-flavour detection.

``UNIFI_API_FLAVOUR`` controls behaviour:

- ``udm``     → UDMAdapter, no probing.
- ``classic`` → honest rejection (no classic fixture captured yet).
- ``auto``    → probe: if the UDM login endpoint exists, use UDMAdapter;
  if it 404s (the classic tell), reject with guidance.

The rejection is deliberate, not lazy: per CLAUDE.md "Real Data First" we
do not ship parsing code for a response shape we have never captured.
A classic adapter lands as soon as someone runs tools/fetch_unifi_sample.py
against a classic controller and contributes the sanitised fixture.
"""

from __future__ import annotations

import logging
from typing import Literal

import httpx

from app.config import Settings
from app.unifi.base import ControllerAdapter, UniFiError, UnsupportedControllerError
from app.unifi.udm import UDMAdapter

logger = logging.getLogger(__name__)

_CLASSIC_GUIDANCE = (
    "This looks like a classic UniFi controller (Cloud Key Gen1/Gen2 or "
    "self-hosted). Only UDM-line controllers are supported so far, because "
    "the project only writes parsers against response shapes captured from "
    "real controllers. To add support: run tools/fetch_unifi_sample.py "
    "against this controller, run it again with --sanitise, and open an "
    "issue attaching the sanitised fixtures."
)


def _normalise_host(host: str) -> str:
    host = host.strip()
    if not host.startswith(("http://", "https://")):
        logger.warning("unifi: UNIFI_HOST has no scheme; assuming https:// (was %r)", host)
        host = "https://" + host
    return host.rstrip("/")


def _probe_flavour(host: str, verify_tls: bool) -> Literal["udm", "classic"]:
    """Return 'udm' or 'classic' by probing the login endpoint.

    UDM-line answers on /api/auth/login (any status except 404 means the
    route exists). Classic controllers 404 there and serve /api/login
    instead. We send an empty POST — we only care about route existence,
    not authentication, so no credentials leave this function.
    """
    with httpx.Client(verify=verify_tls, timeout=10.0, follow_redirects=True) as probe:
        try:
            r = probe.post(f"{host}/api/auth/login", json={})
        except httpx.HTTPError as e:
            raise UniFiError(f"cannot reach {host}: {e!r}") from e
        if r.status_code != 404:
            return "udm"
        try:
            r2 = probe.post(f"{host}/api/login", json={})
        except httpx.HTTPError as e:
            raise UniFiError(f"cannot reach {host}: {e!r}") from e
        if r2.status_code != 404:
            return "classic"
    raise UniFiError(
        f"{host} answers neither /api/auth/login (UDM) nor /api/login (classic); "
        "is this a UniFi controller?"
    )


def create_adapter(settings: Settings) -> ControllerAdapter:
    """Build the right adapter for the configured controller.

    Does not log in — callers (the poller) authenticate lazily on first
    poll so app startup never blocks on controller availability.
    """
    host = _normalise_host(settings.unifi_host)
    flavour = settings.unifi_api_flavour
    if flavour == "auto":
        flavour = _probe_flavour(host, settings.unifi_verify_tls)
        logger.info("unifi: detected controller flavour %r", flavour)
    if flavour == "classic":
        raise UnsupportedControllerError(_CLASSIC_GUIDANCE)
    return UDMAdapter(
        host=host,
        username=settings.unifi_username,
        password=settings.unifi_password,
        site=settings.unifi_site,
        verify_tls=settings.unifi_verify_tls,
    )
