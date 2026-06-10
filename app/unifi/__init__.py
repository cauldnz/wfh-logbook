"""UniFi controller integration.

Schema discipline (CLAUDE.md "Real Data First"): every field name referenced
in this package exists in a committed fixture captured from a real
controller — see ``tests/fixtures/unifi_clients_active.json`` and the
re-fetch script ``tools/fetch_unifi_sample.py``.

Currently verified controller flavour: **UDM-line** (Dream Machine family,
UniFi OS). Classic controllers (Gen1/Gen2 Cloud Key, self-hosted) are
detected and rejected with a clear message until a real classic fixture is
contributed; see ``app/unifi/client.py``.
"""

from __future__ import annotations

from app.unifi.base import ClientObservation, ControllerAdapter, UnsupportedControllerError
from app.unifi.client import create_adapter
from app.unifi.udm import UDMAdapter

__all__ = [
    "ClientObservation",
    "ControllerAdapter",
    "UDMAdapter",
    "UnsupportedControllerError",
    "create_adapter",
]
