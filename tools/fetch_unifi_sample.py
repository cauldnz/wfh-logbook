"""Capture a real UniFi controller response sample for Phase 2 fixtures.

Per CLAUDE.md "Real Data First": we MUST run a real fetch and build code
against the captured schema before writing the UniFi client. This script
is opt-in, runs manually, and never gates CI.

What it does:

1. Reads UniFi credentials from `.env` (via app.config Settings).
2. Logs into the controller (UDM-line auth at /api/auth/login; falls back
   to classic /api/login if the UDM path 404s).
3. Fetches three endpoints to give Phase 2 enough schema to work from:
     - active clients   (/proxy/network/api/s/{site}/stat/sta)
     - SSID config      (/proxy/network/api/s/{site}/rest/wlanconf)
     - device/AP state  (/proxy/network/api/s/{site}/stat/device)
4. Writes RAW responses to `tests/fixtures/unifi_*.raw.json`.
   These files are GITIGNORED — they may contain MACs of every device in
   your house. After review, copy a sanitised slice into a sibling file
   named `unifi_*.json` (committed) using `--sanitise`.

Run:

    .venv/Scripts/python tools/fetch_unifi_sample.py
    .venv/Scripts/python tools/fetch_unifi_sample.py --sanitise

The sanitise step keeps the schema (every field name) but replaces sensitive
MAC values, hostnames, IPs, and SSID names (except WORK_SSID) with stable
placeholder strings. Schema fidelity is preserved; PII isn't.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

import httpx

from app.config import get_settings

logger = logging.getLogger("fetch_unifi_sample")

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures"

RAW_FILES = {
    "active": FIXTURES_DIR / "unifi_clients_active.raw.json",
    "wlan": FIXTURES_DIR / "unifi_wlanconf.raw.json",
    "devices": FIXTURES_DIR / "unifi_devices.raw.json",
}
SANITISED_FILES = {
    "active": FIXTURES_DIR / "unifi_clients_active.json",
    "wlan": FIXTURES_DIR / "unifi_wlanconf.json",
    "devices": FIXTURES_DIR / "unifi_devices.json",
}


# --------------------------------------------------------------------- login


def _login(client: httpx.Client, host: str, username: str, password: str) -> str:
    """Log in, return the X-CSRF-Token to send on subsequent requests.

    Tries UDM-line path first; falls back to classic.
    """
    # UDM-line: POST /api/auth/login
    udm_url = f"{host.rstrip('/')}/api/auth/login"
    r = client.post(udm_url, json={"username": username, "password": password})
    if r.status_code == 200:
        token: str = r.headers.get("x-csrf-token", "") or ""
        logger.info("login ok via UDM path (/api/auth/login)")
        return token
    if r.status_code == 404:
        # Classic controller fallback.
        classic_url = f"{host.rstrip('/')}/api/login"
        r2 = client.post(classic_url, json={"username": username, "password": password})
        if r2.status_code == 200:
            logger.info("login ok via classic path (/api/login)")
            return ""  # classic doesn't use CSRF token header
    raise SystemExit(
        f"login failed: status={r.status_code} body={r.text[:300]!r}\n"
        "Check UNIFI_HOST, UNIFI_USERNAME, UNIFI_PASSWORD. Common causes:\n"
        "  - SSO-linked account instead of a local one (MFA blocks programmatic login).\n"
        "  - Wrong host URL (try https://192.168.1.1 with the scheme included).\n"
        "  - Password contains characters that need escaping in .env (wrap in single quotes)."
    )


# ------------------------------------------------------------------ fetching


def _detect_path_prefix(client: httpx.Client, host: str, site: str, csrf: str) -> str:
    """UDM uses /proxy/network/api; classic uses /api directly."""
    headers = {"X-CSRF-Token": csrf} if csrf else {}
    udm_url = f"{host.rstrip('/')}/proxy/network/api/s/{site}/stat/sta"
    r = client.get(udm_url, headers=headers)
    if r.status_code in (200, 204):
        return "/proxy/network/api"
    classic_url = f"{host.rstrip('/')}/api/s/{site}/stat/sta"
    r2 = client.get(classic_url, headers=headers)
    if r2.status_code in (200, 204):
        return "/api"
    raise SystemExit(
        f"could not reach stat/sta on either path: UDM={r.status_code}, classic={r2.status_code}"
    )


def _fetch(client: httpx.Client, url: str, csrf: str) -> dict[str, Any]:
    headers = {"X-CSRF-Token": csrf} if csrf else {}
    r = client.get(url, headers=headers)
    r.raise_for_status()
    payload: Any = r.json()
    if not isinstance(payload, dict):
        raise SystemExit(f"unexpected response shape (not a dict): {type(payload).__name__}")
    return payload


# ------------------------------------------------------------------- sanitise


_KEEP_AS_IS_FIELDS = {
    # Schema-defining fields we want preserved verbatim:
    "is_wired",
    "is_guest",
    "is_connected",
    "satisfaction",
    "channel",
    "rssi",
    "signal",
    "noise",
    "tx_rate",
    "rx_rate",
    "uptime",
    "last_seen",
    "first_seen",
    "_id",
    "_uptime_by_uap",
    "_last_seen_by_uap",
    "wifi_tx_attempts",
    "rx_packets",
    "tx_packets",
    "rx_bytes",
    "tx_bytes",
}


def _mask_mac(mac: str, salt: int = 0) -> str:
    """Replace MAC with a stable placeholder keyed by hash position."""
    return f"aa:bb:cc:00:00:{salt:02x}"


def _sanitise_client(c: dict[str, Any], work_ssid: str, idx: int) -> dict[str, Any]:
    """Anonymise a single client row while preserving schema.

    Keeps every field name. Replaces:
    - mac, ap_mac, gw_mac → placeholder MACs (stable per index)
    - hostname, name → "device-N"
    - ip → 10.0.0.N
    - ssid → "WFH" if it matches WORK_SSID, otherwise "GUEST-SSID"
    """
    out = dict(c)
    for k in list(out.keys()):
        v = out[k]
        if k == "mac" or k.endswith("_mac"):
            out[k] = _mask_mac(str(v), idx)
        elif k in ("hostname", "name", "oui"):
            out[k] = f"device-{idx}"
        elif k == "ip" or k == "fixed_ip" or k.endswith("_ip"):
            out[k] = f"10.0.0.{idx + 10}"
        elif k == "ssid" or k == "essid":
            out[k] = "WFH" if v == work_ssid else "OTHER-SSID"
        elif k == "user_id" and isinstance(v, str):
            out[k] = f"user-{idx}"
        elif k == "note" and v:
            out[k] = "(redacted)"
    return out


def _sanitise_active(payload: dict[str, Any], work_ssid: str) -> dict[str, Any]:
    if "data" not in payload or not isinstance(payload["data"], list):
        return payload
    return {
        **payload,
        "data": [_sanitise_client(c, work_ssid, i) for i, c in enumerate(payload["data"])],
    }


def _sanitise_wlan(payload: dict[str, Any], work_ssid: str) -> dict[str, Any]:
    if "data" not in payload or not isinstance(payload["data"], list):
        return payload
    out_data: list[dict[str, Any]] = []
    for i, w in enumerate(payload["data"]):
        wc = dict(w)
        if wc.get("name") == work_ssid:
            wc["name"] = "WFH"
        elif "name" in wc:
            wc["name"] = f"OTHER-{i}"
        # Wipe password fields completely.
        for secret_key in ("x_passphrase", "passphrase", "x_password", "wpa_psk_radius_profile_id"):
            if secret_key in wc:
                wc[secret_key] = "(redacted)"
        out_data.append(wc)
    return {**payload, "data": out_data}


def _sanitise_devices(payload: dict[str, Any]) -> dict[str, Any]:
    if "data" not in payload or not isinstance(payload["data"], list):
        return payload
    out_data: list[dict[str, Any]] = []
    for i, d in enumerate(payload["data"]):
        dc = dict(d)
        for k in list(dc.keys()):
            v = dc[k]
            if k == "mac" or k.endswith("_mac"):
                dc[k] = _mask_mac(str(v), i)
            elif k in ("name", "model_in_lts", "model_in_eol", "hostname"):
                if k == "name":
                    dc[k] = f"AP-{i}"
            elif k == "ip" or k.endswith("_ip"):
                dc[k] = f"10.0.0.{i + 1}"
            elif k == "serial":
                dc[k] = "REDACTED"
        out_data.append(dc)
    return {**payload, "data": out_data}


# ------------------------------------------------------------------ runner


def run_fetch() -> None:
    settings = get_settings()
    if not settings.unifi_host or not settings.unifi_username:
        raise SystemExit(
            "UNIFI_HOST and UNIFI_USERNAME must be set in .env before running this script."
        )
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)

    logger.info("connecting to %s as %s", settings.unifi_host, settings.unifi_username)
    with httpx.Client(verify=settings.unifi_verify_tls, timeout=15.0, follow_redirects=True) as c:
        csrf = _login(c, settings.unifi_host, settings.unifi_username, settings.unifi_password)
        prefix = _detect_path_prefix(c, settings.unifi_host, settings.unifi_site, csrf)
        base = f"{settings.unifi_host.rstrip('/')}{prefix}/s/{settings.unifi_site}"

        active = _fetch(c, f"{base}/stat/sta", csrf)
        wlan = _fetch(c, f"{base}/rest/wlanconf", csrf)
        devices = _fetch(c, f"{base}/stat/device", csrf)

    RAW_FILES["active"].write_text(json.dumps(active, indent=2), encoding="utf-8")
    RAW_FILES["wlan"].write_text(json.dumps(wlan, indent=2), encoding="utf-8")
    RAW_FILES["devices"].write_text(json.dumps(devices, indent=2), encoding="utf-8")
    logger.info("wrote %s", RAW_FILES["active"])
    logger.info("wrote %s", RAW_FILES["wlan"])
    logger.info("wrote %s", RAW_FILES["devices"])

    # Summary.
    n_clients = len(active.get("data", []))
    by_ssid: dict[str, int] = {}
    for c2 in active.get("data", []):
        by_ssid[c2.get("ssid") or c2.get("essid") or "(wired)"] = (
            by_ssid.get(c2.get("ssid") or c2.get("essid") or "(wired)", 0) + 1
        )
    print()
    print(f"Summary: {n_clients} active client(s)")
    for ssid, n in sorted(by_ssid.items(), key=lambda kv: -kv[1]):
        marker = "  <- WORK_SSID" if ssid == settings.work_ssid else ""
        print(f"  {n:>3}  on {ssid!r}{marker}")
    print()
    print("Next step: review the raw JSON in tests/fixtures/, then run")
    print("  python tools/fetch_unifi_sample.py --sanitise")
    print("to produce the committed sanitised fixtures.")


def run_sanitise() -> None:
    settings = get_settings()
    if not RAW_FILES["active"].exists():
        raise SystemExit(
            f"no raw file at {RAW_FILES['active']}; run without --sanitise first to fetch."
        )
    active = json.loads(RAW_FILES["active"].read_text(encoding="utf-8"))
    wlan = json.loads(RAW_FILES["wlan"].read_text(encoding="utf-8"))
    devices = json.loads(RAW_FILES["devices"].read_text(encoding="utf-8"))

    SANITISED_FILES["active"].write_text(
        json.dumps(_sanitise_active(active, settings.work_ssid), indent=2), encoding="utf-8"
    )
    SANITISED_FILES["wlan"].write_text(
        json.dumps(_sanitise_wlan(wlan, settings.work_ssid), indent=2), encoding="utf-8"
    )
    SANITISED_FILES["devices"].write_text(
        json.dumps(_sanitise_devices(devices), indent=2), encoding="utf-8"
    )
    for k, p in SANITISED_FILES.items():
        logger.info("wrote sanitised %s (%s)", k, p)
    print()
    print("Review the sanitised files before committing. They preserve every")
    print("field name and structure but redact MACs/IPs/hostnames/SSID names.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sanitise",
        action="store_true",
        help="produce the committed sanitised fixtures from the raw files",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(levelname)s %(name)s %(message)s")
    if args.sanitise:
        run_sanitise()
    else:
        run_fetch()
    return 0


_ = Any  # silence unused-typing import when -O

if __name__ == "__main__":
    sys.exit(main())
