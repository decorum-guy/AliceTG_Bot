# Planning v1 contract freeze

Status: A0 contract-only freeze; no Planning runtime implementation.

Reference: `decorum-guy/artem-control-center` issue #61 and merged planning PR #62, commit `98f1414643db4c9ee8e25e3eb95bfb2af3e7c46d`.

This document freezes the domain and envelope shapes that later Planning
implementation batches must accept. It does not add routes, storage, schema
migrations, schedulers, Home Assistant adapters, Alice handling, Telegram
behavior or Control Center code.

## Contract-wide rules

- Planning v1 has three distinct domains: `reminder`, `task` and
  `calendar_event`. They are discriminated by the `domain` field and are never
  collapsed into a generic item.
- Internal object IDs, project IDs and audit correlation IDs are UUIDv4.
- Timestamps crossing an API or storage boundary are UTC RFC3339 strings with
  a `Z` suffix. A separate IANA timezone carries the local interpretation.
- Object `version` is a positive integer and changes on every canonical object
  mutation.
- Domain objects carry `source`, optional `source_ref`, and an immutable
  `audit_correlation_id`. Mutations also carry explicit actor/surface metadata.
- Every mutation request carries an external `Idempotency-Key`. The effective
  idempotency identity is `(audience, key)`, where `audience` comes from the
  authenticated context. Planning computes and retains the request hash with
  the stored canonical response; the client never supplies that hash.
- Contract models are strict: unknown fields are rejected. Optional fields may
  be omitted or be explicit `null` where the field definition permits it.
- No accepted contract field can carry an HA service/entity, shell command,
  executable, arbitrary URL, host or filesystem path. The transcript/voice
  boundary is an allowlisted domain command, never a generic execution
  primitive.

## Domain objects

All three objects include the common fields `id`, `domain`, `source`,
`version`, `created_at`, `updated_at` and `audit_correlation_id`. `source_ref`
is an opaque source identifier, not an execution target.

### Reminder

Required fields:

```text
id, domain=reminder, title, due_at_utc, timezone, status,
source, created_by, delivery_state, version, created_at, updated_at,
audit_correlation_id
```

Optional fields:

```text
notes, source_ref, completed_at, cancelled_at, next_attempt_at,
final_failure_at, deleted_at
```

`status` is `pending | due | completed | cancelled`.
`delivery_state` is `not_due | queued | retrying | delivered | failed`.
Object status and delivery status are independent: delivery does not complete
the reminder. Cancellation is a tombstone transition, not a physical delete.
All reminder times, including retry timestamps, are UTC RFC3339; `timezone` is
the IANA timezone used for presentation and local-time parsing.

### Task

Required fields:

```text
id, domain=task, title, priority, status, source, version,
created_at, updated_at, audit_correlation_id
```

Optional fields:

```text
notes, due_date, due_time, timezone, project_id, source_ref,
completed_at, archived_at, deleted_at
```

`priority` is `none | low | normal | high`.
`status` is `open | completed | archived`.

`due_date` is a calendar date (`YYYY-MM-DD`). `due_time`, when present, is a
local wall-clock value (`HH:MM` or `HH:MM:SS`) and requires a date and an IANA
timezone. A date-only task has no `due_time` and is never represented as
midnight UTC or any other invented timestamp.

### Calendar event

Required fields:

```text
id, domain=calendar_event, title, all_day, timezone, sync_state,
source, version, created_at, updated_at, audit_correlation_id
```

Optional fields:

```text
notes, location, start_at_utc, end_at_utc, start_date,
end_date_exclusive, recurrence_rule, provider_id,
provider_calendar_id, source_ref, deleted_at
```

When `all_day` is `false`, the object must contain `start_at_utc` and
`end_at_utc` and must not contain the date-only pair. When `all_day` is
`true`, it must contain `start_date` and `end_date_exclusive` and must not
contain timed fields. All-day `end_date_exclusive` is exclusive and later than
`start_date`. Timed end is later than timed start. `sync_state` is
`local_only | pending | synced | stale | conflict | error`.

`recurrence_rule` is reserved for the approved future shape but is not
interpreted or executed in A0; no recurrence behavior is implemented here.
Provider and calendar identity are metadata only. A local-only event must be
presented as not synchronized.

## Versioned envelopes and API semantics

Every Planning v1 envelope has `schemaVersion: "planning.v1"` and a UUIDv4
`correlation_id`. The future route prefix is `/internal/planning/v1`; A0 does
not create that route or any runtime API.

### Canonical object and list responses

A canonical object response has this shape:

```json
{
  "schemaVersion": "planning.v1",
  "kind": "object",
  "domain": "reminder",
  "object": { "...": "canonical domain object" },
  "sourceStatus": "current",
  "lastSyncedAt": "2026-08-11T08:00:00Z",
  "staleAfter": "2026-08-11T08:05:00Z",
  "correlation_id": "8d4a9c5b-1e3f-4a72-9c6b-5e7f1d2a3b04"
}
```

The `object` is authoritative after a mutation; clients do not announce a
write as saved before receiving it. A list response uses the same freshness
metadata plus `generatedAt` and a typed `items` array. `sourceStatus` is
`current | stale | offline | degraded`; `lastSyncedAt` may be `null` when no
source snapshot exists, while `staleAfter` remains an explicit UTC time when
the source can calculate one.

### Canonical task list views

The fixed A4 route `GET /internal/planning/v1/tasks` accepts the task view
values `today`, `overdue`, `upcoming`, and `undated`, with the existing
bounded `limit`/`offset` pagination and optional `project_id` filter.  The
`undated` view is the explicit `Без срока` / Inbox projection: it returns only
active/open canonical Planning tasks with `due_date IS NULL`.  It is a
projection over the canonical `tasks` table, not a separate store.  Completed,
archived, and deleted/tombstoned rows are excluded.  Undated tasks retain
`due_date: null`, `due_time: null`, and `timezone: null` in the existing task
object and use deterministic `created_at, id` ordering.

The dated views retain their existing semantics: `today` is the caller's
local date, `overdue` is before it, and `upcoming` is after it.  `upcoming`
does not include undated tasks.

### Mutation request, actor and idempotency boundary

The client/server trust boundary is explicit. A task create is sent to the
domain route with an external `Idempotency-Key` header and only client-settable
domain fields in the body:

```http
POST /internal/planning/v1/tasks
Idempotency-Key: fixture-idempotency-task-001
```

```json
{
  "title": "Create fixture task",
  "notes": null,
  "due_date": "2026-08-21",
  "priority": "low",
  "project_id": null
}
```

The create body must not contain `id`, `version`, `created_at`, `updated_at`,
`audit_correlation_id`, `source`, `source_ref`, `created_by` or
`request_hash`. Planning generates the internal UUIDv4, object version,
timestamps, audit correlation and canonical source metadata. Source metadata
derived from the authenticated actor/surface is not trusted when copied from a
client body.

The authenticated context supplies the audience and actor/surface metadata:

```json
{
  "audience": "panel-agent",
  "actor": {
    "id": "fixture-operator",
    "type": "operator",
    "surface": "panel-agent"
  }
}
```

`audience` is not an arbitrary mutation-body field. Planning computes the
request hash internally from the authenticated request and stores it with the
`Idempotency-Key` and the exact canonical response. For the same `(audience,
key)` and identical request hash, the server returns the exact previously
stored canonical response. Reusing the key with a different request hash is an
idempotency conflict. The persistence primitive is an A1 concern.

The canonical object is response-only. It contains the server-owned `id`,
`version`, `created_at`, `updated_at`, audit correlation and source metadata.
The `mutation_idempotency.json` fixture is a composite review example with
separate `request`, `authenticated_context` and `server_record` sections; it
must not be interpreted as a request envelope containing trusted server data.

Edits and state transitions identify their target through the route/object ID,
carry `If-Match`/expected-version semantics and contain only mutable fields or
the fixed action. They never accept a full canonical object as trusted input.
The `edit_state_transition.json` fixture shows a completion request with an
`If-Match` version and a canonical response at the newer version.

### Error and conflict envelope

Errors have `kind: "error"`, a stable machine-readable `error.code`, safe
human-readable `error.message`, bounded typed `error.details`, HTTP status and
the same correlation/freshness metadata. The expected-version conflict is
`http_status: 409` and `error.code: "version_conflict"`. Error details never
contain raw transcripts, secrets or arbitrary execution fields.

### Alice interpretation response

The narrow Alice response keeps the approved kinds
`answer | confirmation_required | created | query_result | error` and the
fields `speech`, `end_session`, `pending_confirmation_id` and `object`.
Ambiguous or low-confidence input returns a clarification/confirmation result
with no saved object. HA remains an ingress/response adapter; it does not gain
Planning persistence or date parsing.

## Deterministic fixtures

The JSON fixtures under `tests/fixtures/planning_v1/` are synthetic, stable
examples with no production secrets, Telegram IDs, personal reminder titles or
voice transcripts. `tests/test_planning_v1_contracts.py` validates the fixture
shapes with standard-library checks, including strict keys, enums, UUIDv4,
UTC timestamps, IANA timezones, date-only semantics, timed/all-day exclusivity,
freshness metadata, idempotency, version conflicts and rejection of generic
execution fields.

| Fixture | Coverage |
| --- | --- |
| `valid_reminder.json` | Reminder object and independent delivery state |
| `valid_date_only_task.json` | Date-only task with no invented midnight |
| `valid_timed_task.json` | Timed task with local time plus IANA timezone |
| `valid_all_day_event.json` | Exclusive-end all-day event |
| `valid_timed_event.json` | UTC timed event and provider identity metadata |
| `source_freshness_metadata.json` | Typed list envelope and freshness fields |
| `mutation_idempotency.json` | Create request/response trust boundary and replay key |
| `edit_state_transition.json` | Fixed action, `If-Match` and canonical response |
| `conflict_error_envelope.json` | Expected-version `409` error envelope |
| `alice_created_response.json` | Versioned Alice response envelope |
| `invalid_*.json` | Unknown, unsafe, client-server-field, enum, event-shape and time-zone rejection cases |

## Verified runtime and persistence facts

- Supported runtime is Python 3.12 (`Dockerfile` uses `python:3.12-slim`; CI
  sets up Python `3.12`).
- The deployment model is Docker Compose. The `telegram-bot` service declares
  the bind mount `./data/telegram-bot:/app/data`.
- Current reminder persistence is JSON at `/app/data/reminders.json`, from
  `REMINDERS_STATE_PATH`; the related bot state is `/app/data/state.json`.
- The current legacy reminder store has its own 12-character IDs and legacy
  statuses. A future A2 import must preserve source/status semantics, map IDs
  to Planning UUIDv4 values exactly once, compare counts and semantic hashes,
  and keep the JSON source recoverable during the rollback window. A0 does not
  create a database or migrate any reminder.

> Live production persistence of /app/data has not yet been independently verified in this implementation session. This is an A1 production-preflight STOP gate and must be proven before any Planning database production activation/migration.

This gate is especially required before A2 legacy reminder migration and
repository cutover. The Compose declaration is sufficient evidence for the A0
contract freeze, but it is not a substitute for the A1 production preflight.

## A0 boundary

This PR contains documentation, synthetic contract fixtures and contract
validation tests only. It contains no SQLite implementation, migrations,
repositories, scheduler, Planning API routes, HA changes, Alice changes,
Telegram behavior changes, Control Center changes, Windows/Samsung changes or
deployment actions.
