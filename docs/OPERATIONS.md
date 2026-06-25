# Operations & Handover

Day-to-day running of a deployed WFH Logbook, and how to pick the project up
on a fresh machine. Complements [`DEPLOYMENT.md`](DEPLOYMENT.md) (first-time
bring-up + restore) — this file is the *ongoing* runbook.

This document is deliberately generic and contains **no secrets and no
home-network specifics** (host names, IPs, SSIDs, MACs, bot identifiers).
The operator keeps those in a private note off this repository — see
[Secrets inventory](#secrets-inventory) for what that note should hold.

---

## 1. What runs where

- **Production is a single Docker container on a home NAS** (unRAID in the
  reference deployment). All state — the SQLite database and its backup
  snapshots — lives in one volume mounted at `/data`. The running service
  does **not** depend on any developer machine.
- The poller (every `POLL_INTERVAL_SECONDS`), the nightly sessioniser
  (01:15 local), and the nightly backup (02:00 local) are in-process
  APScheduler jobs — there is nothing else to schedule.
- Developer machines are for **code and management only**. The test suite is
  hermetic (no network, no real secrets), so a fresh checkout runs `pytest`
  green with nothing configured.

## 2. Managing the running service

From a machine with SSH access to the NAS (set up an alias such as `unraid`
in `~/.ssh/config`):

- **One-shot health:** `make nas-status` (container status + healthcheck
  badge + `/api/health` + recent poller lines).
- **Health, three ways:** `GET /api/health` (machine-readable), the
  `/system` web page (human), or `/status` to the Telegram bot.
- **Logs:** `ssh <nas> "docker logs wfh-logbook --tail 80"`.
- **Restart:** `ssh <nas> "docker restart wfh-logbook"`.
- The container runs `--restart always`. If the NAS reboots **and the array
  is set to start manually**, start the array in the NAS UI; the container
  then returns on its own. A short polling gap after a reboot is normal and
  self-healing — the sessioniser back-fills the trailing 7 days and any
  in-session gap surfaces in the review queue.

## 3. Deploying a code update to the NAS

Two paths. The registry path is preferred once the GitHub Actions workflow
(`.github/workflows/docker.yml`) has published an image to GHCR on push/tag.

**Registry path:**
```
ssh <nas> "docker pull ghcr.io/<owner>/wfh-logbook:latest"
# then recreate the container (see below)
```

**Hand-carry path (no registry needed):**
```
# 1. Validate
make test lint typecheck
# 2. Build — --format docker PRESERVES the HEALTHCHECK (podman's default
#    OCI format drops it; Docker/unRAID keep it)
podman build --format docker -t wfh-logbook:latest .
# 3. Ship + load
podman save -o /tmp/img.tar wfh-logbook:latest
scp /tmp/img.tar <nas>:/tmp/ && ssh <nas> "docker load -i /tmp/img.tar"
# 4. Recreate (see below)
```

**Recreate** (env is baked in at `docker run`; a plain `restart` will NOT
re-read `.env`):
```
ssh <nas> "docker rm -f wfh-logbook && docker run -d --name wfh-logbook \
  --restart always --user 99:100 -p 8088:8088 \
  -v /mnt/user/appdata/wfh-logbook:/data \
  --env-file /mnt/user/appdata/wfh-logbook/.env localhost/wfh-logbook:latest"
```

> ⚠ **`--env-file` is not dotenv.** Docker's `--env-file` does not strip
> inline `# comments`, surrounding quotes, or whitespace after `=`. A file
> that works with local `uvicorn` can crash the container or silently send a
> space-prefixed password to the controller (HTTP 403). Keep the NAS `.env`
> as bare `KEY=value` lines. Cleaning sed and full detail: DEPLOYMENT.md §1.

Migrations run automatically at container start (`alembic upgrade head`),
so a new image with a new migration upgrades the live DB on boot. Take a
snapshot first (`/system` → Back up now) if you want a rollback point.

## 4. Backups

- **On the NAS:** nightly `VACUUM INTO` snapshot (02:00 local) to
  `/data/backups/`, retained 30 daily + 12 monthly. On demand via the
  `/system` page or `POST /api/backup`.
- **Off-box (the operator's responsibility):** pull snapshots somewhere off
  the NAS regularly — quarterly at minimum, the ATO retention is five years.
  The reference setup uses a scheduled task on a desktop that `scp`-pulls the
  snapshot files into personal cloud storage, append-only (cloud copies are
  never deleted). The exact task lives in the operator's private note.
- **Restore** is the tested procedure in DEPLOYMENT.md §5 (exercised by
  `tests/test_backups_api.py::TestRestoreProcedure`).

## 5. Setting up a NEW developer machine

1. Install **git, Python 3.12+, uv**, a container engine (Podman in
   Docker-compat mode, or Docker Desktop), and VS Code + the Dev Containers
   extension if you want the devcontainer.
2. Clone and enter:
   ```
   git clone https://github.com/cauldnz/wfh-logbook
   cd wfh-logbook
   ```
3. Environment + deps:
   ```
   uv venv --python 3.12 .venv
   uv pip install -e ".[dev]"
   ```
4. **Verify with no secrets** — the suite is hermetic:
   ```
   .venv/Scripts/python -m pytest          # (Windows path shown)
   .venv/Scripts/python -m ruff check .
   .venv/Scripts/python -m mypy app
   ```
5. **SSH to the NAS** for management — see [Secrets inventory](#secrets-inventory).
6. **`.env`** is only needed to run the app or the `tools/` probes locally
   (tests don't need it). Once SSH works, pull the authoritative copy from
   the NAS:
   ```
   scp <nas>:/mnt/user/appdata/wfh-logbook/.env .env
   ```
   Never commit it (`.env` is gitignored). In a dev `.env`, point `DATA_DIR`
   at a **throwaway local path** — the NAS is the single writer for the real
   logbook; do not run a second writer against production data.

> **Container engine note (reference setup):** the original developer uses
> Podman in Docker-compat mode on Windows. `docker` may resolve to Podman's
> shim; `podman compose` is the host equivalent of `docker compose`. In VS
> Code set `"dev.containers.dockerPath": "podman"` in *user* settings.
> The devcontainer is inner-loop only (edit/pytest/mypy/ruff/uvicorn); the
> full compose stack runs from a host terminal, not nested in the container.

## 6. Secrets inventory

The repository contains **none** of these — `.env` and `*.raw.json` are
gitignored and all committed fixtures are sanitised. Keep a private note
(off-repo, in trusted storage) recording the specifics, and move secrets to
a new machine as below.

| Secret | Lives in | Move to a new machine by |
|---|---|---|
| **SSH key** for NAS management | `~/.ssh/<keyfile>` (+ `.pub`, + a `Host` alias in `~/.ssh/config`) | **Preferred:** generate a *fresh* key on the new machine and add its public key to the NAS (unRAID: Users → root → SSH authorized keys). **Alternative:** copy the private+public key files via trusted storage — never through chat, never onto the NAS itself. |
| **UniFi** controller user + password | `.env` (`UNIFI_USERNAME`, `UNIFI_PASSWORD`); authoritative copy on the NAS at `/mnt/user/appdata/wfh-logbook/.env` | `scp` the `.env` from the NAS once SSH works. |
| **Telegram** bot token | `.env` (`TELEGRAM_BOT_TOKEN`); authoritative on the NAS | same `.env` `scp`. |
| Telegram allowlist id, work SSID, device MACs | `.env` (not credentials, but home-network specifics) | same `.env` `scp`. |

Because the production secrets live on the NAS and a new machine can pull
them over SSH, the **only** secret that needs a manual bootstrap is SSH
access itself.

## 7. Quick reference

| Want | Do |
|---|---|
| See today's running total | Telegram `/today` (rebuilds first), or the web Review page |
| Review & lock a day | `/yesterday` → ✏ Adjust → `-45 lunch` → 🔒 Lock |
| Force a sessioniser run | `/rebuild [YYYY-MM-DD\|today\|yesterday]`, or `/day/{date}` → Build day |
| Days needing attention | web `/review-queue` or `GET /api/review-queue` |
| Year total + projections | web `/year/{fy}` |
| Year-end artefacts | `GET /api/export.xlsx?fy=YYYY-YY`, `GET /api/export.bundle?fy=...`, or `make export-xlsx` |
| Operational health | `make nas-status`, `GET /api/health`, web `/system` |
| Back up now / download snapshots | web `/system` |
