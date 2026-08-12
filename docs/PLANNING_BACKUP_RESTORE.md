# Planning A8 backup and restore operator runbook

This is a verification and disaster-recovery runbook for the durable Planning
SQLite foundation. It is independent of Samsung, Control Center, Panel Agent,
Telegram transport, Home Assistant, and external providers.

## Backup

Provision a persisted, private directory and a dedicated high-entropy key:

```text
PLANNING_ENV=production
PLANNING_BACKUP_ENABLED=true
PLANNING_BACKUP_DIR=/app/data/backups/planning
PLANNING_BACKUP_ENCRYPTION_KEY=<64 hexadecimal characters from the secret manager>
PLANNING_BACKUP_RETENTION_COUNT=14
PLANNING_BACKUP_INTERVAL_SECONDS=86400
```

The directory is created with `0700`; package and sidecar files use `0600`.
The key is not the Telegram, HA, panel-agent, operator, or Alice secret and
must never be committed or printed.

Run:

```text
python -m app.planning.backup backup
```

The command uses SQLite's native online backup API, so normal Planning reads
and writes continue. It writes a standalone encrypted AES-256-GCM package
atomically. It does not copy a live database file, package a source WAL, pause
the bot, or include application/private ephemeral files.

List and inspect bounded state:

```text
python -m app.planning.backup list
python -m app.planning.backup status
```

Retention deletes only recognized A8 package names in the configured directory;
it never deletes unrelated files and never deletes the newly finalized valid
package.

## Verification

Verify by recognized filename, not an arbitrary filesystem path:

```text
python -m app.planning.backup verify planning-<timestamp>-schema4-<id>.sqlite3.a8
```

Verification decrypts into a temporary isolated directory, checks the
authenticated package, exact members, manifest SHA/size, schema compatibility,
aggregate counts, SQLite `integrity_check`, `foreign_key_check`, and bounded
Planning semantic invariants. Older supported schemas may be migrated only in
that temporary copy. A newer schema fails closed. The encrypted source package
and live database are not changed.

The verifier uses no Telegram or Home Assistant transport. It invalidates
unconsumed action capabilities only in the temporary restored copy and reports
an aggregate count. Refresh Telegram menus after any approved recovery; old
callback capabilities are not a recovery dependency.

## Disaster recovery boundary

A8 deliberately has no restore-over-live command or API. No browser, HTTP
caller, or Telegram callback can replace the live Planning database.

For an actual incident, an authorized operator must follow the deployment's
separately reviewed offline procedure:

1. Stop the bot and preserve the current database and logs according to the
   incident policy.
2. Copy the encrypted package to an isolated restorer with the same dedicated
   key; do not decrypt it into a shared or world-readable location.
3. Run `verify` and retain the bounded result, including schema, integrity, FK,
   count, and resumable-due-job checks.
4. Obtain explicit approval for any file replacement. The A8 verifier itself
   never performs it and never supplies generic SQL or a shell passthrough.
5. Restore only through the reviewed platform procedure while the bot is
   stopped, preserve the pre-restore database, then start the bot and verify
   Planning health.
6. Refresh Telegram Planning menus and treat all old action callbacks as
   stale; do not rely on pre-restore capabilities.

If verification fails, do not repair or rewrite the package silently. Preserve
the failed artifact for incident analysis and choose another recognized valid
backup.

## Privacy boundary

The package contains the durable Planning SQLite snapshot because it is needed
for recovery, but its outer package is encrypted. The manifest is operational
metadata only: versions, timestamp, digest, size, aggregate counts, integrity,
method, encryption mode, and bounded application metadata. It does not contain
titles, notes, locations, Telegram IDs, receipts, audit payloads, raw tokens,
audio, Whisper data, logs, environment files, or secrets. Filenames contain no
personal content.

## Rollback

Code/config rollback is independent from backup retention: restore the prior
bot image/config, restart only the bot, and keep the database, dedicated key,
and valid encrypted packages. Do not delete backups as part of a code rollback
and do not automatically restore an older database.
