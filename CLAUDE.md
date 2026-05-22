# CLAUDE.md

Conventions for AI-agent contributors (Claude Code primarily) working in this repository. Humans should follow them too.

---

## Source of truth

This project has three spec documents. They are read top-down on every non-trivial change:

1. **`HANDOFF.md`** — the implementation contract. *What* to build, in what phases, with what acceptance criteria. If code disagrees with `HANDOFF.md`, the code is wrong.
2. **`docs/ARCHITECTURE.md`** — the design rationale. *Why* the design is the way it is. When `HANDOFF.md` and `ARCHITECTURE.md` disagree, `HANDOFF.md` wins and the architecture doc is updated to match.
3. **`docs/METHODOLOGY.md`** — the ATO-facing document describing how hours are derived. It is the document the taxpayer hands to a tax officer. If the code computes hours differently from how `METHODOLOGY.md` says it does, *the code is wrong* — the methodology doc is the contract with the outside world, not just an internal note.

Design changes are made by editing the relevant spec document **first**, then code. If you find yourself wanting to deviate, stop and propose the change in the PR description rather than guessing. Ambiguity resolved silently is the most expensive kind of bug in a project where the output is supposed to be defensible under audit.

---

## Real Data First

**Hard rule.** If this tool reads, parses, or otherwise depends on the shape of an external artifact at runtime — UniFi controller JSON responses, Telegram update payloads, Cloudflare Tunnel config, anything fetched at runtime — **fetch a real sample first**. Build production code AND test fixtures off that sample.

**Never invent the schema** from documentation, intuition, or what "obviously must" be there. The Telegram Bot API docs are accurate but not exhaustive; UniFi response shapes vary across controller versions (Classic vs UDM vs self-hosted) and firmware updates change field names.

**Why this section exists.** This pattern has bitten me on previous projects: synthetic fixtures encode the schema we *thought* was there, tests pass, real runs hit "field not found" the moment the code touches a live controller. The cost of getting this wrong is large (tests pass, real runs fail later); the cost of asking is one chat turn.

**Operationally:**

1. Before adding the UniFi client, the Telegram adapter, or any parser that names fields from an external source, run one real fetch. Save a representative slice into `tests/fixtures/` (sanitised — strip MACs you don't want public, redact bot tokens). The script that re-fetches the artifact lives in `tools/`.
2. Build the synthetic fixtures in `tests/conftest.py` to mirror that real sample's **exact** field names and structure. Synthetic *values* are fine; synthetic *schema* is not.
3. **If you can't run the fetch right now** — Chris's UniFi isn't reachable, the Telegram webhook isn't set up yet, anything — say so explicitly in the PR description and stop. Don't guess. Don't ship code referencing schemas you haven't seen.

**The acid test.** For any external field, path, or filename your code or spec mentions, you should be able to point at either:
- a fixture file in `tests/fixtures/` derived from a real fetch, or
- a `tools/` script that re-fetches the live artifact reproducibly.

If you can't, you're guessing. Stop and fetch — or stop and ask.

---

## Workflow

1. Pick a phase from `HANDOFF.md` §6. **One phase per PR.** Don't bundle phases.
2. Read the relevant section end-to-end before writing code. Each phase has explicit deliverables and acceptance criteria.
3. Implement. Add tests. Run `make lint typecheck test` until green.
4. Open a PR. Reference the spec section (e.g. "implements HANDOFF §6 Phase 3"). Note any deviations in the description.
5. If a deviation requires a spec change, edit the spec document **in the same PR** so reviewer can see the design change alongside the code change.

### Parallel work via git worktrees

Agents handling multiple PRs in parallel may create scratch worktrees under `.claude/worktrees/<branch-slug>/`. That directory is **gitignored** and **excluded from ruff/mypy/pytest** (see `.gitignore` and `pyproject.toml`). Don't add lint or test config that bypasses those excludes — running `ruff check .claude/...` explicitly re-introduces duplicate findings and stale-branch noise.

When done with a worktree, clean it up:

```bash
git worktree remove .claude/worktrees/<slug>
git worktree prune
```

---

## Tech stack (locked)

Detail lives in `HANDOFF.md` §3; this is the quick reference:

- **Python 3.12+**, modern syntax (`from __future__ import annotations`, `|` unions, `match`).
- **FastAPI** + **APScheduler** (in-process).
- **SQLAlchemy 2.x** + **Alembic** + **SQLite**.
- **`httpx`** for all outbound HTTP (UniFi, Telegram). Do **not** introduce `python-telegram-bot`, `aiogram`, or other framework libraries — the adapter is intentionally thin.
- **`pydantic-settings`** for config, **`pydantic`** v2 for models.
- **Jinja2** + **HTMX** for the web UI. No SPA framework.
- **`openpyxl`** for XLSX.
- **`pytest`** + **`time-machine`** for tests.
- **`ruff`** (lint + format), **`mypy --strict`** for `app/`.
- **`uv`** for package management.

Adding a runtime dependency requires a written justification in the commit message or PR description.

---

## Commands

```bash
# Install (editable + dev extras)
uv pip install -e ".[dev]"

# Run tests
pytest

# Lint + format
ruff check .
ruff format .

# Type check
mypy app/

# DB migrations
alembic upgrade head
alembic revision --autogenerate -m "description"

# Run locally (without Docker)
uvicorn app.main:app --reload --port 8088

# Docker bring-up
docker compose up --build

# Ad-hoc sessioniser run for a date
python -m app.sessions --date 2026-05-20

# XLSX export for a financial year
python -m app.exporters --fy 2025-26 --out /tmp/wfh-2025-26.xlsx
```

The Makefile (Phase 6) wraps these.

---

## Conventions

- **Type hints everywhere.** `mypy --strict` passes for `app/` before committing.
- **Pydantic v2 models** for config and any structured intermediate data.
- **Tests next to features.** Every new module gets a corresponding `tests/test_<module>.py`. No new logic merges without a test.
- **Mock all network in tests.** Tests must not hit a real UniFi controller, real Telegram, or real Cloudflare. Use `httpx`'s `MockTransport` or `respx`. Hermetic by default.
- **Real-network probes** live in `tools/` (mirroring the pattern from `abs-census-augmentor`). Opt-in, run manually, never gate CI.
- **Small functions, pure where possible.** Side effects (I/O, HTTP, DB) live at the edges. `app/sessions/builder.py` and `app/notifier/conversation.py` are pure — keep them that way.
- **Errors loud and helpful.** Pydantic validation errors with context, not bare exceptions. No bare `except:`.
- **No `print`.** Use `logging`. Each module gets `logger = logging.getLogger(__name__)`.
- **No hardcoded paths.** Paths come from `app.config`. If you need a new path, add it to `config.py`.
- **`pyproject.toml` version and `CHANGELOG.md` move together.** Any release section in `CHANGELOG.md` (anything not under `[Unreleased]`) updates `[project].version` in `pyproject.toml` in the same commit. Don't let installed-from-`main` artifacts ship metadata for an older version than the code.
- **Time is UTC at rest, local at the edges.** Stored timestamps are ISO-8601 UTC. Daily-attribution dates are local-time per `ARCHITECTURE.md` §5.3. Never store naive datetimes.
- **Secrets never logged.** UniFi credentials, Telegram bot token, Cloudflare tunnel token: configured via `.env`, never echoed by the API, never logged at INFO or above. DEBUG may log redacted forms.

---

## Testing

- **Hermetic.** Tests don't touch the network. HTTP mocked via `respx` or `httpx.MockTransport`. DB is a fresh in-memory SQLite or `tmp_path` per test.
- **Sessionisation is the audit-defence core.** Effectively 100% branch coverage required in `app/sessions/`. Every rule in `METHODOLOGY.md` §4 has at least one corresponding test that names the rule in its docstring.
- **Notifier core is also audit-defence.** The grammar parser (`app/notifier/grammar.py`) and conversation state machine (`app/notifier/conversation.py`) get the same coverage discipline. Adjustments made via Telegram are claimed hours; the parser cannot silently misinterpret input.
- **Immutability is testable.** `tests/test_immutability.py` verifies that `UPDATE`/`DELETE` against `observations` and `bot_messages` raises. Don't disable or skip these tests.
- **Time-dependent tests use `time-machine`.** Never rely on wall-clock. Tests that depend on "today" or "yesterday" freeze time first.
- **Don't commit large fixtures.** `tests/fixtures/` holds small, representative JSON slices from real UniFi/Telegram responses, sanitised. If you need a multi-MB capture, gitignore it and put the re-fetch script in `tools/`.

---

## What Not To Do

These are considered and rejected. Don't reintroduce them without a spec change first.

1. **Do not mutate `observations` or `bot_messages`.** Append-only forever. No `UPDATE`, no `DELETE`, not even a migration that "just cleans up old rows." If you genuinely need to remove data (e.g. user requested account deletion), that's a spec change and a discussion, not a one-liner.
2. **Do not overwrite `daily_summaries` rows on edit.** Adjustments and locks create new versioned rows. See `ARCHITECTURE.md` §5.5.
3. **Do not invent UniFi or Telegram schema details from documentation or intuition.** See "Real Data First" above.
4. **Do not introduce framework libraries** for Telegram (`python-telegram-bot`, `aiogram`) or for the web UI (React, Vue, Svelte). The stack is locked in `HANDOFF.md` §3 for a reason — minimal, boring, easy to back up and reason about.
5. **Do not bypass the internal API from the Telegram bot.** The bot calls `POST /api/days/{date}/adjust` and `POST /api/days/{date}/lock` — same code paths as the web UI. Don't reach into the DB directly from the notifier package.
6. **Do not log secrets**, full Telegram payloads at INFO, or UniFi credentials anywhere.
7. **Do not auto-truncate "long days."** Flag for review in the UI; never modify computed totals silently.
8. **Do not load assets from CDNs** in the web UI. HTMX is vendored. Same for any future JS/CSS dependency.
9. **Do not implement features that aren't in `HANDOFF.md`** without proposing a spec edit first.
10. **Do not weaken the methodology** to make code easier. If the code can't implement the methodology, change the methodology *deliberately* (with rule_version bump) — don't quietly diverge.

---

## Logging conventions

- **INFO**: state transitions worth seeing in normal operation — poll succeeded, sessioniser ran for a date, day locked, bot received an authorised command, backup completed.
- **DEBUG**: per-event detail useful only when debugging — individual observation rows, raw Telegram payload contents (redacted), per-client UniFi fields.
- **WARNING**: data-quality issues that don't block progress — a poll cycle failed but next one succeeded, an unauthorised Telegram user attempted contact, a session looked anomalous but was preserved.
- **ERROR**: hard failures — DB unreachable, Telegram webhook registration failed at startup, sessioniser raised.

Every poll cycle logs success/failure at INFO with a duration. Every sessioniser run logs `(date, sessions_built, computed_hours)` at INFO. Every adjustment logs `(date, channel, signed_minutes, version)` at INFO. **Never** log the adjustment *reason* string at INFO above DEBUG — reasons are user-authored text and may contain personal context.

---

## File ownership

| File / directory | Owner | Notes |
|---|---|---|
| `HANDOFF.md` | maintainer + agent | Source of truth for *what* to build. Spec changes require justification in PR. |
| `docs/ARCHITECTURE.md` | maintainer + agent | Source of truth for *why* the design is what it is. Must stay consistent with `HANDOFF.md`. |
| `docs/METHODOLOGY.md` | maintainer | The contract with the ATO. Changes need maintainer review; bump rule_version when the method changes. |
| `CLAUDE.md` | maintainer | Convention changes only via maintainer review. |
| `README.md` | maintainer + agent | Update when public-facing behaviour or quick-start commands change. |
| `pyproject.toml` | maintainer + agent | Add deps freely with justification; remove deps via PR. |
| `LICENSE` | maintainer | Don't touch. |
| `alembic/versions/` | maintainer + agent | Each migration is append-only after merge. Never edit a merged migration; write a new one. |
| `app/sessions/` | maintainer + agent | Audit-critical. Coverage discipline above; rule_version bump on behaviour change. |
| `app/notifier/conversation.py` + `grammar.py` | maintainer + agent | Same discipline as sessions. |
| `tests/fixtures/` | maintainer + agent | Sanitised real samples. Don't replace with synthetic schemas. |
| `tools/` | maintainer + agent | Real-network probes. Never run in CI. |

---

## When Stuck

In order of preference:

1. **Re-read the relevant spec section.** It might already answer the question.
2. **Check the design rationale** in `ARCHITECTURE.md` for the *why* — a decision that looks arbitrary in `HANDOFF.md` often has a documented reason there.
3. **Open a draft PR with the question** in the description, leaving the code blocked at the unclear point. The maintainer responds.
4. **Make the conservative choice and call it out** as a `TODO(spec)` comment in the code, with the question in the PR description.

Do *not*:
- Silently invent behaviour and assume it's right.
- Bundle a spec change with implementation in a way that makes the spec change hard to spot in review.
- Touch files outside the phase you're working on.
- Ship code that depends on UniFi/Telegram schema details you haven't seen in a real response.

---

## Phase status

Tracked in `HANDOFF.md` §6. When you complete a phase, update its status in the spec and reference the merged PR.
