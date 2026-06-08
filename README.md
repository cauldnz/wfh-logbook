# WFH Logbook

A self-hosted, auditable logbook for tracking work-from-home hours, designed to meet the Australian Taxation Office's record-keeping requirements for the fixed-rate working-from-home deduction (PCG 2023/1).

The system uses a dedicated Wi-Fi SSID on a Ubiquiti home network as an explicit "I am working" signal. Connecting to the work SSID is the act of clocking on; disconnecting is the act of clocking off. Connection events are captured contemporaneously from the UniFi controller, stored immutably, sessionised, reviewed daily, and exported annually to a spreadsheet suitable for an ATO logbook.

## Status

Phases 1, 3, 4, 5, 6 implemented per [HANDOFF.md](HANDOFF.md). Phases 2
(UniFi poller) and 7 (Telegram bot) are pending real-data capture from
the user's controller and Telegram updates per the project's
"Real Data First" rule (see [CLAUDE.md](CLAUDE.md)). The web UI, API,
sessionisation, XLSX/CSV export, and nightly backup are all working;
observations land in the database manually until Phase 2 wires the poller.

## Why this exists

Since 1 March 2023, the ATO requires a contemporaneous record of every hour worked from home across the entire income year — a four-week representative sample is no longer accepted. Many people are now keeping this record in a spreadsheet they update manually, which is error-prone and tends to be reconstructed rather than truly contemporaneous.

This project aims to produce a record that is:

- **Contemporaneous by construction** — events are captured at the moment they occur by an automated poller, not entered after the fact.
- **Immutable at the source** — raw observations are write-once. Daily summaries are reviewed and adjusted by a human, with all adjustments versioned and reason-tagged.
- **Defensible** — paired with a written methodology document explaining exactly how hours are derived, so the output is the product of a documented, repeatable process rather than "some hours in a spreadsheet."
- **Self-hosted** — runs entirely on your own infrastructure. No cloud service has your presence data.

## How it works

1. You create a dedicated `WFH` SSID on your UniFi network and configure your work devices not to auto-join it.
2. When you start work you manually connect to `WFH`. When you finish you disconnect.
3. A small Docker service polls the UniFi controller and records every connect/disconnect event.
4. Each night, raw events are converted into work sessions using documented rules (gap-bridging, multi-device handling, daily caps).
5. Each morning, you spend ~15 seconds confirming yesterday's total or adjusting it — either in a local web UI, or via an optional Telegram bot if you'd rather review from your phone.
6. At year end, export an XLSX logbook for your tax return.

The Telegram bot is optional and disabled by default. When enabled, it talks to the same internal API the web UI uses, so adjustments made through either channel are equally first-class and equally audit-logged. Public ingress for the bot's webhook is provided by a Cloudflare Tunnel sidecar — no router port-forwarding required.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the technical design and [docs/METHODOLOGY.md](docs/METHODOLOGY.md) for the ATO-facing methodology document.

## Who this is for

Australian residents who:

- Work from home and intend to claim the fixed-rate WFH deduction.
- Have a Ubiquiti home network (UniFi controller — Cloud Key, UDM, UDM Pro, Dream Router, or self-hosted).
- Are comfortable running a Docker container on a home server or NAS.
- Want a defensible record rather than the easiest possible record.

It is not useful for people without a UniFi network. The same design pattern could be ported to other vendors (Aruba Instant On, Omada, OpenWRT) but no such adapter exists here.

## Quick start

Requires a container engine (Docker Desktop or Podman in Docker-compat mode)
and ~10 minutes.

```bash
# 1. Configure
cp .env.example .env
# Edit .env: UNIFI_HOST, UNIFI_USERNAME, UNIFI_PASSWORD, WORK_SSID,
# and WORK_DEVICE_MACS (form: aa:bb:cc:dd:ee:ff=iPhone,11:22:...=Laptop).

# 2. Build & run
make docker-build
make docker-up

# 3. Verify
curl http://localhost:8088/api/health
# Open http://localhost:8088 in a browser.
```

The container runs `alembic upgrade head` on startup, seeds the `config`
row from `.env`, then exposes the FastAPI service on port 8088 (LAN). Data
lives in the `wfh-data` named volume.

### Local development

If you prefer to iterate without Docker:

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"
make migrate        # alembic upgrade head
make dev            # uvicorn with --reload on :8088
make test
make lint typecheck
```

A VS Code devcontainer is provided ([.devcontainer/devcontainer.json](.devcontainer/devcontainer.json));
Reopen-in-Container gives you a Python 3.12 / uv / Ruff / Mypy environment
ready for the inner loop. The full compose stack is run from the host with
`podman compose up` (or `docker compose up`).

### Year-end export

```bash
# From a host shell (with the venv active):
make export-xlsx FY=2025-26 OUT=/tmp/wfh-2025-26.xlsx

# Or via the API:
curl -o wfh-2025-26.xlsx "http://localhost:8088/api/export.xlsx?fy=2025-26"
```

### Optional: Telegram bot via Cloudflare Tunnel

Set `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`, `PUBLIC_BASE_URL`, and
`TELEGRAM_ALLOWED_USER_IDS` in `.env`, plus `CLOUDFLARE_TUNNEL_TOKEN`, then:

```bash
podman compose --profile tunnel up -d
```

One-time setup: create a Cloudflare Tunnel in the dashboard, bind a
public hostname to `app:8088`. Phase 7 of the build (Telegram + bot
conversation logic) is gated on capturing real Telegram payload samples
per the project's Real Data First rule — see [CLAUDE.md](CLAUDE.md).

## Disclaimer

This software is provided as-is to help you keep a record. It does not provide tax advice. The author is not an accountant. Eligibility for any deduction, the correct method to use, and the validity of any record in your specific circumstances are matters for a registered tax agent. The ATO requires records to be kept for five years from the date of lodgement; the choice to rely on the output of this tool, and the responsibility for that output, is yours.

## Licence

MIT — see [LICENSE](LICENSE).
