# Planning A7 — task and calendar-event domain services

## Scope

A7 adds provider-neutral domain-service foundations on top of the existing
`PlanningRepository`. The services do not add SQL, routes, providers, network
calls, Telegram creation flows, or recurrence. A4 API envelopes, idempotency,
audit, `If-Match`, and audience authorization remain the public contract.

The service boundaries are:

```text
API / Alice parser / Telegram views
        ↓
TaskService · ProjectService · EventService
        ↓
PlanningRepository
        ↓
SQLite
```

## Tasks

`TaskService` owns create, update, complete, archive/tombstone, get, and the
`today`, `overdue`, and `upcoming` views. It delegates persistence and version
guards to the repository; it does not duplicate SQL. Open-view queries continue
to exclude completed and archived/tombstoned tasks. A project filter is passed
to the existing repository primitive.

The priority enum remains closed:

```text
none · low · normal · high
```

### Date-only and timed tasks

`due_date` without `due_time` is a date-only value. Its `timezone` is also
`null`. It is never represented as midnight UTC, midnight in a user timezone,
or any other instant. Service, API, parser, and Telegram round-trips retain
this shape.

A timed task requires `due_date`, `due_time`, and an IANA timezone. The shared
`resolve_local_datetime` helper enumerates both `zoneinfo` folds and
round-trips them. It rejects both a nonexistent spring-forward wall time and
an ambiguous fall-back wall time; it never silently selects `fold=0` or
`fold=1`. The parser delegates to the same resolver while retaining its
existing Russian error codes and wording. Tests cover Europe/Moscow and
Europe/Berlin, including both Berlin DST transitions.

### Caller-timezone views

Every canonical task view takes an explicit `reference_time_utc` and caller
IANA timezone. The service converts that instant to the caller's local date
and delegates the date-based repository view. It does not call
`date.today()` or use the machine-local timezone. Existing product semantics
are preserved: `today` is the local date, `overdue` is before it, and
`upcoming` is after it.

## Projects

`ProjectService` formalizes get, active-list, create, update, and tombstone
operations. There is no automatically seeded default project and no provider
identity. Project listing remains deterministic and excludes tombstones.

Project tombstoning preserves the task's `project_id`. The existing physical
foreign-key restriction, rather than a cascade or reassignment, protects
referential integrity. The future UI decision about how to present tasks that
reference a tombstoned project remains intentionally unresolved.

## Calendar events

`EventService` owns create, update, get, delete/tombstone, bounded range, and
`today`, `tomorrow`, and `upcoming` queries. It uses the existing repository
primitives. Native Planning events are always:

```text
sync_state = local_only
provider_id = null
provider_calendar_id = null
```

No Google, iCloud, Exchange, CalDAV, TickTick, or other provider is implied or
called.

The event shape is strictly one of:

```text
timed:   start_at_utc + end_at_utc + timezone
all-day: start_date + end_date_exclusive + timezone
```

All-day `end_date_exclusive` is exclusive. A multi-day event overlaps every
local calendar date in `[start_date, end_date_exclusive)`. Range queries use a
half-open interval and return all valid overlaps, including events beginning
before the range, ending after it, or spanning the entire range. An event that
ends exactly at the range start or starts exactly at the range end does not
overlap. Ordering is deterministic: all-day first, then local timed start, then
object ID. Overlapping events are valid and are not rejected as conflicts.

The query helpers accept an explicit UTC range/reference time and caller
timezone. Local day boundaries are resolved in that timezone, including
Europe/Moscow and Europe/Berlin DST dates; UTC is not treated as the caller's
calendar. Timed events crossing UTC/local midnight are included in the caller
day they actually overlap. All-day events retain their date representation and
are never converted to UTC-midnight instants.

### Start-only event proposal

The shared `propose_default_event_end` helper proposes an end exactly 60 local
minutes after a supplied start. It returns an `EventEndProposal` only; it does
not write an event or create confirmation state. A caller must present and
accept the complete event shape before calling the repository/service create
operation. If the proposed local boundary is nonexistent or ambiguous because
of DST, the helper fails safely.

This preserves A5a's `confirmation_required` behavior. There is no multi-turn
confirmation store.

## Recurrence and capabilities

Recurrence remains disabled. Non-null recurrence continues to fail validation;
A7 does not parse or round-trip recurrence rules.

`PlanningCapabilityMetadata` is a typed, frozen, provider-neutral structure.
It describes task read/create/update/complete/archive and local authority;
event read/create/update/delete, `recurrence=false`,
`provider_sync=false`, and `local_only=true`; and the supported local project
management operations. It contains no object data and accepts no arbitrary
client capabilities. A4 `/status` keeps its existing audience-scoped
`capabilities` field and additionally exposes this content-free metadata as
`capabilityMetadata`.

## Regression and deferred decisions

The A4 API, A5 Alice grammar, and A6 Telegram callback/token behavior remain
unchanged. Alice still writes only high-confidence unambiguous candidates,
preserves `source=alice`, retains date-only tasks, and does not write a
start-only event. Telegram still has views/actions only; it does not gain task
or event creation and no snooze presets were added.

The following decisions remain deliberately deferred:

```text
SNOOZE_PRESET_PRODUCT_DECISION_PENDING
TELEGRAM_TASK_EVENT_CREATION_PRODUCT_DECISION_DEFERRED
```

No provider integration, external sync, provider selection, or A8 work is part
of this change.

## Migrations and validation

No migration 005 was needed. Existing migrations 001–004 and all existing rows
are preserved.

The A7 tests use temporary file-backed SQLite databases and injected clocks.
They cover task/date/DST/view/project/version rules, event shape/day/range/
overlap/all-day/local-only rules, the 60-minute proposal boundary,
capabilities, and integrity checks. The full A0–A6 suite remains a required
regression gate.

## P0 production result

The already-merged A6 commit was rolled out separately before this branch was
created. The live preflight found a clean checkout, valid legacy reminder data,
no pending/overdue reminders, a deterministic A2 import, and an intact source
file. The production checkout was fast-forwarded exactly to
`db238aff927e92af7e9f1c6453bccaff03888c13`; the documented backup/import
procedure completed, the second import was a no-op, and SQLite integrity was
`ok`.

The three authorized Planning flags were enabled and only the AliceTG_Bot
container was recreated. Live health, schema migration 004, import marker,
cutover adapter, durable-only scheduler mode, and Telegram UI gate were
verified. The A7 branch was not deployed.
