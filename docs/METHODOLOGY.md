# Working-From-Home Hours: Methodology

This document describes the method by which work-from-home hours have been recorded for taxation purposes. It is intended to be provided alongside any logbook produced by this system in the event of an ATO review or audit.

It is written as a template. Items in `[BRACKETS]` are to be confirmed by the taxpayer once the system is configured for their environment. The completed document should be saved alongside annual exports.

---

## 1. Taxpayer and scope

- **Taxpayer**: `[NAME, TFN ON REQUEST ONLY]`
- **Income year(s) covered by this methodology version**: `[e.g. 2025–26]`
- **Method claimed**: ATO revised fixed-rate method under PCG 2023/1 (currently 70 cents per work hour).
- **Record retention period**: five years from the date of lodgement of the relevant return, as required by the ATO.

This document describes only the method used to record *hours worked from home*. Substantiation of running expenses (one bill or invoice per expense category) is held separately.

## 2. Source of evidence

Hours are recorded from association events on a dedicated Wi-Fi SSID on the taxpayer's home Ubiquiti network. The SSID, named `[WORK_SSID]`, exists for the sole purpose of providing a contemporaneous "I am working" signal: it is not used for any other purpose, and the taxpayer's work devices are configured *not* to join it automatically.

The act of connecting a work device to `[WORK_SSID]` is the deliberate act of clocking on. The act of disconnecting (manually, or by leaving the premises and falling off Wi-Fi) is the act of clocking off.

The following devices are configured as work devices for this purpose:

| Device | MAC address (per-SSID) | Notes |
|---|---|---|
| `[Windows PC]` | `[xx:xx:xx:xx:xx:xx]` | Employer-managed device. |
| `[iPhone]` | `[xx:xx:xx:xx:xx:xx]` | Employer-managed device with Intune. iOS per-SSID private MAC; this MAC is stable for `[WORK_SSID]`. |

Other devices in the household (including the same physical devices when connected to other SSIDs) are not tracked.

## 3. Contemporaneity

Connection and disconnection events are captured at the time they occur by an automated poller that runs every 60 seconds against the local UniFi controller and writes each event to an append-only database table. No event is recorded later than approximately 60 seconds after it physically occurred. No retroactive insertion of events into the raw evidence is performed.

The raw evidence table is append-only by design: there is no application code path, and no user-facing function, that modifies or deletes a row once written. The complete history of raw events is retained for the full record-retention period.

## 4. Derivation of hours

Raw events are converted into daily hours by the following rules. These rules constitute the *method* by which a contemporaneously-captured event stream becomes a claim of hours.

### 4.1 A work session

A work session is a continuous period during which at least one configured work device is associated with `[WORK_SSID]`. If the taxpayer steps away momentarily and only one device disconnects (e.g. closing the laptop while the phone remains on the SSID), the session continues. If all devices disconnect, the session ends.

### 4.2 Gap-bridging

If two would-be-separate sessions are separated by a period of no association no longer than `[GAP_BRIDGE_MINUTES, default 10]` minutes, they are treated as a single session. This handles brief Wi-Fi disconnections (signal blip, walking between rooms, momentary roaming) without artificially fragmenting a continuous work period.

### 4.3 Minimum session length

Any computed session shorter than `[MIN_SESSION_MINUTES, default 2]` minutes is discarded. This filters out incidental momentary associations (e.g. walking through the house with the phone). These periods are not work and are not claimed.

### 4.4 Midnight-crossing sessions

A session that begins on one calendar date and ends on the next is attributed in full to the calendar date on which it began. This is a deterministic rule, applied identically every time.

### 4.5 Daily anomaly review

A computed daily total exceeding `[DAILY_CAP_HOURS, default 12]` hours is flagged for the taxpayer's attention but is not automatically truncated. The taxpayer reviews the underlying sessions and, if appropriate, applies a manual adjustment per §4.6.

### 4.6 Manual adjustments

In addition to the automatic rules above, the taxpayer reviews each day's computed total within approximately 24 hours and may apply a single signed adjustment (positive or negative) accompanied by a written reason. Typical reasons include:

- "Left desk for personal lunch 12:30–13:15, device remained on work SSID. Deducted 45 minutes."
- "Brief disconnection 14:00–14:08 covered by gap-bridge; no adjustment required."
- "Poller outage 09:00–11:00; corroborated by Microsoft Teams sign-in logs showing continuous activity. Added 2 hours."

Every adjustment is recorded as a new versioned row alongside the original computed value. The original computed value is never overwritten. Once a day is finalised it is locked; if a later correction is required, locking creates a new version with a new timestamp, preserving the previously-locked state for audit.

### 4.7 Rule version

The rules above are stamped on every computed session and summary by a `rule_version` identifier. If any rule changes (for instance, if the gap-bridge threshold is changed from 10 to 15 minutes), the `rule_version` is incremented and the change is recorded in Appendix A of this document. Records computed under different rule versions are not silently reprocessed.

## 5. What this method *does not* claim to measure

This method does not measure keystrokes, application usage, productive output, or any device-side signal. It measures the presence of work devices on a dedicated work-only Wi-Fi network within the home, interpreted by the rules in §4.

The taxpayer asserts, by manually associating to `[WORK_SSID]` and by reviewing each day's record, that the resulting hours are a reasonable representation of time spent performing duties of employment from home. The method is a tool to make that record contemporaneous, consistent, and reviewable. It is not a substitute for the taxpayer's own honest judgement.

## 6. Retention and reproducibility

- **Raw evidence**: retained for the full retention period in an append-only database. A copy of the database, including all raw observations, is available on request.
- **Backups**: a nightly snapshot of the database is taken locally and copied off-device on at least a quarterly basis.
- **Reproducibility**: given the same raw evidence and the same `rule_version`, sessionisation produces the same daily totals on every run. The sessioniser is deterministic.
- **Exports**: the annual XLSX export, produced from the database at end of financial year, is the document the taxpayer relies on. It includes per-day computed hours, adjustments, reasons, and the version that was locked.

## 7. Disclaimer

This methodology describes how a record is *kept*. It does not constitute tax advice and does not make any claim about deductibility, the appropriateness of the fixed-rate method for the taxpayer's circumstances, or the correct value to claim. Those determinations are made by the taxpayer in conjunction with a registered tax agent.

---

## Appendix A — Rule version history

| Rule version | Effective from | Change |
|---|---|---|
| `2026.1` | `[YYYY-MM-DD]` | Initial rules: gap-bridge 10 min, min-session 2 min, daily-cap 12 h. Midnight-crossing attributed to start date. |

## Appendix B — Configuration snapshot at time of locking

This appendix is reproduced at year-end alongside the annual export.

- Work SSID: `[WORK_SSID]`
- Gap-bridge: `[GAP_BRIDGE_MINUTES]` minutes
- Minimum session: `[MIN_SESSION_MINUTES]` minutes
- Daily cap (review flag): `[DAILY_CAP_HOURS]` hours
- Local timezone: `Australia/Sydney`
- Devices tracked: `[as listed in §2]`
- Rule version in effect at end of year: `[e.g. 2026.1]`
