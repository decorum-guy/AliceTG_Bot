# Planning reminder cutover (A2)

This document describes the A2 legacy reminder import and the guarded
repository cutover. It does not enable production cutover and does not
describe the A3 scheduler/outbox worker.

## Data boundary

`reminders.json` is an import source for reminder records only. The existing
`settings` object remains authoritative for notification and voice settings;
it is not copied into a new Planning table. `ReminderSettingsStore` is the
settings-only compatibility boundary used after cutover. The import itself
does not write or reformat the source JSON.

Planning reminder rows use `timezone=UTC` because the legacy schema carries an
aware timestamp or offset but no IANA timezone. The original timestamp strings
and normalized UTC values are retained in `legacy_reminder_mappings`. A
timezone-naive timestamp is a preflight blocker; no Europe/Moscow assumption is
made.

## Approved legacy mapping

| Legacy record | Planning status | Planning delivery state | Preserved metadata |
| --- | --- | --- | --- |
| `pending` | `pending` | `not_due` | original status/source/timestamps |
| `cancelled` | `cancelled` | `not_due` | original status/source/cancelled timestamp; Planning tombstone |
| `fired` | `completed` | `delivered` | `completed_at` from normalized `fired_at`, original `fired_at`, marker `legacy_delivery_inferred` |

The fired mapping is historical provenance only. Newly created Planning
reminders remain active after delivery: the adapter changes only
`delivery_state` to `delivered` and never changes object status to `completed`.

## Read-only preflight

```bash
python -m app.planning.legacy_import preflight \
  --source /app/data/reminders.json
```

The bounded JSON report contains the source SHA-256, record/status/source
counts, valid/invalid counts, duplicate-ID count, timestamp problem categories,
semantic import counts, expected Planning rows, mapping count, semantic hash,
warnings and blockers. It never prints reminder bodies, chat IDs or Telegram
IDs. A non-zero exit means the source is blocked.

Preflight and import use the same parser and validation path. Unknown record
fields, malformed records, unknown statuses/sources, duplicate legacy IDs,
inconsistent terminal timestamps and timezone-naive timestamps block import.
Semantically duplicate records with different legacy IDs are retained and
reported as a warning because the IDs distinguish them.

## Import and marker

The runtime import is separate from migration 002:

```bash
python -m app.planning.legacy_import import \
  --source /app/data/reminders.json \
  --db /app/data/planning.sqlite3
```

The importer validates every row before `BEGIN IMMEDIATE`, then writes the
Planning row, mapping/provenance row, bounded `legacy_import` audit event and
completed import marker in one transaction. The mapping table preserves the
legacy ID, Planning UUIDv4, original status/source, original fired/cancelled
timestamp strings and normalized values, chat target and delay required by the
current compatibility workflow. The reminder body remains only in the
canonical reminder row; it is not copied into migration metadata or audit
payloads.

The marker is keyed by `002_import_legacy_reminders` and stores the raw source
hash, imported/mapping counts, semantic hash and bounded report metadata.

- Same source hash after a completed import: deterministic no-op.
- Different source hash on a second import: explicit changed-source error.
- Failed or interrupted transaction: no completed marker and no partial rows.
- A cutover gate may tolerate formatting/settings-only source-byte changes only
  when the normalized reminder semantic hash is unchanged; a reminder-data
  change remains a hard operator-review condition.

## Explicit cutover gate

The default is disabled:

```text
PLANNING_REMINDER_CUTOVER_ENABLED=false
```

Before it is enabled, `ReminderStore` remains authoritative. When enabled,
startup opens `PLANNING_DB_PATH`, requires migration 002 and a matching
completed import marker, and then constructs `PlanningReminderStoreAdapter`.
The adapter is the only reminder-record persistence path; it does not dual-write
records to JSON. The existing `asyncio.sleep` scheduling behavior remains in
place until A3. Settings continue through the legacy settings-only boundary.

No production configuration was changed by A2.

## Backup and rollback

Create a backup before a future rollout, using a destination outside the live
data directory:

```bash
python scripts/backup_reminder_cutover.py \
  --source-dir /app/data \
  --destination-root /var/backups/alice-tg
```

The tool creates a timestamped `alice-reminder-cutover-YYYYMMDDTHHMMSSZ`
directory, copies `reminders.json` and `state.json` when present, uses SQLite's
online backup API for `planning.sqlite3` when present, writes a content-free
manifest with sizes/hashes/integrity, and never deletes old backups.

Rollback is asymmetric:

1. Disable the Planning cutover gate and stop the new process.
2. Before returning to a JSON-only image, export/reconcile any reminders
   created or changed only in Planning after cutover.
3. Preserve the SQLite database and the untouched legacy JSON; do not run a
   destructive down-migration.
4. Start the prior image only after the post-cutover Planning records are
   preserved and the rollback window is documented.

Blindly starting an old JSON-only image after new SQLite-only reminders have
been created can lose those newer reminders.

## Rollout status

Live production preflight is currently `LIVE_PRODUCTION_PREFLIGHT_PENDING`.
The configured SSH endpoint closed during key exchange before a remote shell or
Docker inspection could run. A2 local implementation and synthetic dry-runs
are permitted, but real production backup/import/cutover remains blocked until
the operator verifies the live container, persistent `/app/data` mount,
`reminders.json`, disk space and structural counts.
