# Planning v1 API (A4)

This document records the A4 implementation boundary for the internal
Planning HTTP adapter.  The feature is disabled by default and does not
enable the A2 reminder cutover or the A3 durable scheduler.

## Route surface

The application registers these explicit routes under
`/internal/planning/v1` when `PLANNING_API_ENABLED=true`:

```text
GET    /reminders?state=&from=&to=&limit=&offset=
POST   /reminders
PATCH  /reminders/{id}
POST   /reminders/{id}/complete
POST   /reminders/{id}/cancel

GET    /tasks?view=today|overdue|upcoming&project_id=&limit=&offset=
POST   /tasks
PATCH  /tasks/{id}
POST   /tasks/{id}/complete
DELETE /tasks/{id}

GET    /events?from=&to=&limit=&offset=
POST   /events
PATCH  /events/{id}
DELETE /events/{id}

GET    /projects?limit=&offset=
GET    /status
```

`/alice/interpret` is intentionally not implemented in A4; it belongs to
A5.  There is no generic `/objects`, `/execute`, `/proxy`, `/action`,
`/tool` or `/service` operation.  Unmatched paths remain inside the Planning
error envelope and return a redacted `404` only.

## Authentication and permissions

Requests must pass both the existing internal secret boundary and a separate
audience credential.  The headers are:

```text
X-Internal-Secret: <existing INTERNAL_WEBHOOK_SECRET>
X-Planning-Audience: ha | panel-agent | operator
X-Planning-Secret: <audience-specific secret>
```

Audience secrets are configured with `PLANNING_HA_SECRET`,
`PLANNING_PANEL_AGENT_SECRET`, and the optional `PLANNING_OPERATOR_SECRET`.
They must be independently rotatable.  Secret comparison uses
`hmac.compare_digest`; secrets are never put in query strings, logs,
responses, audit representations, or fixtures.  The audience header only
selects the configured credential; it does not establish identity on its
own, and a body `audience` field is rejected.

| Route class | `ha` | `panel-agent` | `operator` |
| --- | --- | --- | --- |
| Read reminders/tasks/events/projects/status | yes | yes | yes |
| Reminder create/edit/complete/cancel | no | yes | yes |
| Task create/edit/complete/archive | no | yes | yes |
| Local event create/edit/delete | no | yes | yes |

Authenticated mutation context is derived from the audience.  A4 does not
accept caller-supplied actor, surface, source, or source reference fields:

```text
ha           -> planning-ha,           service,   ha
panel-agent  -> planning-panel-agent,  service,   panel-agent
operator     -> planning-operator,     operator,  operator
```

The canonical source enum is derived by the repository from that trusted
surface.  Home Assistant is intentionally read-only in A4; its mutation
adapter is A5 work.

## Strict requests and limits

The API uses small standard-library schema parsers with explicit allowlists
and duplicate-JSON-key rejection.  Unknown fields and server-owned fields are
errors.  Canonical IDs, versions, timestamps, audit/source metadata,
`request_hash`, audience and delivery/sync state are response/repository
fields, never create or full-object PATCH input.  Action bodies are empty
objects only.

The current conservative limits are:

| Item | Limit |
| --- | --- |
| Request body | 64 KiB |
| Query string | 1 KiB; each value 128 characters |
| Idempotency-Key | 256 characters |
| List page | default 50, maximum 100 |
| Offset | maximum 10,000 |
| Reminder/event UTC range | maximum 366 days |
| Rate limiting | 120 authenticated requests per audience per minute per process |

The rate limiter is intentionally in-process; it does not claim distributed
or global enforcement and does not add Redis or another service.

Task dates remain date-only when no local time is supplied.  Timed tasks use a
local wall-clock time plus IANA timezone.  Reminders and timed events require
UTC `Z` timestamps plus IANA timezone.  Events enforce timed/all-day
exclusivity, exclusive all-day end dates, `end > start`, disabled recurrence,
and `sync_state=local_only`.  A4 performs no provider network writes.

## Mutation transaction and replay

Every mutation requires `Idempotency-Key`.  Edits, actions, archive and event
tombstone deletes additionally require a positive `If-Match` version.  The
effective idempotency identity is `(authenticated audience, key)`.

The service computes a deterministic SHA-256 request hash over the
authenticated audience, fixed route/action, object ID, normalized body,
expected version, and trusted actor context.  The client cannot provide this
hash.  Within the A1 `BEGIN IMMEDIATE` transaction, A4:

1. claims the idempotency key;
2. runs the existing repository mutation and its audit/outbox work;
3. builds the final canonical object envelope and correlation ID;
4. stores the exact serialized response JSON; and
5. commits domain mutation, audit, and idempotency response together.

Any exception rolls back all of those records.  A same-audience retry with the
same hash returns the exact stored JSON, including the original object,
timestamps and correlation ID.  Reuse with a different hash returns
`409 idempotency_conflict`; an active claim follows the storage primitive's
`idempotency_in_progress` semantics.  Stale `If-Match` values return
`409 version_conflict` with bounded version details and no full object.

Task DELETE is an archive/tombstone transition.  Reminder cancel and event
DELETE are logical tombstones; physical rows remain for audit/history.  Normal
active list views exclude tombstones, while an explicit reminder
`state=cancelled` query is the bounded cancelled audit view.

## Envelopes and status

Object and list responses are built centrally with `schemaVersion:
planning.v1`, typed domain data, `sourceStatus`, `lastSyncedAt`, `staleAfter`,
and a UUIDv4 `correlation_id`.  Lists additionally carry `generatedAt` and
explicit bounded pagination metadata.  Local Planning SQLite is reported as
the current authoritative source; no external TickTick/calendar freshness is
claimed.  Errors use the same versioned envelope with stable machine-readable
codes, bounded details and safe messages.

`GET /status` contains only API/storage capability facts, freshness metadata,
and audience-scoped capabilities.  It does not expose titles, notes,
Telegram identifiers, secrets, raw rows, database paths, transcripts or
provider responses.

## Feature and rollout state

`PLANNING_API_ENABLED=false` is the default.  Enabling the API requires both
audience secrets and the existing `INTERNAL_WEBHOOK_SECRET`; invalid security
configuration fails closed.  A4 leaves
`PLANNING_REMINDER_CUTOVER_ENABLED` and
`PLANNING_DURABLE_SCHEDULER_ENABLED` unchanged.  `LIVE_PRODUCTION_PREFLIGHT_PENDING`
remains a later rollout gate.  A4 tests use synthetic credentials and
temporary file SQLite databases only; no deployment, restart, provider call,
or real Telegram message is part of this phase.
