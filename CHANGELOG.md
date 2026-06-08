# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

### Pending

- **Phase 2 (UniFi client + poller)**: blocked on capturing a real UniFi
  controller response sample per CLAUDE.md "Real Data First". Will be
  unblocked in the morning after fetching `tests/fixtures/unifi_clients_*.json`.
- **Phase 7 (Telegram bot)**: blocked on capturing real Telegram update
  payloads for `tests/fixtures/telegram_updates_*.json`.

[Unreleased]: https://github.com/USERNAME/REPO/commits/main
