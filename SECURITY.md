# Security

This is a single-user, LAN-bound service. When the optional Telegram bot is
enabled, it additionally exposes a single public webhook endpoint via
Cloudflare Tunnel. The threat model is laid out in
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) §8; this document is the
concise pointer to it and the report-an-issue path.

## What the service holds

- **Presence data**: timestamps of when work devices joined/left a dedicated
  Wi-Fi SSID. No keystrokes, no app usage, no content of any communication.
- **Hours logbook**: computed daily hours derived from the presence data,
  with user-applied adjustments and reasons.
- **Configuration**: UniFi controller hostname + credentials, optionally a
  Telegram bot token and a Cloudflare Tunnel token.

There is no employer data, no payroll data, no PII beyond the device MACs
and any text the user types into adjustment-reason fields or Telegram
messages.

## Threats considered

Summarised from ARCHITECTURE §8:

1. **Credential leakage.** Credentials in `.env`, never logged or echoed.
   Telegram tokens and Cloudflare tunnel tokens are rotatable from the
   respective dashboards.
2. **Casual LAN access.** The web UI has no authentication. Users who don't
   trust their LAN should bind to `127.0.0.1` and access via SSH tunnel.
3. **Backup data exfiltration.** Backups are plain SQLite files; the user
   is responsible for off-box encryption (e.g. `age`).
4. **Hostile container compromise.** Container runs as a non-root user,
   writes only to `/data`, makes only outbound HTTPS to the UniFi
   controller, Cloudflare's edge (if the tunnel is enabled), and
   `api.telegram.org` (if the bot is enabled).
5. **Unauthenticated webhook hits.** The `/webhook/telegram/{secret}` path
   is reachable from the public internet when the tunnel is enabled. Two
   independent secret checks (path segment + Telegram's
   `X-Telegram-Bot-Api-Secret-Token` header) plus a per-user allowlist on
   the inbound `from.id`.
6. **Hostile Telegram users.** Allowlist of Telegram user IDs in
   `TELEGRAM_ALLOWED_USER_IDS`. Unauthorised users receive exactly one
   polite rejection and are then silently dropped.
7. **Stolen Cloudflare tunnel.** A compromised tunnel token allows
   redirecting the public hostname. The Telegram secret-token check
   still prevents a redirected endpoint forging valid payloads without
   also possessing the bot's secret.

## Out of scope

- Defence against a determined attacker already on the LAN.
- Multi-user authentication.
- Encryption at rest inside the application database (the host filesystem
  is the trust boundary).

## Reporting an issue

Open a private GitHub issue with the label `security`, or email the
repository maintainer directly. Please include:

- A clear description of the issue.
- Steps to reproduce, if applicable.
- Impact assessment (what could go wrong, in concrete terms).

We commit to acknowledging within 7 days and to publishing a fix or a
deliberate "won't fix" rationale within 30 days.

## Supported versions

This project follows a simple "latest release supported" model — there is
no LTS branch. Security fixes are applied to `main` and tagged in the
next release. If you are running an older release, upgrade.
