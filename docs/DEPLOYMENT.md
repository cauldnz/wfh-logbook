# Deployment

How to run WFH Logbook as a container — on an unRAID NAS (the maintainer's
target), on any generic Docker/Podman host, or via compose. Also: backups,
the tested restore procedure, and upgrading.

The container is self-contained: on start it applies database migrations,
seeds configuration from environment variables (first run only — thereafter
the database is authoritative, see `ARCHITECTURE.md` §7.5), and serves the
web UI and API on port 8088. All state lives in a single volume mounted at
`/data`.

---

## 1. Image

Pull from GHCR (published by CI on every release tag):

```
docker pull ghcr.io/OWNER/wfh-logbook:latest
```

Or build locally:

```
docker build -t wfh-logbook:latest .
# or:  podman build -t wfh-logbook:latest .
```

The image runs as a non-root user by default and supports arbitrary UIDs —
it writes only under `/data` and assumes nothing about `$HOME`.

## 2. unRAID

1. **Docker tab → Add Container**.
2. **Repository**: `ghcr.io/OWNER/wfh-logbook:latest`.
3. **Network type**: `bridge`. Add a port mapping: container `8088` → host
   port of your choice (e.g. `8088`).
4. Add a **path mapping**: container `/data` → host
   `/mnt/user/appdata/wfh-logbook`. Everything — database, backups — lives
   here, so your appdata backup strategy covers it automatically.
5. Add **variables** (the minimum set):

   | Variable | Example | Notes |
   |---|---|---|
   | `UNIFI_HOST` | `https://192.168.1.1` | your controller |
   | `UNIFI_USERNAME` | `wfh-readonly` | **local** account, read-only role |
   | `UNIFI_PASSWORD` | … | |
   | `WORK_SSID` | `MyWFH` | exact, case-sensitive |
   | `WORK_DEVICE_MACS` | `aa:bb:cc:dd:ee:ff=iPhone` | per-SSID private MAC |
   | `LOCAL_TIMEZONE` | `Australia/Sydney` | IANA name |
   | `LOG_FORMAT` | `json` | or `text` |

   Sessionisation knobs (`GAP_BRIDGE_MINUTES` etc.) only matter on the very
   first start; after that the database is the source of truth.

   > **`--env-file` gotchas (learned the hard way during a real migration):**
   > Docker's `--env-file` is far dumber than the python-dotenv parser used
   > in local development. It does **not** strip inline `# comments`, does
   > **not** strip quotes around values, and does **not** trim whitespace
   > after the `=`. A `.env` that works perfectly with `uvicorn` on a dev
   > box can crash the container (`ValidationError`) or — worse — silently
   > send a space-prefixed password to your controller (HTTP 403). Before
   > using a `.env` with Docker: one `KEY=value` per line, no inline
   > comments, no quotes, no stray spaces. Cleaning sed:
   > `sed -E 's/[[:space:]]+#.*$//; s/[[:space:]]+$//; s/^([A-Za-z_]+)=[[:space:]]+/\1=/'`
   > Also note: env values are baked in at `docker run` — editing the file
   > requires `docker rm -f` + re-`run`, not just `restart`.
6. **Extra Parameters**: `--user 99:100` (unRAID's `nobody:users`) so files
   in appdata get the ownership unRAID expects.
7. Apply. Browse to `http://<nas>:8088` — the review page should load and
   `http://<nas>:8088/api/health` should return `"status":"ok"`.

## 3. Generic Docker / Podman

```
docker run -d --name wfh-logbook \
  --restart unless-stopped \
  -p 8088:8088 \
  -v /srv/wfh-logbook:/data \
  --env-file .env \
  ghcr.io/OWNER/wfh-logbook:latest
```

(`podman run` is identical. `.env` follows `.env.example` in the repo root.)

Or with compose — the repo's `docker-compose.yml` runs the app service, and
adds the optional Cloudflare Tunnel sidecar only under
`--profile tunnel` (Telegram webhook mode, Phase 7):

```
docker compose up -d
```

## 4. Backups

Three layers, in increasing order of paranoia:

1. **Automatic**: nightly `VACUUM INTO` snapshot at 02:00 local into
   `/data/backups/`, retained 30 daily + 12 monthly (first snapshot of each
   month). Nothing to configure.
2. **On demand**: the **System** page (`/system`) has a *Back up now* button
   and download links for every snapshot; or
   `curl -X POST http://host:8088/api/backup`.
3. **Off-box** (your job — ARCHITECTURE §7.1): download a snapshot from the
   System page, or copy from the appdata share, at least quarterly. The ATO
   retention period is five years from lodgement; a NAS is not an off-box
   plan on its own.

## 5. Restore (tested procedure)

Snapshots are complete, self-contained SQLite databases. To restore:

1. **Stop the container** (`docker stop wfh-logbook`). Do not restore under
   a running app.
2. In the data volume, move the live database aside and put the snapshot in
   its place:

   ```
   cd /mnt/user/appdata/wfh-logbook        # or your volume path
   mv wfh-logbook.sqlite wfh-logbook.sqlite.broken
   rm -f wfh-logbook.sqlite-wal wfh-logbook.sqlite-shm   # stale WAL must not outlive the db
   cp backups/wfh-logbook-YYYYMMDD.sqlite wfh-logbook.sqlite
   ```

3. **Start the container.** Migrations run automatically (a snapshot from an
   older app version is upgraded on boot).
4. Verify: `/api/health` returns `"db_ok":true`, and spot-check a known day
   in the calendar.

Everything after the snapshot date is gone — that's what restoring means.
Observations the poller captured in the interim cannot be reconstructed;
note the gap in your next day's review per `METHODOLOGY.md` §4.6.

This procedure is exercised by an automated test
(`tests/test_backups_api.py::TestRestoreProcedure`) so it can't silently rot.

## 6. Upgrading

```
docker pull ghcr.io/OWNER/wfh-logbook:latest
docker stop wfh-logbook && docker rm wfh-logbook
# re-run with the same volume + env (unRAID: just hit "force update")
```

Migrations are append-only and applied automatically at startup. Take a
snapshot first (System page → Back up now) if you want a belt-and-braces
rollback point.

## 7. Ports / endpoints worth knowing

| Path | What |
|---|---|
| `/` | daily review |
| `/review-queue` | days needing attention |
| `/calendar`, `/year/{fy}` | history and yearly stats |
| `/system` | health, backups, snapshot downloads |
| `/api/health` | machine-readable health (use for unRAID monitoring) |
| `/api/export.xlsx?fy=…`, `/api/export.bundle?fy=…` | year-end artefacts |
