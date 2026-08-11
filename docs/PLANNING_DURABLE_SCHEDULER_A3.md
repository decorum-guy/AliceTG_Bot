# Planning durable scheduler and delivery outbox (A3)

This document records the A3 implementation boundary for durable Planning
reminder scheduling. It does not enable production cutover and does not add
Planning HTTP routes, Telegram task/event UX, Home Assistant automations,
provider sync, or health/backup features.

## Lifecycle and feature flags

The durable scheduler is disabled by default:

```text
PLANNING_DURABLE_SCHEDULER_ENABLED=false
PLANNING_REMINDER_CUTOVER_ENABLED=false
PLANNING_SCHEDULER_POLL_INTERVAL_SECONDS=5
PLANNING_SCHEDULER_LEASE_SECONDS=60
PLANNING_SCHEDULER_BATCH_SIZE=10
PLANNING_SCHEDULER_JITTER_SECONDS=5
```

The application validates the lifecycle before startup. Exactly one reminder
scheduler lifecycle must be selected. Durable mode requires the A2 Planning
cutover gate; when durable mode is off, the existing asyncio-based scheduler
remains available. The two schedulers cannot be attached at the same time.
The durable poll interval is constrained to the approved 5–10 second range.

## Scheduler, repository, and transport boundaries

`DurableReminderScheduler` owns due detection, reconciliation, transactional
job claims, lease recovery, retry timing, and the reminder state machine. The
Planning repository owns SQLite transactions, optimistic versions, logical
job deduplication, lease transitions, delivery-attempt records, and bounded
audits. Channel transports perform one provider attempt and return a typed
success/retryable/permanent result; they do not choose scheduler policy.

The Telegram adapter wraps the existing `TelegramMessages` bot/session path.
It does not create a second Telegram client. The optional Home Assistant
mobile adapter uses only the configured `ha_mobile_notify_services` allowlist;
A3 does not add arbitrary service or entity execution.

## Migration 003

A2 had durable outbox and delivery-attempt tables but lacked the metadata
needed to safely discover, claim, and correlate reminder delivery work.
Migration `003_delivery_policy.sql` is forward-only and adds:

| Field/index | Why A3 needs it |
| --- | --- |
| `outbox.dedupe_key` plus its partial unique index | One logical delivery job per reminder, including restart/multi-worker reconciliation. |
| `outbox.reminder_id` | Foreign-key-like lookup from generic outbox work to the canonical reminder state during atomic claims. |
| `outbox.lease_token` | Lease generation identity so a stale worker cannot commit after expiry/reclaim. |
| `outbox.attempt_window_started_at` | Durable start of the current 24-hour required-Telegram retry window; set only when the first Telegram attempt is persisted and reset by manual retry. |
| `outbox.last_error_code` | Bounded machine-readable retry/incident classification without storing provider response bodies. |
| `outbox.delivery_cycle_id` | Durable identity for the current required-Telegram retry window; it is not assigned by lease acquisition. |
| `delivery_attempts.correlation_id` and its index | Durable per-attempt correlation across the local audit and one provider call. |
| `delivery_attempts.delivery_cycle_id` and its index | Derives the required-Telegram retry ordinal from persisted attempts while preserving historical manual-retry rows. |
| `outbox(reminder_id,status,available_at)` index | Efficient due/retry/reconciliation scans for reminder work. |

No existing rows are deleted or rewritten by the migration.

## Atomic creation and reconciliation

The durable Planning creation path inserts the reminder, its audit event, and
its logical delivery outbox job in one database transaction. A rollback leaves
neither object. The A2 adapter uses that same transaction while creating its
legacy mapping. The outbox dedupe key is `planning.reminder:<reminder UUID>`.

Imported A2 pending reminders may predate an outbox job. Every scheduler
startup and polling iteration reconciles active reminders and creates missing
jobs idempotently. The unique dedupe index makes this safe across restarts and
workers. Delivered/failed terminal reminders are reconciled to terminal jobs,
and an active reminder with an inconsistent failed/succeeded job is repaired
without creating a second logical job.

## State machine and required-channel policy

Native Planning reminders do not become completed when delivered:

```text
future       pending / not_due
due          due / queued
retry        due / retrying
Telegram OK  due / delivered       (still active)
terminal     due / failed          (still active; final_failure_at set)
complete     completed             (future delivery suppressed)
cancel       cancelled             (future delivery suppressed)
```

Telegram is the required delivery channel. Optional iPhone/HA delivery is an
independent attempt: its success does not compensate for Telegram failure,
and its failure does not undo a successful Telegram delivery. Each optional
mobile cycle attempts every configured allowlisted HA notify service once;
the aggregate succeeds when at least one configured service succeeds. A
previous successful optional attempt is not repeated merely because Telegram
is retrying, including after a trusted manual retry of failed Telegram. A
Telegram
success permits `delivery_state=delivered`, but never changes `status` to
`completed`. The A2 legacy `fired -> completed/delivered` interpretation
remains limited to migrated historical records.

## Leases and startup recovery

Claims run in `BEGIN IMMEDIATE` and atomically select one due/retry job,
increment its lease-claim counter, and write owner, expiry, and a fresh lease
token. Lease claims do not consume the delivery budget or shift retry timing.
Only the owner/token pair can commit the attempt result. An expired lease is
reclaimed by the next transaction; startup reconciliation also makes expired
and overdue work recoverable immediately rather than waiting for a retry
interval. The canonical reminder status is rechecked immediately before each
provider call, so completion/cancellation around lease acquisition suppresses
delivery. A provider call already in flight cannot be recalled; its result is
ignored if the lease or canonical state is no longer valid.

## Attempts, retries, and manual retry foundation

An attempt row is inserted before a provider call. Attempt numbers are
sequential per reminder/channel and include start/finish timestamps, bounded
error code/message, safe receipt, correlation ID, and delivery-cycle ID. The
required Telegram retry ordinal and 24-hour window are derived from persisted
Telegram attempt rows in the active delivery cycle. A settings/runtime failure
or lease reclaim before the attempt row is created consumes no Telegram
attempt; a crash after the row is created conservatively counts it. Provider
diagnostics are redacted and bounded; titles and arbitrary response bodies are
not persisted.

Retryable Telegram failures use the approved sequence:

```text
30s, 2m, 10m, 30m, 2h, 6h, 12h
```

Jitter is bounded and injectable for deterministic tests. Delivery is capped
at eight attempts within 24 hours. A retry persists the failed attempt,
leaves the reminder due, sets `delivery_state=retrying`, and requeues the same
logical job. Exhaustion or a permanent required-channel failure sets
`delivery_state=failed`, records `final_failure_at`, terminalizes the job, and
writes a bounded terminal-failure audit; it never completes the reminder.

The repository exposes an internal trusted/operator manual-retry primitive.
It is version-aware and idempotent, rejects completed/cancelled reminders and
live leases, resets active retry state safely, reuses the deduped job, and
preserves all historical attempt rows. A3 adds no Telegram command or UI for
this capability.

## Crash and duplicate boundary

SQLite and a remote Telegram send are not one transaction. A3 persists the
attempt identity before sending, uses leases and correlation IDs, and commits
the canonical reminder/outbox result atomically after the provider result.
There remains an unavoidable at-least-once window:

```text
remote Telegram send succeeds
  -> process crashes before local success commit
  -> lease expires/recovery requeues the job
  -> a retry may produce a duplicate notification
```

The implementation does not claim exactly-once delivery. Crash-before-send,
provider failure, and crash-after-remote-success/local-commit tests document
these distinct outcomes. Completion/cancellation suppresses queued retries;
an in-flight provider request remains the unavoidable boundary described above.

## Rollout status

All A3 validation uses temporary file SQLite databases, injected clocks, and
fake Telegram/Home Assistant transports. No production deployment, service
restart, configuration change, or real Telegram send was performed.

`LIVE_PRODUCTION_PREFLIGHT_PENDING` remains an explicit future rollout gate.
It blocks production activation, not local A3 code, CI, or PR review.
