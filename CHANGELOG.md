# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Review queue** (`/review-queue`, `GET /api/review-queue`): days needing
  attention — unlocked backlog (no statute of limitations), anomalous days,
  detected in-session data gaps (poller outage / host sleep, distinguished
  from genuine absence via disconnect rows), heavy-bridging days. Gap windows
  shown with local times for corroborate-and-adjust per METHODOLOGY §4.6.
- **Audit bundle export** (`GET /api/export.bundle?fy=`, year-view button):
  one zip with the XLSX, populated methodology, raw observations/sessions
  CSVs, ALL daily-summary versions, and a manifest carrying SHA-256 hashes,
  row counts, config snapshot, and rule version. Stdlib only.
- **Year-view statistics**: weekly average, projected year-end hours at
  current pace, locked-progress, per-weekday averages. Hours only — no
  dollar figures (HANDOFF §2.5), enforced by test.
- **NAS deployment + backups UX**: `/system` page with health summary,
  Back-up-now, snapshot list + downloads; `POST /api/backup`,
  `GET /api/backups[/{name}]` with strict name validation; image
  HEALTHCHECK + arbitrary-UID support (verified live under unRAID's
  `--user 99:100`); GHCR publish workflow; `docs/DEPLOYMENT.md` with
  unRAID bring-up and a test-automated restore procedure.

### Pending

- **Phase 7 (Telegram bot)**: blocked on capturing real Telegram update
  payloads for `tests/fixtures/telegram_updates_*.json` per CLAUDE.md
  "Real Data First". The web UI remains the sole review channel until then.
- **Classic-controller adapter** (Cloud Key Gen1/Gen2, self-hosted):
  detected and rejected with guidance; an adapter lands when a real classic
  fixture is contributed via `tools/fetch_unifi_sample.py`.

## [0.1.0] - 2026-06-10

First working release: a UDM-line UniFi controller feeds an append-only
observations log, sessionisation derives reviewable daily totals, the web UI
covers the daily review-and-lock cycle, and the year-end XLSX export carries
the populated methodology document. Verified live against a real Dream
Machine. Phases 1-6 of HANDOFF §6 complete; Phase 7 (Telegram) pending.

### Added

- Initial project documentation: README, ARCHITECTURE, METHODOLOGY template.
- HANDOFF.md implementation brief for Claude Code, covering seven delivery phases.
- Phase 7 spec for an optional Telegram bot daily-review channel, served via Cloudflare Tunnel.
- CLAUDE.md conventions for AI-agent contributors, including the Real Data First rule for external schemas and the worktree exclusion pattern.
- MIT licence.
- `.devcontainer/devcontainer.json`: VS Code dev environment (Python 3.12, uv,
  Ruff, Mypy --strict). Inner-loop only; compose stack runs on host.
- **Phase 1 (skeleton + data layer)**: SQLAlchemy models for observations,
  sessions, daily_summaries, config, devices, poller_state. Alembic initial
  migration with append-only triggers on `observations`. FastAPI app with
  `/api/health`. Dockerfile (multi-stage, non-root, /data volume).
  docker-compose.yml (app + optional cloudflared sidecar under `--profile
  tunnel`). Immutability test verifying BOTH ORM hooks and SQL triggers
  block UPDATE/DELETE.
- **Phase 2 (UniFi client + poller)**: `ControllerAdapter` protocol with a
  UDM-line adapter built strictly against a sanitised fixture captured from
  a real controller (`tests/fixtures/unifi_clients_active.json`); key schema
  facts encoded: SSID lives in `essid`, timestamps are unix-epoch ints,
  `signal` is dBm. Flavour auto-detection; classic controllers rejected with
  fixture-contribution guidance. Poller filters to work-SSID + tracked MACs,
  writes connect rows and DB-derived disconnect-transition rows (restart-safe),
  tracks consecutive failures for `/api/health`, and never crashes the
  process on transient controller errors. Devices table seeded from
  `WORK_DEVICE_MACS`. Verified live: first real observation row captured
  from the maintainer's network.
- **Phase 3 (sessionisation)**: Pure `build_sessions_for_date` per
  ARCHITECTURE §5.2 (per-MAC interval state machine → sweep-line union →
  gap-bridging → min-session filter → midnight-crossing attribution to
  start date). Persistence layer that replaces sessions rows transactionally
  and creates a new daily_summaries version only when `computed_seconds`
  changes (CLAUDE.md "never overwrite" honoured). APScheduler nightly job
  at 01:15 local. CLI: `python -m app.sessions --date / --nightly-window
  --dry-run`.
- **Phase 4 (review API + web UI)**: `/api/days/...` JSON endpoints
  (list/detail/adjust/lock/resessionise). Server-rendered web UI with
  HTMX 2.0.3 **vendored** locally (no CDN). Three primary screens:
  today/yesterday review, 90-day calendar, AU financial-year view.
  Stale-poll banner if last_poll_succeeded_at > 30 min old.
- **Phase 5 (exports + backups)**: XLSX export with Summary / Year total /
  Methodology sheets, blank ATO fixed-rate cell with explanatory comment,
  `=B3*B8` dollar formula, configuration snapshot + device list +
  disclaimer. CSV variant. SQLite `VACUUM INTO` nightly snapshots at 02:00
  local with 30-daily + 12-monthly (first-of-month) retention.
- **Phase 6 (hardening)**: Structured JSON logging to stdout with
  `LOG_FORMAT=text` fallback. `/api/health` enriched with `db_size_bytes`
  and `observations_last_24h`. Makefile (help/dev/test/lint/format/
  typecheck/migrate/revision/docker-build/docker-up/docker-down/export-xlsx).
  SECURITY.md. README quick start with podman/docker, devcontainer pointer,
  export examples, and Telegram + Cloudflare Tunnel setup.
- Real-network probes in `tools/`: `fetch_unifi_sample.py` (capture +
  sanitise controller fixtures) and `live_poll_check.py` (one live poll
  cycle, optionally writing through to the DB).

### Fixed

- Dockerfile: builder stage no longer COPYs files excluded by
  `.dockerignore`; CMD invokes `python -m alembic` / `python -m uvicorn`
  because `--target`-installed dependencies don't put console scripts on
  PATH. Image now builds and serves `/api/health` 200 (verified with
  podman 5.8.2).

[Unreleased]: https://github.com/USERNAME/REPO/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/USERNAME/REPO/releases/tag/v0.1.0
