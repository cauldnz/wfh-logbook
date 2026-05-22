# Implementation Handoff

**Audience**: a coding agent (typically Claude Code) implementing this project autonomously.
**Authoritative**: this document is the spec. `docs/ARCHITECTURE.md` is the design rationale. When in doubt, this document wins; if a change is required to this document, update it *first*, then code.

Sections are numbered for reference in commit messages, issues, and follow-up prompts (e.g. "fix per §3.4", "phase 2 of §6 complete").

---

## 1. Project mission, in one paragraph

Build a self-hosted service that produces a contemporaneous, audit-defensible record of hours worked from home for the Australian Taxation Office's fixed-rate WFH deduction. The signal is association events on a dedicated UniFi Wi-Fi SSID. Raw evidence is immutable. Daily totals are computed by a deterministic, versioned rule set. A lightweight web UI lets the user review and lock each day in seconds. An XLSX export produced at year-end is the deliverable for tax filing. Design rationale is in `docs/ARCHITECTURE.md`; the user-facing methodology is in `docs/METHODOLOGY.md`. Both must remain consistent with this spec.

## 2. Hard constraints

These are non-negotiable. If implementation forces a choice that violates one of these, stop and flag it; do not work around.

1. **Raw observations are append-only.** No code path may `UPDATE` or `DELETE` rows in the `observations` table. Tests must enforce this.
2. **Daily summary adjustments are versioned.** Editing a daily summary creates a new row; the previous row is preserved unchanged.
3. **Sessionisation is deterministic.** Running the sessioniser twice on the same date with the same raw observations and the same `rule_version` must produce identical `sessions` and identical computed `daily_summaries.computed_seconds`.
4. **Local-only by default.** No outbound network calls except to the configured UniFi controller. No telemetry. No analytics. No CDN-loaded assets in the web UI.
5. **No employer/tax-agent advice.** UI copy and exported documents must not make claims about deductibility, eligibility, or correct dollar values. Hours only. The methodology document handles framing.
6. **Methodology and code agree.** If `docs/METHODOLOGY.md` describes a rule, the code implements that rule. Tests must verify this on the rules in §4 of `METHODOLOGY.md` (gap-bridging, min session, midnight crossing, adjustment versioning).

## 3. Tech stack (locked)

- **Language**: Python 3.12+.
- **Web framework**: FastAPI.
- **Background scheduling**: APScheduler, in-process with FastAPI.
- **ORM**: SQLAlchemy 2.x (with async support via `asyncio` not required; sync is fine for this load).
- **Migrations**: Alembic.
- **Database**: SQLite, file-backed, on a mounted Docker volume.
- **HTTP client (for UniFi)**: `httpx`.
- **Config**: `pydantic-settings`, `.env` file at runtime.
- **Templating**: Jinja2 for the UI. HTMX for interactivity. No SPA framework.
- **XLSX**: `openpyxl`.
- **Testing**: `pytest`, `pytest-asyncio` if needed, `httpx`'s test client via FastAPI's `TestClient`. Use `freezegun` or `time-machine` for time-dependent tests.
- **Linting/formatting**: `ruff` (lint + format), `mypy` in `--strict` mode for application code (tests may be looser).
- **Package management**: `uv` if possible, else `pip` with `pyproject.toml` and a lock file (`uv.lock` or `requirements.txt` + `requirements-dev.txt`).
- **Container**: a single multi-stage Dockerfile producing a runtime image based on `python:3.12-slim`. Non-root user. Volume mount for `/data`.

Do not introduce additional runtime dependencies without a written reason in a commit message or PR description.

## 4. Repository layout

Build inside the existing repo. Create exactly this structure:

```
.
├── app/
│   ├── __init__.py
│   ├── main.py              # FastAPI app factory + startup wiring
│   ├── config.py            # pydantic-settings, .env loading
│   ├── db.py                # SQLAlchemy engine, session factory
│   ├── models.py            # ORM models for observations, sessions, daily_summaries, config, devices
│   ├── schemas.py           # Pydantic schemas for API I/O
│   ├── unifi/
│   │   ├── __init__.py
│   │   ├── client.py        # UniFi controller HTTP client (login, fetch clients)
│   │   └── poller.py        # APScheduler job: poll + write observations
│   ├── sessions/
│   │   ├── __init__.py
│   │   ├── builder.py       # Sessionisation algorithm (§5.2 of ARCHITECTURE)
│   │   └── rules.py         # Rule constants + rule_version
│   ├── api/
│   │   ├── __init__.py
│   │   ├── days.py          # /api/days, /api/days/{date}, adjust, lock, resessionise
│   │   ├── exports.py       # /api/export.xlsx, /api/export.csv
│   │   └── health.py        # /api/health
│   ├── web/
│   │   ├── __init__.py
│   │   ├── routes.py        # HTML routes (review, calendar, year view)
│   │   ├── templates/
│   │   │   ├── base.html
│   │   │   ├── review.html
│   │   │   ├── calendar.html
│   │   │   └── year.html
│   │   └── static/
│   │       ├── styles.css
│   │       └── htmx.min.js  # vendored, do not load from CDN
│   ├── backup/
│   │   ├── __init__.py
│   │   └── snapshot.py      # nightly VACUUM INTO job
│   ├── exporters/
│   │   ├── __init__.py
│   │   ├── xlsx.py
│   │   └── csv.py
│   └── notifier/
│       ├── __init__.py
│       ├── base.py          # abstract Notifier interface, message/event types
│       ├── telegram.py      # Telegram adapter (httpx-based, webhook + polling)
│       ├── conversation.py  # pure conversation logic: event + state -> actions
│       ├── grammar.py       # adjustment-string parser
│       └── webhook.py       # FastAPI route for /webhook/telegram/{secret}
├── alembic/
│   ├── env.py
│   ├── script.py.mako
│   └── versions/
├── tests/
│   ├── conftest.py
│   ├── test_sessionisation.py
│   ├── test_versioning.py
│   ├── test_immutability.py
│   ├── test_unifi_client.py
│   ├── test_api_days.py
│   ├── test_api_export.py
│   ├── test_backup.py
│   ├── test_telegram_grammar.py
│   ├── test_telegram_conversation.py
│   ├── test_telegram_adapter.py
│   └── fixtures/
│       ├── unifi_clients_*.json
│       └── telegram_updates_*.json
├── Dockerfile
├── docker-compose.yml       # includes the app service AND a cloudflared sidecar
├── pyproject.toml
├── alembic.ini
├── .env.example
├── .dockerignore
└── README.md (already exists; update only the Quick Start section)
```

Do not create directories not listed here. If a need arises, propose the addition by updating this section first.

## 5. Configuration contract

`.env.example` must include, with comments:

```
# UniFi controller
UNIFI_HOST=                  # e.g. https://192.168.1.1
UNIFI_SITE=default
UNIFI_USERNAME=
UNIFI_PASSWORD=
UNIFI_VERIFY_TLS=false       # most home controllers use self-signed certs
UNIFI_API_FLAVOUR=auto       # auto | udm | classic

# Work SSID and devices
WORK_SSID=
WORK_DEVICE_MACS=            # comma-separated, with labels: aa:bb:cc:dd:ee:ff=iPhone,11:22:33:44:55:66=Laptop

# Sessionisation defaults (used only to seed config table on first run)
GAP_BRIDGE_MINUTES=10
MIN_SESSION_MINUTES=2
DAILY_CAP_HOURS=12
LOCAL_TIMEZONE=Australia/Sydney
RULE_VERSION=2026.1

# Service
HTTP_HOST=0.0.0.0
HTTP_PORT=8088
POLL_INTERVAL_SECONDS=60
DATA_DIR=/data
LOG_LEVEL=INFO

# Telegram bot (Phase 7)
TELEGRAM_BOT_TOKEN=
TELEGRAM_MODE=webhook                          # webhook | polling
TELEGRAM_ALLOWED_USER_IDS=                     # comma-separated numeric Telegram user IDs
TELEGRAM_WEBHOOK_SECRET=                       # random string; forms part of the webhook URL path
PUBLIC_BASE_URL=                               # e.g. https://wfh.example.com (only needed in webhook mode)

# Cloudflare Tunnel (only needed in webhook mode)
CLOUDFLARE_TUNNEL_TOKEN=
```

On startup, if the `config` row is absent, seed it from `.env`. On subsequent starts, `.env` values for sessionisation parameters are ignored — the database is the source of truth. Print a warning if `.env` values differ from DB values on startup, but do not overwrite.

## 6. Phased delivery

Deliver in the order below. Each phase ends with all its acceptance criteria green and is independently mergeable. Do not begin a phase until the previous phase's acceptance criteria pass.

### Phase 1 — Skeleton and data layer

**Deliverables**

- `pyproject.toml` with pinned deps, ruff + mypy configured.
- SQLAlchemy models for `observations`, `sessions`, `daily_summaries`, `config`, `devices` per `docs/ARCHITECTURE.md` §4.
- Alembic initial migration creating all tables and indexes.
- `app/main.py` with a minimal FastAPI app that starts, opens the DB, and exposes `/api/health` returning `{status, db_ok, last_poll: null}`.
- `Dockerfile` and `docker-compose.yml` that bring the app up with a mounted `/data` volume.
- `tests/test_immutability.py` verifying that `UPDATE`/`DELETE` against `observations` raises (use a SQL trigger or an ORM-level guard; both is fine).

**Acceptance**

- `docker compose up` produces a running container with `/api/health` returning HTTP 200.
- `alembic upgrade head` produces all tables with correct columns and indexes.
- `pytest` passes with at least the immutability test.
- `ruff check` and `mypy --strict app` are clean.

### Phase 2 — UniFi client and poller

**Deliverables**

- `app/unifi/client.py`: an HTTP client that authenticates to the local UniFi controller, handles both UDM and classic controller URL flavours, and exposes `list_active_clients()` returning a normalised list of dicts containing at minimum `mac`, `ssid`, `last_seen`, `signal`, and the raw payload.
- `app/unifi/poller.py`: APScheduler job that runs every `POLL_INTERVAL_SECONDS`, calls `list_active_clients()`, filters to the work SSID and configured MACs, and writes `observations` rows per §5.1 of `ARCHITECTURE.md` including disconnect-transition rows.
- Surface poller state on `/api/health`: `last_poll_attempted_at`, `last_poll_succeeded_at`, `consecutive_failures`.
- `tests/test_unifi_client.py` with fixture JSON for at least UDM and classic responses, exercising the normalisation.
- Integration-style test that runs the poller against an in-memory fake controller and verifies observations land correctly, including transition rows.

**Acceptance**

- Pointed at a real UniFi controller, the poller produces observations rows at the configured interval with the correct device labels.
- Transient HTTP failures are logged but do not crash the process; the next poll proceeds.
- All authentication credentials are read from config; nothing hard-coded.

### Phase 3 — Sessionisation

**Deliverables**

- `app/sessions/rules.py`: dataclass capturing all sessionisation parameters and a `rule_version` constant initialised from config.
- `app/sessions/builder.py`: pure function `build_sessions_for_date(observations, rules) -> list[Session]` per §5.2 of `ARCHITECTURE.md`. The function must not touch the database; it operates on inputs and returns outputs.
- A persistence layer that calls the builder, replaces existing `sessions` rows for the date in a transaction, and recomputes the unlocked `daily_summaries` row.
- APScheduler nightly job at 01:15 local that runs the sessioniser for "yesterday" plus any non-locked dates in the prior 7 days.
- `tests/test_sessionisation.py` covering at minimum: empty day, single short session below min, gap below bridge threshold merged, gap above bridge threshold not merged, multi-device overlap merged into one, midnight-crossing attributed to start date, daily-cap flagging without truncation, idempotence (running twice produces identical rows).

**Acceptance**

- All tests in `test_sessionisation.py` pass.
- Sessions and daily summary rows for a given date are reproducible from raw observations alone.
- The `rule_version` stored on every row matches the rule_version in `rules.py`.

### Phase 4 — Review API and Web UI

**Deliverables**

- API endpoints per `docs/ARCHITECTURE.md` §6.2: `GET /api/days`, `GET /api/days/{date}`, `POST /api/days/{date}/adjust`, `POST /api/days/{date}/lock`, `POST /api/days/{date}/resessionise`.
- Adjustments use the versioning rule per §5.5 of `ARCHITECTURE.md`. `tests/test_versioning.py` covers: adjustment on unlocked → new version, adjustment after lock → new unlocked version, computed_seconds preserved across versions, history retrievable.
- Web UI screens:
  - **Today/Yesterday review** (`/`): the day to review most prominently, with its sessions, computed hours, an adjustment form (minutes + reason), and a Lock button. Banner at top if `health.last_poll_succeeded_at` is stale.
  - **Calendar** (`/calendar`): last 90 days, colour-coded by status (no data / unreviewed / reviewed-not-locked / locked), each linking to the daily detail.
  - **Year** (`/year/{fy}`): financial-year view with running totals (e.g. claimed hours to date) and a "Download XLSX" button.
- Static assets served locally. No CDN.
- HTMX used for the adjustment form and lock button to avoid full page reloads.

**Acceptance**

- A full review cycle works end-to-end: data arrives in observations → sessioniser produces a draft summary → UI shows it → user adjusts → user locks → locked state visible on next load.
- All adjustments produce new versions; no version is ever overwritten.
- UI passes manual accessibility check: tab order is logical, forms have labels, contrast is acceptable.

### Phase 5 — Exports and backups

**Deliverables**

- `app/exporters/xlsx.py`: produces an XLSX with the following sheets:
  - **Summary**: one row per day for the requested financial year, columns: `Date`, `Day of week`, `Computed hours`, `Adjustment (hours)`, `Adjustment reason`, `Claimed hours`, `Version`, `Locked`, `Locked at`, `Rule version`.
  - **Year total**: total claimed hours, count of locked days, count of unlocked days, count of anomalous days, fixed-rate-method rate cell (left blank with a comment "Set this to the ATO published rate for the relevant year"), and a formula computing the dollar figure from the previous two.
  - **Methodology**: a copy of `docs/METHODOLOGY.md` with config-snapshot fields populated from the database at export time.
- `app/exporters/csv.py`: same data as the Summary sheet, no methodology.
- `app/backup/snapshot.py`: nightly `VACUUM INTO` to `${DATA_DIR}/backups/wfh-logbook-YYYYMMDD.sqlite`, with retention of 30 daily and 12 monthly snapshots (keep the first snapshot of each month as the monthly).
- Tests: `test_api_export.py` for XLSX structure and content; `test_backup.py` for snapshot creation and retention rotation.

**Acceptance**

- The XLSX export opens cleanly in Excel and LibreOffice.
- The Methodology sheet contains the populated values matching the live database config.
- Backups appear on the volume on schedule and retention rotates correctly.

### Phase 6 — Hardening

**Deliverables**

- Structured JSON logging to stdout.
- A `--dry-run` flag on the sessioniser CLI for ad-hoc invocation.
- `/api/health` enriched with: DB size, observations count for last 24h, last successful poll, last successful sessioniser run, last successful backup.
- A small Makefile or `justfile` with targets: `dev`, `test`, `lint`, `migrate`, `docker-build`, `docker-up`, `export`.
- A `SECURITY.md` covering the threat model from `ARCHITECTURE.md` §8 and how to report issues.
- README's Quick Start section completed with the actual commands.

**Acceptance**

- A new user can clone the repo, copy `.env.example` to `.env`, fill in values, run `make docker-up`, and have a working service within 10 minutes.

### Phase 7 — Telegram daily-review bot

This phase is purely additive. The web UI remains the canonical review interface; Telegram is a second, equally-privileged channel. Adjustments made via either channel are visible in the other.

**7.A Notifier abstraction**

- `app/notifier/base.py`: define an abstract `Notifier` protocol and concrete dataclasses for `IncomingEvent`, `OutgoingMessage`, `Button`, and `SentMessage`. The intent is that a future Signal/WhatsApp adapter could be added without changing `conversation.py`.
- `app/notifier/conversation.py`: a pure module that takes `(IncomingEvent, current_bot_state, db_reader) -> list[OutgoingAction]`. No HTTP. No Telegram-specific types. Testable in full isolation.
- `app/notifier/grammar.py`: the adjustment-string parser. Pure function: `parse_adjustment(text: str) -> Adjustment | ParseError`.

**7.B Telegram adapter**

- `app/notifier/telegram.py`: a thin httpx-based client for the Telegram Bot API. Supports `sendMessage`, `editMessageText`, `setWebhook`, `deleteWebhook`, `getUpdates`, `answerCallbackQuery`.
- Two operating modes selected by `TELEGRAM_MODE`: `webhook` (default) and `polling`. Polling mode exists for local development and tunnel-outage resilience.
- In webhook mode, on startup the app calls `setWebhook` with `${PUBLIC_BASE_URL}/webhook/telegram/${TELEGRAM_WEBHOOK_SECRET}` and the `secret_token` parameter set to the same value. On shutdown, optionally `deleteWebhook`.
- In polling mode, a background asyncio task runs a `getUpdates` long-poll loop.
- Do not use `python-telegram-bot` or other framework libraries. Raw httpx keeps the adapter minimal and consistent with the rest of the codebase.

**7.C Webhook ingress**

- `app/notifier/webhook.py`: a FastAPI router exposing `POST /webhook/telegram/{secret}`.
- The handler must verify both that `{secret}` in the path matches `TELEGRAM_WEBHOOK_SECRET` *and* that the `X-Telegram-Bot-Api-Secret-Token` header matches. Either failing → 401, no body details.
- The handler writes the raw update to `bot_messages` (direction='in') *before* attempting to process it. If processing crashes, the evidence is preserved.
- Idempotency: the unique index on `bot_messages.telegram_update_id` ensures replays of the same update are no-ops.
- `docker-compose.yml` adds a `cloudflared` sidecar service (`image: cloudflare/cloudflared:latest`) configured via `CLOUDFLARE_TUNNEL_TOKEN`. The README Quick Start documents the one-time Cloudflare dashboard steps to create the tunnel and bind a hostname to the `app:8088` service.

**7.D Schema additions**

Add tables in a new Alembic migration:

```
bot_chats(
  chat_id INTEGER PRIMARY KEY,
  telegram_user_id INTEGER NOT NULL,
  authorised INTEGER NOT NULL DEFAULT 0,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL
)

bot_state(
  chat_id INTEGER PRIMARY KEY REFERENCES bot_chats(chat_id),
  awaiting TEXT,            -- e.g. 'adjustment' | NULL
  awaiting_date TEXT,       -- YYYY-MM-DD when awaiting='adjustment'
  updated_at TEXT NOT NULL
)

bot_messages(
  id INTEGER PRIMARY KEY,
  chat_id INTEGER NOT NULL,
  direction TEXT NOT NULL CHECK (direction IN ('in','out')),
  telegram_update_id INTEGER,                          -- inbound only
  telegram_message_id INTEGER,                         -- outbound only
  text TEXT,
  raw_json TEXT NOT NULL,
  created_at TEXT NOT NULL
)
CREATE UNIQUE INDEX ux_bot_messages_update_id ON bot_messages(telegram_update_id) WHERE telegram_update_id IS NOT NULL;
```

Also extend `daily_summaries.created_by` to accept the values `'sessioniser' | 'web' | 'telegram'`. The existing audit semantics (versioned-on-edit, never overwritten) are unchanged.

**7.E Authorisation**

- `TELEGRAM_ALLOWED_USER_IDS` is the allowlist. The bot only acts on updates whose `from.id` is in the list.
- An unauthorised user receives exactly **one** polite rejection message ("This bot is private."). The rejection is logged. Subsequent unauthorised messages from the same user are silently dropped — no further outbound traffic to that chat.
- The `/start` command from an authorised user creates or upserts the `bot_chats` row and sends the help text.

**7.F Commands and grammar**

Slash commands (all only accepted from allowlisted users):

| Command | Behaviour |
|---|---|
| `/start` | Register the chat, send help. |
| `/help` | Show available commands and adjustment-string examples. |
| `/today` | Show today's running total and sessions so far. Buttons: `[✏ Adjust]`. No `[Lock]` — the day isn't done. |
| `/yesterday` | Show yesterday's daily summary. Buttons: `[✓ Confirm]` `[✏ Adjust]` `[🔒 Lock]`. |
| `/day YYYY-MM-DD` | Show the specified date. Same buttons as `/yesterday`. |
| `/week` | Last 7 complete days, totals only. No buttons. |
| `/year` | Current FY total claimed hours and count of days locked/unlocked. No buttons. |
| `/status` | Last successful poll, last sessioniser run, last backup. |

Callback queries (inline keyboard buttons):

- `confirm:YYYY-MM-DD` — apply a zero-magnitude adjustment with reason "Confirmed via Telegram" (recorded as a new version so the confirmation is auditable). Edit the message to reflect the new state.
- `adjust:YYYY-MM-DD` — set `bot_state.awaiting='adjustment'` for this chat, prompt with examples, await the next text message.
- `lock:YYYY-MM-DD` — lock the latest version. Edit the message to show locked state, remove `Adjust`/`Lock` buttons.

Adjustment grammar (parsed by `grammar.py`):

```
ADJUSTMENT := [SIGN] DURATION WS REASON
SIGN       := '+' | '-'                          # default '-' if unsigned and looks negative-ish, otherwise reject
DURATION   := minutes | 'NNm' | 'Hh' | 'HhMMm' | 'H:MM'
REASON     := non-empty free text, max 200 chars
```

Examples that must parse:
- `-45 lunch` → −45 min, reason "lunch"
- `+30 poller outage 9-11` → +30 min
- `-1h15m doctor's appointment` → −75 min
- `-1:30 GP visit` → −90 min
- `+2h corroborated by Teams` → +120 min

Examples that must reject with helpful error text:
- `confirm but I left at 4` (no duration token)
- `-45` (no reason)
- `lunch -45` (reason before duration)
- `−45 lunch` (en-dash, not minus — reject; prevent silent acceptance of typographic minus)

Once an adjustment is successfully parsed while `awaiting='adjustment'`, the bot calls the same internal API as the web UI (do not duplicate the adjust logic) and replies with the new computed/claimed/version state plus `[🔒 Lock] [✏ Re-adjust]`.

**7.G Bot bring-up and lifecycle**

- On FastAPI startup: if `TELEGRAM_BOT_TOKEN` is set, initialise the adapter. In webhook mode, register the webhook. In polling mode, start the background task.
- If `TELEGRAM_BOT_TOKEN` is unset or empty, the bot is disabled entirely with a single INFO log line. The rest of the app must continue to function.
- All inbound updates and outbound messages logged to `bot_messages` regardless of processing outcome.

**Tests**

- `tests/test_telegram_grammar.py`: parametrised positive and negative cases for `parse_adjustment`. Cover every example above plus malformed inputs.
- `tests/test_telegram_conversation.py`: state-machine tests. Each test constructs an `IncomingEvent`, a `bot_state`, a stub DB reader, and asserts on the returned list of `OutgoingAction`. Cover: `/yesterday` first time, `/yesterday` after lock, callback `adjust`, free-text adjustment while awaiting, free-text while not awaiting (ignored or helpful reply), unauthorised user (one rejection, then silent).
- `tests/test_telegram_adapter.py`: with httpx mocked, verify outbound API calls have correct shape (sendMessage payload, inline_keyboard structure). Use a fixture set of realistic Telegram update payloads in `tests/fixtures/telegram_updates_*.json` for inbound parsing.
- Idempotency test: posting the same update_id twice results in a single `bot_messages` row and a single side effect.

**Acceptance**

- An authorised Telegram user can complete the full daily review cycle through the bot: `/yesterday` → tap `Adjust` → reply `-45 lunch` → tap `Lock`. The result is visible in the web UI calendar as locked, with `created_by='telegram'` on the relevant `daily_summaries` versions.
- An unauthorised user messaging the bot is rejected exactly once and then silently ignored.
- With `TELEGRAM_BOT_TOKEN` unset, the rest of the app builds, starts, and serves the web UI normally.
- Cloudflare Tunnel sidecar starts as part of `docker compose up` and the Telegram webhook is reachable through the public hostname.

## 7. Testing standards

- **Coverage target**: 85% lines for `app/sessions/`, `app/api/`, `app/exporters/`, and `app/notifier/`. The sessionisation module and the notifier conversation/grammar modules are the audit-defence cores; coverage there should be effectively 100% of branches.
- **Style**: pytest, parametrised tests preferred for sessionisation edge cases. One assertion concept per test; multiple `assert` lines fine if they describe the same outcome.
- **Time**: use `freezegun` or `time-machine`. Never rely on wall-clock in tests.
- **Database**: each test uses a fresh in-memory SQLite or a temp file. No shared state between tests.
- **Fixtures**: realistic UniFi response payloads live in `tests/fixtures/`. Capture and sanitise these from a real controller; do not invent fields the controller does not emit.

## 8. Coding standards

- Type hints everywhere. `mypy --strict` clean for `app/`. Tests may use `# type: ignore` sparingly.
- Functions in `app/sessions/builder.py` are pure where possible. The DB persistence is a thin wrapper.
- Prefer dataclasses or Pydantic models for in-memory structures; avoid raw dicts in interfaces between modules.
- No bare `except:`. Catch the specific exceptions you can handle; let the rest propagate.
- Log at INFO for state transitions (poll succeeded, sessioniser ran, day locked) and DEBUG for per-event details. Never log credentials or full client payloads at INFO.
- Commit messages: one logical change per commit, imperative mood, reference spec section where applicable (e.g. `sessions: implement gap-bridging per HANDOFF §6.3`).

## 9. Definitely-not-doing list

These have been considered and rejected; do not introduce them without a spec change first.

1. **Multi-user auth.** Single user, LAN-bound.
2. **Cloud sync.** Backups are the user's problem off-box.
3. **Mobile app.** Web UI is mobile-responsive; that is enough.
4. **Integrations with Microsoft 365 / Google / Teams.** Tempting for corroboration but adds auth complexity and out-of-scope data flows.
5. **Inferred work from network activity volume.** Explicit SSID association is the only signal.
6. **Auto-truncation of long days.** Flag, do not modify.
7. **Editing observations.** Append-only forever.
8. **Per-task time tracking.** This is a logbook, not a productivity tool.

## 10. Out-of-band concerns the agent should flag

If you encounter any of the following, stop and write a note in the PR or commit body rather than working around:

- The local UniFi controller does not return enough data to derive `last_seen` per client.
- iOS per-SSID MAC randomisation produces a fresh MAC on every connection (it should be stable per SSID; if it is not, the device-tracking model breaks).
- A new ATO ruling supersedes PCG 2023/1 in a way that materially affects what records are required.
- SQLite is showing lock contention under the poll-every-60s workload (it should not, by orders of magnitude — investigate before working around).
- Any case where the methodology document and the code diverge.

## 11. Definition of done for the whole project

The project is "done" when:

1. All seven phases above are merged.
2. A user can run `docker compose up`, configure their UniFi credentials, work SSID, and (optionally) Telegram bot token, and produce an XLSX export of a populated year.
3. The methodology document in the XLSX export reflects the live configuration.
4. All raw observations from the year are retrievable and have never been mutated.
5. The README Quick Start describes the bring-up accurately, including the optional Telegram + Cloudflare Tunnel setup.
6. Test coverage targets in §7 are met.
7. `docs/METHODOLOGY.md` and `HANDOFF.md` are mutually consistent and reflect what the code actually does.
8. An authorised Telegram chat can drive a full review cycle and the result is reflected in the web UI; with the bot token unset, the rest of the app behaves identically.

After that, this project is in maintenance mode. The author runs it for a year, files a tax return with the output, and either declares victory or opens issues for v2.
