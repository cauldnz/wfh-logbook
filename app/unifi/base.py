"""Controller-agnostic types for the UniFi integration.

``ControllerAdapter`` is the seam future controller flavours implement
(classic Cloud Key, self-hosted). The poller and tests depend only on this
protocol plus ``ClientObservation`` — never on adapter internals.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable


class UniFiError(RuntimeError):
    """Base class for UniFi integration failures."""


class UniFiAuthError(UniFiError):
    """Login rejected (bad credentials, SSO account, MFA challenge)."""


class UnsupportedControllerError(UniFiError):
    """The controller flavour is detected but not yet supported.

    Raised for classic controllers until a real fixture is contributed —
    per CLAUDE.md "Real Data First" we do not ship code against a schema
    we have never seen.
    """


@dataclass(frozen=True, slots=True)
class ClientObservation:
    """One active client as reported by the controller, normalised.

    Field mapping is per the captured UDM fixture
    (tests/fixtures/unifi_clients_active.json):

    - ``mac``        ← ``mac`` (lower-cased)
    - ``ssid``       ← ``essid`` (UDM has no ``ssid`` key; verified in fixture)
    - ``last_seen``  ← ``last_seen`` unix epoch → tz-aware UTC datetime
    - ``signal_dbm`` ← ``signal`` (dBm, negative)
    - ``is_wired``   ← ``is_wired``
    - ``hostname``   ← ``hostname`` (label fallback)
    - ``raw``        ← the whole client dict, for the observations raw_json column
    """

    mac: str
    ssid: str | None
    last_seen: datetime | None
    signal_dbm: int | None
    is_wired: bool
    hostname: str | None
    raw: dict[str, Any] = field(repr=False)  # never logged at INFO


@runtime_checkable
class ControllerAdapter(Protocol):
    """What the poller needs from any controller flavour."""

    def login(self) -> None:
        """(Re-)authenticate. Raises UniFiAuthError on rejection."""
        ...

    def list_active_clients(self) -> list[ClientObservation]:
        """Fetch currently-active clients, normalised.

        Implementations must re-authenticate transparently on session
        expiry. Raises UniFiError subclasses on failure.
        """
        ...

    def close(self) -> None:
        """Release the underlying HTTP resources."""
        ...
