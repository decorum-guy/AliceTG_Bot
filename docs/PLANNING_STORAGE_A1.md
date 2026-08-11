# Planning storage foundation (A1)

Status: A1 implementation only. The storage package is not wired into bot
startup, reminder handlers, HTTP routes, Alice handling, Telegram UX or any
worker in this phase.

Reference: `docs/PLANNING_V1_CONTRACT.md` and the approved Sol architecture in
`decorum-guy/artem-control-center` PR #62 / issue #61.

## Database configuration

The explicit configuration variable is `PLANNING_DB_PATH`. Its safe persisted
default is:

```text
/app/data/planning.sqlite3
```

This is compatible with the documented Compose bind mount
`./data/telegram-bot:/app/data`. `PlanningDatabaseConfig.from_env()` also
recognises `PLANNING_ENV`, `APP_ENV` or `ENVIRONMENT`. When that environment is
`production`, `:memory:`, relative paths and known temporary locations such as
`/tmp`, `/private/tmp`, `/var/tmp`, `/private/var/folders` and `/dev/shm` are
rejected before a connection is opened.

The existing legacy reminder path remains authoritative:

```text
/app/data/reminders.json
```

A1 does not create a production database, migrate that JSON file, dual-write,
replace `ReminderStore`, or change current bot startup behavior.

Live production persistence of `/app/data` has not yet been independently
verified in this implementation session. This is an A1 production-preflight
STOP gate and must be proven before any Planning database production
activation/migration, especially before A2 legacy reminder migration/cutover.

## Storage boundary

`PlanningDatabase` configures each connection with WAL, foreign keys,
`busy_timeout`, an explicit row factory and autocommit mode. Mutations use the
`database.transaction()` context manager, which starts `BEGIN IMMEDIATE` and
uses savepoints for nested repository calls. Migration application uses the
same database-level locking, not only an in-process lock.

`PlanningRepository` creates server-owned UUIDv4 IDs, versions, timestamps,
object audit correlation IDs and source metadata derived from the trusted
`MutationContext`. It accepts domain fields rather than canonical client
objects. Existing-object changes use `WHERE id = ? AND version = ?` and raise a
typed version conflict when the guarded update does not match.

The repository's low-level idempotency methods require a surrounding
transaction. `execute_idempotent()` is the convenience primitive that claims
`(audience, Idempotency-Key)`, runs a trusted mutation, and stores the canonical
response atomically. The request hash is supplied only by trusted internal
code, never by a client request. A same-hash replay returns the stored JSON;
different hashes raise an idempotency conflict.

Audit rows are written by `AuditWriter` in the same transaction as each domain
create/update/tombstone. Sensitive keys are redacted and representations are
bounded before persistence. The outbox only provides durable enqueue/storage
operations; A1 contains no polling, leasing worker, retry execution or
transport send.

Provider mappings, cursors and conflicts are schema/repository foundations
only. No OAuth, provider network call, sync loop or provider write is included.

## A1 boundary

This phase adds SQLite schema/migrations, typed models, repositories,
optimistic concurrency, tombstones, transactional audit, idempotency storage
and outbox enqueue primitives. It does not add Planning HTTP endpoints,
scheduler workers, reminder import/cutover, Home Assistant changes, Alice
behavior, Telegram behavior, Control Center code, Samsung deployment or
production deployment.
