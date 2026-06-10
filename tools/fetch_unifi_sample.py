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

# Allow running this script directly from cmd.exe / PowerShell without
# requiring `pip install -e .` to have been done — add the repo root to
# sys.path so `from app.config import ...` resolves.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from app.config import get_settings

logger = logging.getLogger("fetch_unifi_sample")

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures"

RAW_FILES = {
    "active": FIXTURES_DIR / "unifi_clients_active.raw.json",
    "wlan": FIXTURES_DIR / "unifi_wlanconf.raw.json",
    "devices": FIXTURES_DIR / "unifi_devices.raw.json",
}
# Devices (stat/device) intentionally NOT sanitised+committed: the response
# is large (>100KB) with deeply nested arrays of every adjacent device's
# BSSID/IPv6/etc., and Phase 2's poller only reads stat/sta. The raw is
# still saved (gitignored) for ad-hoc diagnostics.
SANITISED_FILES = {
    "active": FIXTURES_DIR / "unifi_clients_active.json",
    "wlan": FIXTURES_DIR / "unifi_wlanconf.json",
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

    Keeps every field name and type. Replaces PII-bearing values with stable
    placeholders. Covers the UDM-line `stat/sta` shape observed in
    tests/fixtures/unifi_clients_active.raw.json.
    """
    mac_fields = {"mac", "ap_mac", "gw_mac", "sw_mac", "bssid", "last_uplink_mac"}
    ipv4_fields = {"ip", "fixed_ip", "last_ip"}
    name_fields = {"hostname", "name", "oui", "last_uplink_name"}
    # UniFi-internal IDs — site/account-specific, redact for stability.
    id_fields = {
        "_id",
        "anon_client_id",
        "user_id",
        "network_id",
        "site_id",
        "wlanconf_id",
        "last_connection_network_id",
        "user_group_id_computed",
        "usergroup_id",
    }

    out = dict(c)
    for k in list(out.keys()):
        v = out[k]
        if k in mac_fields or k.endswith("_mac"):
            out[k] = _mask_mac(str(v), idx)
        elif k in ipv4_fields or (k.endswith("_ip") and isinstance(v, str)):
            out[k] = f"10.0.0.{idx + 10}"
        elif k in name_fields:
            out[k] = f"AP-{idx}" if k == "last_uplink_name" else f"device-{idx}"
        elif k in ("ipv6_addresses", "last_ipv6"):
            out[k] = ["fe80::aa:bb:cc:dd"]
        elif k in id_fields and isinstance(v, str):
            out[k] = f"id-{idx}"
        elif k in ("network", "last_connection_network_name") and isinstance(v, str):
            # Leave "Default" alone; redact other names that might be PII.
            if v != "Default":
                out[k] = "network-X"
        elif k in ("ssid", "essid"):
            out[k] = "WFH" if v == work_ssid else "OTHER-SSID"
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


# ------------------------------------------------------------------ runner


def run_fetch() -> None:
    settings = get_settings()
    if not settings.unifi_host or not settings.unifi_username:
        raise SystemExit(
            "UNIFI_HOST and UNIFI_USERNAME must be set in .env before running this script."
        )
    host = settings.unifi_host
    if not host.startswith(("http://", "https://")):
        # Friendly auto-fix for the common omission. Home UniFi controllers
        # use self-signed HTTPS, so https:// is the right default.
        logger.warning("UNIFI_HOST has no scheme; assuming https:// (was %s)", host)
        host = "https://" + host
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)

    logger.info("connecting to %s as %s", host, settings.unifi_username)
    with httpx.Client(verify=settings.unifi_verify_tls, timeout=15.0, follow_redirects=True) as c:
        csrf = _login(c, host, settings.unifi_username, settings.unifi_password)
        prefix = _detect_path_prefix(c, host, settings.unifi_site, csrf)
        base = f"{host.rstrip('/')}{prefix}/s/{settings.unifi_site}"

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
    SANITISED_FILES["active"].write_text(
        json.dumps(_sanitise_active(active, settings.work_ssid), indent=2), encoding="utf-8"
    )
    SANITISED_FILES["wlan"].write_text(
        json.dumps(_sanitise_wlan(wlan, settings.work_ssid), indent=2), encoding="utf-8"
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
