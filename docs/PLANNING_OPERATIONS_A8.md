# Planning A8 — backup, observability, and the independent Plan 1 gate

Status: A8 implementation. This document describes the final Plan 1 / AliceTG_Bot
foundation phase. It does not add providers, Control Center integration, or a
new scheduler.

## Scope and operating principle

Planning is recoverable and observable from the bot backend alone. The A8
services do not require Samsung, Control Center, Panel Agent, TickTick, an
external calendar, Telegram network access, or Home Assistant transport to
create or verify a backup.

The boundaries are:

```text
operator CLI / existing A4 status
             ↓
PlanningBackupService · PlanningBackupVerifier · PlanningHealthService
             ↓                         ↓
SQLite online snapshot       isolated temporary restore database
```

`PlanningBackupService` owns backup and retention. `PlanningHealthService`
only observes the existing `DurableReminderScheduler`; it never creates a
second worker. Neither service imports Telegram, Home Assistant, HTTP route,
Control Center, or presentation code.

## Configuration

The settings are disabled by default:

```text
PLANNING_ENV=development
PLANNING_BACKUP_ENABLED=false
PLANNING_BACKUP_DIR=/app/data/backups/planning
PLANNING_BACKUP_ENCRYPTION_KEY=
PLANNING_BACKUP_RETENTION_COUNT=14
PLANNING_BACKUP_INTERVAL_SECONDS=86400
```

For production, `PLANNING_BACKUP_DIR` must be an absolute persisted path and
must not be `/tmp`, `/dev/shm`, or an equivalent ephemeral runtime directory.
The configured directory is created with mode `0700`; recognized package and
sidecar files are restricted to `0600` where the filesystem supports it.

The backup key is a dedicated operational secret. It is supplied as exactly
64 hexadecimal characters (`32` bytes) through
`PLANNING_BACKUP_ENCRYPTION_KEY`. It must not equal the Telegram bot token,
`INTERNAL_WEBHOOK_SECRET`, `PLANNING_HA_SECRET`, panel-agent secret, operator
secret, or Alice idempotency secret. The key is never written to a manifest,
status response, filename, or log. A missing, malformed, or unavailable
cryptography runtime makes the backup subsystem fail closed; the bot does not
crash unrelated coffee workflows merely because backup is unavailable. A
production ephemeral path is a startup configuration error.

No automatic backup worker is added to the durable reminder scheduler. The
repeatable production procedure is an operator/Compose/host scheduler invoking
the narrow CLI at the configured interval. `86400` seconds is the documented
default target; it is an interval, not a user-selected time of day.

## Online backup format

The source is copied with Python's `sqlite3.Connection.backup()` API. For the
persisted file database, A8 opens a dedicated bounded-timeout source
connection to the configured Planning database path and closes it after the
snapshot. The live `PlanningDatabase` mutation lock is not held during the
copy, so normal repository writes can commit while the native snapshot is in
progress. SQLite WAL coordinates the source snapshot with those writes. The
`:memory:` test fallback remains serialized because a second connection cannot
see the same in-memory database.

A8 never copies the live database file with a filesystem `copy` and never
pauses the bot for a normal backup. The target is a standalone SQLite
snapshot, so it does not depend on the source WAL file and no source WAL file
is packaged or checkpointed for finalization. The manifest records this
explicit WAL policy.

Each package is named like:

```text
planning-20260812T012345Z-schema5-<12-hex-random-chars>.sqlite3.a8
```

The name contains only UTC creation time, schema version, and a collision-safe
random component. It contains no title, user ID, chat ID, or secret.

The encrypted package is an AES-256-GCM stream with a fixed A8 header, version,
nonce, ciphertext, and authentication tag. The authenticated plaintext ZIP
contains exactly:

```text
manifest.json
planning.sqlite3
```

The manifest is canonical, sorted JSON and contains only bounded operational
metadata:

- manifest and package format versions;
- UTC creation time and source schema version;
- database SHA-256 and byte size;
- aggregate counts for the allowlisted Planning tables;
- application version/commit when configured;
- integrity and foreign-key results;
- SQLite online-backup method/version and WAL policy;
- encryption mode metadata;
- the restore capability policy.

It never contains titles, notes, locations, Telegram IDs, provider receipts,
audit before/after blobs, action tokens, paths, environment files, audio,
Whisper data, logs, or any secret. The SQLite snapshot naturally contains the
durable Planning tables, including the token table because it is part of the
schema; A8 does not export raw action tokens separately.

Finalization writes temporary artifacts in the configured directory, encrypts
and authenticates the complete package, applies restrictive permissions, fsyncs
where available, and atomically renames the completed artifact. A previous
valid artifact is never removed on a failed backup. Retention recognizes only
the strict A8 filename pattern, always keeps the newly created valid package,
and keeps at most `PLANNING_BACKUP_RETENTION_COUNT` recognized packages.
Unrelated files are not deleted.

## Restore verifier

`PlanningBackupVerifier` is an isolated verifier, not a live restore command.
It accepts only a recognized package in the configured backup directory and
uses a temporary directory for every decrypted/intermediate file:

```text
recognized encrypted package
  → decrypt/authenticate into temporary directory
  → require exactly manifest.json + planning.sqlite3
  → verify manifest, SHA-256, and byte size
  → reject a schema newer than this binary
  → open the extracted SQLite copy
  → migrate only an older supported schema in that isolated copy
  → integrity_check + foreign_key_check
  → aggregate counts and semantic Planning checks
  → invalidate unconsumed Telegram capabilities in the isolated copy
  → count resumable due jobs
```

It never replaces the live database and never modifies the encrypted package.
It does not connect to Telegram, Home Assistant, or any provider. Corrupt
ciphertext/tag, wrong key, malformed ZIP, modified manifest/hash, truncated or
corrupt SQLite, foreign-key violations, unknown future schema, impossible
reminder state, orphaned reminder outbox, or unsafe path fails closed with a
bounded category/code. Error reports identify a domain/table/category and do
not include row content.

Old action rows are not intended to remain usable after a restore. The
verifier marks restored unconsumed action capabilities consumed in its
temporary copy and reports the aggregate count. Operators must refresh
Telegram menus after any approved disaster recovery; A8 does not promise that
pre-restore callbacks remain usable.

The verifier checks the durable reminder/outbox relationship only for active
`pending`/`due` reminders whose undelivered state still expects delivery;
historical `completed`/`cancelled`/tombstoned reminders do not need a live job.
It also checks duplicate logical outbox keys and a due-job count. The deterministic recovery proof additionally
opens a restored copy with a fake Telegram transport and injected clock,
delivers one due job, reopens the database, and proves a second worker run does
not send it again.

## CLI / operator boundary

The only supported local operator interface is:

```text
python -m app.planning.backup backup
python -m app.planning.backup verify <recognized-package-name>
python -m app.planning.backup list
python -m app.planning.backup status
```

The command reads the configured database, backup directory, environment, and
dedicated key from environment. `verify` accepts a filename or path only when
it resolves beneath the configured backup directory and matches the strict A8
package name. There is no arbitrary-path reader, generic SQL, shell passthrough,
or restore-over-live-database command.

## Health and readiness semantics

The content-free health block is added to the existing audience-authenticated
`GET /internal/planning/v1/status` response as `planningHealth`. The existing
A4 envelope, capabilities, capability metadata, freshness fields, and
correlation ID remain present. Health includes bounded operational facts only:

```text
planningSchemaVersion
dbAvailable
dbIntegrityStatus
durableSchedulerEnabled
schedulerHeartbeatAt
schedulerHeartbeatAgeSeconds
schedulerHealth
oldestQueuedOrLeasedOutboxAgeSeconds
queuedOutboxCount
leasedOutboxCount
retryingReminderCount
terminalFailedReminderCount
activeDueReminderCount
eligibleQueuedOrLeasedOutboxCount
backupStatus
lastSuccessfulBackupAt
lastSuccessfulRestoreVerificationAt
lastBackupAgeSeconds
lastRestoreVerificationStatus
providerStatus
providerLastSyncAt
capabilityMetadata
applicationVersion
applicationCommit
incidents
```

No titles, notes, locations, object arrays, callback tokens, receipts, raw SQL,
filesystem backup path, secret, or full error is exposed.

The scheduler heartbeat is process-local and is updated at the start and end of
each real `DurableReminderScheduler.run_forever()` iteration. It is not updated
by direct `run_once()` calls and it is not a second scheduler. The threshold is
explicitly derived from the current cadence:

```text
max(15 seconds, 3 × poll interval + jitter bound + 5 seconds)
```

After process restart the heartbeat is `unknown` until the first loop
iteration. A configured-off scheduler is reported as `disabled`, not failed.
When enabled, a fresh successful loop is `healthy`; an old heartbeat or failed
iteration is `degraded`.

`oldestQueuedOrLeasedOutboxAgeSeconds` and its eligible count include only
queued/leased rows whose availability is due and whose linked reminder is
active and not delivered/terminal. A `due + delivered` reminder waiting for
user completion is not a delivery-stuck incident.

The typed incident codes are:

```text
planning.scheduler_heartbeat_stale
planning.outbox_stuck
planning.delivery_terminal_failure
planning.database_integrity_failure
planning.backup_failed
planning.backup_overdue
planning.restore_verification_failed
```

The scheduler-stale incident is inactive while heartbeat is merely unknown or
the feature is intentionally disabled. Stuck outbox is raised only after an
eligible queued/leased item is at least 30 seconds old. Terminal failure is an
aggregate count of active reminders with terminal delivery failure. Backup
health distinguishes disabled, unavailable, failed, unknown, overdue, and
fresh. Restore health reflects the last verifier result.

Incident logs are structured and transition-suppressed. A transition contains
only the incident code, active state, aggregate count, bounded age, and an
operation/correlation ID. Unchanged incidents are not logged on every health
poll. No log line contains content, IDs, receipts, tokens, secrets, or rows.

Health is a readiness/degradation signal, not process liveness. An overdue or
failed backup, absent provider, stale/unknown heartbeat, or unavailable
Planning database does not crash the entire bot. Unsafe configured invariants
such as an ephemeral production backup directory fail configuration closed;
an invalid backup key makes only the backup subsystem unavailable until fixed.

## Production rollout and rollback after review/merge

This branch must not be deployed. After architecture review and merge, use
this exact sequence:

1. Generate a dedicated 32-byte secret on the deployment host or secret
   manager, for example `openssl rand -hex 32`; do not paste it into source,
   shell history, logs, or a PR.
2. Provision a persisted directory mounted at
   `/app/data/backups/planning`, with ownership suitable for the bot and no
   world access. Keep the encryption secret separate from the bot/HA/Alice
   secrets.
3. Update the bot to merged A8 main and set
   `PLANNING_ENV=production`, `PLANNING_BACKUP_ENABLED=true`, the persisted
   `PLANNING_BACKUP_DIR`, the dedicated key, retention, and interval. Keep the
   already approved Planning flags unchanged.
4. Restart/recreate only the bot container. Do not change Home Assistant or
   Control Center.
5. Run `python -m app.planning.backup backup`, record only the package filename
   and bounded result, then run `python -m app.planning.backup verify
   <filename>` in the same configured directory.
6. Query the existing authenticated Planning status endpoint and confirm the
   backup is fresh, the scheduler becomes healthy after its first loop, the
   database is available/integrity-clean, and no unexpected incident is active.
7. Soak through at least one backup interval boundary or the deployment's
   approved shorter staging interval. Confirm retention remains bounded and
   no backup secret or personal content appears in logs.
8. If rollback is needed, revert the bot code/config and recreate only the bot
   with the prior known-good A6 configuration. Do not delete the Planning DB,
   backup directory, encryption key, or valid packages. Do not perform a live
   restore as part of rollback.

Actual disaster recovery requires an explicit operator-approved procedure:
stop the bot, preserve the current database, verify a package in an isolated
environment, and only then perform a separately reviewed file replacement.
The A8 CLI cannot trigger that replacement from HTTP, a browser, or Telegram.
Refresh Telegram menus after recovery.

## Deferred decisions and non-goals

No TickTick, calendar provider, provider sync, Home Assistant adapter, Alice
API enablement, Telegram task/event creation, snooze presets, automatic live
restore, generic admin shell, generic SQL, Control Center change, B0, or B1 is
included.

```text
SNOOZE_PRESET_PRODUCT_DECISION_PENDING
TELEGRAM_TASK_EVENT_CREATION_PRODUCT_DECISION_DEFERRED
```
