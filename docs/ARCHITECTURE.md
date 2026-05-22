# Architecture

This document is the technical reference for the WFH Logbook. Sections are numbered so they can be referenced precisely from issues, commit messages, and prompts to coding agents (e.g. "implement §4.2", "fix per §6.3").

When this document and `HANDOFF.md` disagree, `HANDOFF.md` is the authoritative implementation brief; this document is the design rationale. Both should be updated when the design changes; spec-first, code second.

---

## 1. Goals and non-goals

### 1.1 Goals

1. Produce a contemporaneous, daily record of hours worked from home for the whole income year, suitable for the ATO fixed-rate WFH deduction (PCG 2023/1).
2. Make the underlying evidence (connection events) immutable.
3. Make the human review step lightweight (≤30 seconds/day) so it actually happens.
4. Produce an audit-ready export (XLSX) and retain raw evidence for at least five years.
5. Run entirely on the user's own infrastructure with no external dependencies at runtime beyond the local UniFi controller.

### 1.2 Non-goals

1. Replacing employer-side time tracking (Teams, JIRA, payroll). This is a personal record.
2. Activity tracking on the device (keystrokes, screen time). The signal is network association only.
3. Multi-tenant SaaS. One user, one home network, one server.
4. Real-time alerting on work patterns. Daily review is the cadence.
5. Anything that improves accuracy at the cost of methodological simplicity. The audit defence is "I applied the same documented rule every day," not "my system is clever."

## 2. Design principles

### 2.1 Explicit intent over inference

The signal that triggers a "work" event is the user manually associating a device to a dedicated SSID (`WFH`). This makes intent legible and contemporaneous. Inferred work-from-network-activity heuristics are rejected.

### 2.2 Raw evidence is immutable

The `observations` table is append-only. No code path mutates a row once written. All adjustments occur in downstream tables that reference the immutable observations.

### 2.3 Document the method, then mechanise

The methodology document (`docs/METHODOLOGY.md`) is the contract. The code implements it. When the code and the document disagree, the document wins and the code is fixed — not the other way around.

### 2.4 Human-in-the-loop, with audit trail

A computed daily total is a draft. The user confirms or adjusts it. Adjustments are versioned, reason-tagged, and never overwrite the computed value.

### 2.5 Boring tech, easy to back up

Python, SQLite, XLSX. The whole dataset for a year fits in a small file you can email yourself.

## 3. System overview

### 3.1 Components

| # | Component | Responsibility |
|---|---|---|
| 1 | UniFi controller | Source of truth for client association events. Out of scope for this codebase; assumed to exist. |
| 2 | Poller | Periodically queries the UniFi controller and writes `observations` rows. |
| 3 | Sessioniser | Nightly job that derives `sessions` and `daily_summaries` from `observations`. |
| 4 | API (FastAPI) | Read/write endpoints for the UI, the bot, and exports. |
| 5 | Web UI | Browser-based daily review and historical browsing. |
| 6 | Exporter | Produces XLSX/CSV outputs for tax filing and audit. |
| 7 | Backup job | Snapshots SQLite to a second location nightly. |
| 8 | Notifier (Telegram bot) | Second daily-review channel. Peer with the web UI, not a wrapper around it. Optional — disabled if no bot token configured. |
| 9 | Cloudflare Tunnel sidecar | Provides public HTTPS ingress for the Telegram webhook without opening home-network ports. Only present in webhook mode. |

### 3.2 Process boundaries

All components except the UniFi controller run inside a single Docker container, supervised by the FastAPI process. APScheduler runs the poller, the nightly sessioniser, and the backup job in-process. This is deliberately simple; if the workload ever grows, the poller can be split out, but for one user it does not need to be.

### 3.3 Deployment topology

```
                                                +-----------------------+
                                                | Telegram (cloud)      |
                                                +-----------+-----------+
                                                            |
                                                  webhook POST (HTTPS)
                                                            |
                                                  +---------v---------+
                                                  | Cloudflare        |
                                                  | (public hostname) |
                                                  +---------+---------+
                                                            |
                                                  outbound tunnel (no inbound port)
                                                            |
+----------------+         poll (HTTPS,         +-----------v----------+
| UniFi          |<------- local LAN) ----------| WFH Logbook          |
| Controller     |                              | Docker container     |
| (UDM / CK /    |                              |  - FastAPI           |
|  self-hosted)  |                              |  - APScheduler       |
+----------------+                              |  - Notifier (bot)    |
                                                |  - SQLite (volume)   |
                                                +----------+-----------+
                                                           |
                                  user browser (LAN) ------+ HTTP UI :8088
                                                           |
                                                           +-- nightly XLSX export
                                                           +-- nightly SQLite snapshot

  + cloudflared sidecar container running alongside the app container; holds the
    outbound tunnel to Cloudflare and forwards inbound webhook traffic to app:8088.
```

## 4. Data model

### 4.1 `observations` (immutable)

One row per device per poll cycle where the device is observed on the work SSID, plus one row for state transitions (last-known-connected → disconnected) detected by the poller.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | auto |
| `observed_at` | TEXT (ISO8601 UTC) | timestamp when the poller recorded this |
| `controller_seen_at` | TEXT (ISO8601 UTC) | `last_seen` from the controller for this client at this poll, if available |
| `mac` | TEXT | client MAC (per-SSID private MAC for iOS) |
| `device_label` | TEXT | human label resolved from config, e.g. "iPhone" |
| `ssid` | TEXT | the SSID name; always the work SSID for stored rows |
| `is_connected` | INTEGER (0/1) | 1 = currently associated, 0 = transition to disconnected |
| `signal_dbm` | INTEGER | optional, for diagnostics |
| `raw_json` | TEXT | raw client payload from the controller, JSON-encoded, for forensic use |

Indexes: `(mac, observed_at)`, `(observed_at)`.

No `UPDATE` or `DELETE` statements are issued against this table by application code. It can only grow.

### 4.2 `sessions` (derived, regeneratable)

One row per contiguous period during which at least one tracked device was associated with the work SSID, with gap-bridging applied.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | auto |
| `local_date` | TEXT (YYYY-MM-DD) | the local-time date the session is attributed to (see §5.3) |
| `started_at` | TEXT (ISO8601 UTC) | first observed-connected timestamp |
| `ended_at` | TEXT (ISO8601 UTC) | last observed-connected timestamp before final disconnect |
| `duration_seconds` | INTEGER | `ended_at - started_at` |
| `devices_seen` | TEXT | comma-separated device labels that contributed |
| `bridged_gaps_count` | INTEGER | number of gap-bridges applied to construct this session |
| `bridged_gaps_seconds` | INTEGER | total bridged time |
| `created_at` | TEXT (ISO8601 UTC) | when the row was written |
| `rule_version` | TEXT | version of sessionisation rules used (see §5.6) |

This table is regeneratable from `observations` at any time. The sessioniser deletes and rewrites rows for a given `local_date` on each run.

### 4.3 `daily_summaries` (reviewable, versioned)

One row per local date *per version*. The most recent unlocked row is the "current" summary; once locked, further edits create new rows.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | auto |
| `local_date` | TEXT (YYYY-MM-DD) | the date |
| `version` | INTEGER | starts at 1, increments per edit |
| `computed_seconds` | INTEGER | sum of session durations as computed by sessioniser |
| `adjustment_seconds` | INTEGER | signed; negative = deducted (lunch etc.), positive = added (rare) |
| `adjustment_reason` | TEXT | free text, required if `adjustment_seconds != 0` |
| `claimed_seconds` | INTEGER | `computed_seconds + adjustment_seconds`, never negative |
| `locked` | INTEGER (0/1) | once locked, no further edits to this version |
| `locked_at` | TEXT (ISO8601 UTC) | when locked |
| `created_at` | TEXT (ISO8601 UTC) | when this version was written |
| `created_by` | TEXT | `'sessioniser'`, `'web'`, or `'telegram'` |
| `rule_version` | TEXT | sessionisation rule version applied |

Indexes: `(local_date, version)` unique; `(local_date)` for lookups.

### 4.4 `config` (single-row)

Holds the canonical configuration that the methodology document references. One row, updated rarely.

| Column | Type |
|---|---|
| `work_ssid` | TEXT |
| `gap_bridge_minutes` | INTEGER |
| `min_session_minutes` | INTEGER |
| `daily_cap_hours` | INTEGER |
| `local_timezone` | TEXT (IANA, e.g. `Australia/Sydney`) |
| `rule_version` | TEXT |
| `updated_at` | TEXT (ISO8601 UTC) |

Devices are configured in a separate `devices` table: `(mac, label, active_from, active_to)`. iOS per-SSID MACs change rarely, but if one changes the old row is end-dated and a new row inserted.

### 4.5 `bot_chats`

One row per Telegram chat the bot has interacted with.

| Column | Type | Notes |
|---|---|---|
| `chat_id` | INTEGER PK | Telegram chat id |
| `telegram_user_id` | INTEGER | the user who first messaged this chat |
| `authorised` | INTEGER (0/1) | derived from the env allowlist at the time of first contact; refreshed on each inbound message |
| `first_seen_at` | TEXT (ISO8601 UTC) | |
| `last_seen_at` | TEXT (ISO8601 UTC) | |

### 4.6 `bot_state`

Per-chat conversation state. Small. Used to remember "the bot asked the user for an adjustment for date X — the next free-text message is that adjustment."

| Column | Type | Notes |
|---|---|---|
| `chat_id` | INTEGER PK FK → `bot_chats` | |
| `awaiting` | TEXT | currently one of `'adjustment'` or NULL; extension point |
| `awaiting_date` | TEXT (YYYY-MM-DD) | populated when `awaiting='adjustment'` |
| `updated_at` | TEXT (ISO8601 UTC) | |

State is reset (`awaiting=NULL`) after the awaited input is consumed, or on any new slash command, or after 30 minutes of inactivity.

### 4.7 `bot_messages` (immutable)

Append-only audit log of everything that passed through the bot in either direction. Forms part of the audit evidence for adjustments made via Telegram, alongside `daily_summaries`.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | auto |
| `chat_id` | INTEGER | |
| `direction` | TEXT | `'in'` or `'out'` |
| `telegram_update_id` | INTEGER | inbound only; unique index where not null (idempotency) |
| `telegram_message_id` | INTEGER | outbound only; the id Telegram assigned |
| `text` | TEXT | for quick browsing |
| `raw_json` | TEXT | the full Telegram payload, JSON-encoded |
| `created_at` | TEXT (ISO8601 UTC) | |

The same immutability rule that applies to `observations` (§4.1) applies here: no application path may `UPDATE` or `DELETE` rows.

## 5. Algorithms

### 5.1 Polling

Every 60 seconds, the poller:

1. Calls the UniFi clients endpoint (or its v2 equivalent) for the work site.
2. Filters to clients on the work SSID whose MAC matches an active row in `devices`.
3. For each such client, writes an `observations` row with `is_connected=1`.
4. Compares with the previous poll's set of connected work-MACs; for any MAC present last poll but absent this poll, writes an `observations` row with `is_connected=0` at the controller's reported `last_seen` if available, otherwise the current time.

### 5.2 Sessionisation

Run nightly at 01:15 local time for "yesterday and any non-locked prior day in the trailing 7 days." Also runnable ad-hoc per date via the API.

For each in-scope date:

1. Load all observations whose `controller_seen_at` (or `observed_at` if absent) falls within the local day's UTC range (with a small buffer on each side to catch sessions crossing midnight; see §5.3).
2. Build per-MAC contiguous "device-connected" intervals from the observation stream.
3. Merge intervals across MACs into a union timeline (any device connected ⇒ open interval).
4. Apply gap-bridging: if two intervals in the union are separated by ≤ `gap_bridge_minutes`, merge them and increment `bridged_gaps_*`.
5. Drop intervals shorter than `min_session_minutes`.
6. Attribute each resulting interval to a `local_date` per §5.3.
7. Replace existing `sessions` rows for the date with the new set.
8. Recompute the unlocked `daily_summaries` row for the date, or insert one if none exists.

### 5.3 Midnight-crossing sessions

A session that spans local midnight (e.g. 22:30 → 01:30) is attributed to the calendar date on which it *starts*. The next day's record is unaffected. This is documented in `METHODOLOGY.md` so the rule is part of the methodology, not a code quirk.

### 5.4 Daily cap

If a computed `claimed_seconds` exceeds `daily_cap_hours * 3600`, the day is flagged as `anomalous` in the API response but not auto-truncated. The user reviews and decides.

### 5.5 Manual adjustment

The user submits `(local_date, adjustment_seconds, reason)`. The server:

1. Looks up the latest version for that date.
2. If locked, creates a new version with `version = old + 1`, copying `computed_seconds`, applying the new adjustment, and setting `created_by='user'`.
3. If unlocked, creates a new version (same logic — adjustments are *always* a new row, never an in-place update).

Locking is a separate operation that sets `locked=1, locked_at=now()` on the latest version. After locking, the next adjustment will create version+1, which itself starts unlocked.

### 5.6 Rule versioning

`rule_version` is a short string (e.g. `2026.1`) bumped whenever any of `gap_bridge_minutes`, `min_session_minutes`, the midnight-crossing rule, or the sessionisation algorithm change. Every session and summary records the rule version it was computed under. The methodology document records the history of rule versions.

### 5.7 Bot conversation logic

The bot is an additional channel into the existing daily-review flow. It does not implement its own adjustment, lock, or sessionisation logic — it calls the same internal API the web UI uses. This guarantees parity: an adjustment made via Telegram is indistinguishable downstream from one made via the web UI, except for the `created_by` stamp.

Conversation is modelled as a small state machine, per chat:

```
              /yesterday, /day, /today
                       │
                       ▼
                ┌──────────────┐  callback "adjust"   ┌──────────────────┐
                │  showing day │ ───────────────────► │ awaiting adjust  │
                │   (idle)     │ ◄─────────────────── │  (for date X)    │
                └────┬─────────┘   any new command    └────────┬─────────┘
                     │                                          │
        callback "confirm" / "lock"                  free-text matching grammar
                     │                                          │
                     ▼                                          ▼
              (apply via API, edit                   (parse, apply via API,
               message in place)                      reply with new state)
```

Three rules are non-obvious and worth stating:

1. **The state machine has no rendering responsibility.** `conversation.py` returns abstract `OutgoingAction` objects (`SendMessage`, `EditMessage`, `AnswerCallback`). The adapter turns those into Telegram API calls. The web UI does not consume `OutgoingAction`s; it does its own rendering. The shared core is only the read/write operations against `daily_summaries`.
2. **`awaiting` state is best-effort.** It clears on any new slash command, on a successful adjustment parse, on the 30-minute idle timeout, or on app restart. There is no scenario in which clearing the awaited state produces incorrect data downstream — at worst, a free-text message is ignored or treated as a new command.
3. **Confirmation is a versioned event.** Tapping `Confirm` writes a new `daily_summaries` row with `adjustment_seconds=0` and `adjustment_reason='Confirmed via Telegram'`. This is so that the act of confirmation is itself part of the audit trail, not just a UI affordance.

## 6. Interfaces

### 6.1 UniFi API

Read-only. Local controller authentication via username/password, or API key on controller versions that support it. The poller targets the local API on the controller, not Ubiquiti's cloud. Network failures, auth failures, and unexpected response shapes are logged and the poll is skipped; gaps in observations are tolerated by design (see §7.3).

### 6.2 Internal HTTP API

JSON over HTTP on port 8088 by default. No authentication beyond binding to the LAN (configurable to bind to localhost only and front with a reverse proxy if the user prefers). Endpoints minimum set:

- `GET /api/days?from=YYYY-MM-DD&to=YYYY-MM-DD` — list daily summaries with their sessions.
- `GET /api/days/{date}` — full detail for a date including observations.
- `POST /api/days/{date}/adjust` — apply an adjustment.
- `POST /api/days/{date}/lock` — lock the latest version.
- `POST /api/days/{date}/resessionise` — re-run sessionisation for this date.
- `GET /api/export.xlsx?fy=2025-26` — XLSX export for a financial year.
- `GET /api/export.csv?from=&to=` — CSV export for a range.
- `GET /api/health` — liveness/readiness, last successful poll timestamp, last successful sessioniser run, last successful backup, bot mode and webhook status.

### 6.3 Web UI

Server-rendered with light interactivity (HTMX is a fine choice; React is overkill for this). Three primary screens: today/yesterday review, calendar/list view of the last 90 days, and a year view with the running total and projected deduction.

### 6.4 Telegram bot

Two transport modes, selected by `TELEGRAM_MODE`:

- **Webhook** (default in production): Telegram POSTs updates to `${PUBLIC_BASE_URL}/webhook/telegram/${TELEGRAM_WEBHOOK_SECRET}`. The handler verifies both the path secret and the `X-Telegram-Bot-Api-Secret-Token` header before processing. Ingress is provided by a `cloudflared` sidecar; no inbound ports are opened on the home network.
- **Polling**: a background asyncio task runs `getUpdates` long-polling. Useful for local development, and as a fallback if the tunnel is down for an extended period.

Outbound traffic in both modes goes directly to `https://api.telegram.org`. Authorisation is by allowlist of Telegram user IDs (`TELEGRAM_ALLOWED_USER_IDS`). Unauthorised users receive a single polite rejection and are then silently ignored. All inbound updates and outbound messages are persisted to `bot_messages` (see §4.7) before further processing, so the evidence trail survives processing errors. The bot is disabled entirely if `TELEGRAM_BOT_TOKEN` is unset; the rest of the app must function without it.

## 7. Operational concerns

### 7.1 Backups

Nightly at 02:00 local: SQLite `VACUUM INTO` to `/data/backups/wfh-logbook-YYYYMMDD.sqlite`. Retain 30 daily, 12 monthly. The user is responsible for off-box copies; the README documents a recommended quarterly procedure.

### 7.2 Health and alerting

The UI surfaces a banner if the poller's last success is older than five minutes during likely-work hours, or older than 30 minutes at any time. No external alerting (no email, no push). The signal is the banner the user sees during daily review.

### 7.3 Tolerating outages

If the poller is down for some period, observations will be missing. The sessioniser cannot invent data. The user resolves this via manual adjustment with a documented reason ("poller outage 2026-04-12 between 09:00 and 11:00; corroborated by Teams login records"). The methodology document describes how to handle this case.

### 7.4 Time zones

All timestamps stored in UTC. All user-facing dates and times rendered in the configured local timezone (`Australia/Sydney` by default). Daily-attribution date uses local time per §5.3.

### 7.5 Configuration

A `.env` file holds secrets (UniFi credentials) and operational knobs (poll interval, port). Sessionisation rules live in the database (`config` table) so they're versioned with the data, not lost on container redeploy. Initial values for the `config` row are seeded from `.env` on first start only.

## 8. Security model

This is a single-user, LAN-bound service that, when the bot is enabled, additionally exposes a single public webhook endpoint via Cloudflare Tunnel. Threats considered:

1. **Credential leakage**: UniFi credentials and the Telegram bot token are in `.env`, never logged, never echoed by the API. Use a UniFi local account with read-only scope if available. The bot token grants full control of the bot; rotate via BotFather if compromised.
2. **Casual LAN access**: the UI has no auth and exposes presence data. Users who don't trust their LAN should bind to localhost and access via SSH tunnel.
3. **Backup data exfiltration**: backups are plain SQLite; if copied off-box, the off-box destination is the user's responsibility (encrypt with `age` or similar).
4. **Hostile container compromise**: the container needs outbound HTTPS to the UniFi controller, the Cloudflare edge (for the tunnel), and `api.telegram.org`. It writes to a single volume. It should run as a non-root user.
5. **Unauthenticated webhook hits**: the `/webhook/telegram/{secret}` endpoint is reachable from the public internet. Two layers protect it: the secret path segment (matched against `TELEGRAM_WEBHOOK_SECRET`) and the `X-Telegram-Bot-Api-Secret-Token` header (also matched against the same secret, per the Telegram-supported convention). Either failure returns 401 with no body. The handler additionally only acts on updates whose `from.id` is in `TELEGRAM_ALLOWED_USER_IDS`.
6. **Hostile Telegram users**: anyone can discover a bot's username and send it messages. The allowlist (§6.4) is the defence. Unauthorised users get a single response (so the bot doesn't appear broken to a mis-typing legitimate user) and are then silently dropped to avoid amplification or harassment.
7. **Stolen Cloudflare tunnel**: a compromised `CLOUDFLARE_TUNNEL_TOKEN` allows an attacker to redirect the public hostname. Mitigation: rotate the token from the Cloudflare dashboard; the secret-token verification on the webhook means a redirected endpoint cannot forge valid Telegram payloads without also possessing the bot's secret token.

Out of scope: hardening against a determined attacker on the LAN; multi-user authentication; encryption at rest within the application.

## 9. Glossary

- **Session**: a contiguous period of work-SSID presence after gap-bridging and filtering.
- **Observation**: a single timestamped record that a device was (or was not) associated to the work SSID at a moment in time.
- **Daily summary**: the per-day total presented to the user for review, including computed value and any adjustment.
- **Locked**: a daily summary version that has been finalised by the user; further edits create new versions.
- **Rule version**: a stamp identifying which sessionisation rules were in effect when a session/summary was computed.
- **Bridged gap**: a short period of no device association that the sessioniser treated as part of an enclosing session.
- **Notifier**: the abstract interface a messaging channel implements. Currently only the Telegram adapter implements it.
- **Awaiting state**: the bot's per-chat note that the next free-text message should be interpreted as an adjustment for a specific date, rather than as a generic message.
- **Webhook secret**: the shared string used in both the URL path and the `X-Telegram-Bot-Api-Secret-Token` header for incoming Telegram webhooks.
