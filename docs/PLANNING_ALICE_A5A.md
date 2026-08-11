# Planning Alice interpreter — A5a

A5a adds a deterministic Russian Planning parser and the bot-side Alice
adapter. It is an implementation-only phase: Home Assistant routing, live
Yandex acceptance, production deployment, Telegram task/event UX, TickTick,
and external calendar providers are outside this change.

## Boundary and flow

The code path is deliberately narrow:

```text
POST /internal/planning/v1/alice/interpret
        ↓ strict request parser + HA authentication
AliceInterpretationService
        ↓ typed ParserInput / ParseResult
PlanningParser
        ↓ canonical repository transaction + A4 idempotency store
PlanningRepository / Planning SQLite
```

The parser has no `aiohttp`, Home Assistant, Yandex, Telegram, provider,
filesystem, URL, shell, or LLM dependency. The output is a closed typed
candidate, not a generic action or tool payload.

## Supported grammar

The parser accepts locale `ru-RU` and requires an explicit UTC reference time
and IANA timezone. It supports:

- relative time: `через минуту`, `через N минут`, `через час`, `через N часов`,
  `через полчаса`, `через полтора часа`, `через день`, and `через N дней`;
- anchored dates: `сегодня`, `завтра`, `послезавтра`;
- common weekday forms, including `во вторник` and `в среду`;
- dates such as `15 августа` and `15 августа 2026`. A yearless date resolves
  to the next non-past occurrence relative to the supplied reference date, and
  the chosen year is present in the normalized paraphrase/speech;
- explicit clocks: `в 10`, `в 10:30`, `в 17:00`, and word clocks with an
  explicit part-of-day suffix such as `в пять вечера`;
- ranges such as `с 17:00 до 19:00` and `с пяти до семи вечера` when the
  date and part of day are established;
- date-only tasks, exact-time reminders, all-day events, timed events with an
  explicit start/end, and the first bounded query intents.

The first supported domains are `reminder`, `task`, and `calendar_event`. The
first supported operations are `create` and `query`. Recurrence, arbitrary
actions, provider writes, and generic execute operations are not supported.

## Typed parser result

`ParseResult` contains:

- an optional `Candidate` with `domain`, `operation`, structured allowlisted
  fields, and a normalized paraphrase;
- `confidence`: `high`, `medium`, or `low`;
- typed `ambiguities` with a field, candidates, and reason;
- `requires_confirmation`;
- normalized text and a safe parser error code when applicable.

Only a high-confidence `create` candidate with no ambiguity and no
confirmation requirement can reach the repository mutation path.

## Ambiguity and no-write policy

Any ambiguity returns `kind=confirmation_required`, `end_session=false`, a
bounded Russian clarification, and no saved object. This includes:

- a command such as `запиши завтра в 10` without a domain;
- `на следующей неделе` without a concrete day/date;
- recurrence requests;
- a time range without an established part of day;
- a start-only timed event;
- low/medium-confidence or invalid parser results.

The word `вечером` is intentionally unresolved in A5a. No default such as
18:00, 19:00, or 20:00 is chosen. The explicit suffix in `в пять вечера` is
different: it establishes 17:00 and is accepted.

For a start-only event, A5a proposes a 60-minute interval in the clarification
but does not create an event. The user must repeat a complete phrase with both
start and end. `pending_confirmation_id` remains `null`; no persistent
multi-turn confirmation state is claimed.

## Timezone and DST policy

All relative arithmetic uses the supplied `reference_time_utc`. Explicit local
times are resolved with the supplied IANA timezone and normalized to UTC
RFC3339 with a `Z` suffix. Nonexistent local wall times during a DST
spring-forward return a safe parser error. Ambiguous fall-back wall times are
also rejected unless a future phase supplies explicit disambiguating context.
Europe/Moscow uses the same generic timezone path.

Date-only tasks retain `due_date` with `due_time=null` and `timezone=null`;
they are never converted to midnight UTC. All-day events use an exclusive next
date in `end_date_exclusive`. Timed events and all-day events remain mutually
exclusive. A5a event records use `sync_state=local_only`.

## Alice request contract and authorization

The route is enabled only when `PLANNING_ALICE_INTERPRET_ENABLED=true`.
The strict JSON body accepts only:

```json
{
  "text": "завтра в 10 напомни позвонить Егору",
  "intent": "",
  "dialog": "alice_planning",
  "application_id": "…",
  "session_id": "…",
  "message_id": "…",
  "request_id": "…",
  "user_id": "…",
  "reference_time_utc": "2026-08-12T09:00:00Z",
  "timezone": "Europe/Moscow",
  "locale": "ru-RU",
  "correlation_id": "…"
}
```

`text`, `reference_time_utc`, and `timezone` are required. Metadata is
optional; the future HA adapter may flatten the current Yandex
`session.dialog` value into the scalar `dialog` field. Unknown fields,
duplicate JSON keys, server-owned fields, and structural execution fields
(`service`, `entity_id`, `shell`, `command`, `url`, `host`, `path`, arbitrary
headers/methods, `source`, `audience`, and `request_hash`) are rejected.

The A4 centralized route matrix grants `POST /alice/interpret` only to the
authenticated `ha` audience. `panel-agent` and `operator` cannot use it, and
HA does not gain generic Planning domain CRUD writes. The body cannot claim an
audience, source, surface, or actor. The authenticated HA context supplies:

- actor `{id: "planning-ha", type: "service", surface: "ha"}`;
- canonical object `source="alice"`;
- trusted source reference `alice:yandex`.

Created responses preserve the frozen Planning v1 Alice envelope:
`schemaVersion`, `kind`, `speech`, `end_session`,
`pending_confirmation_id`, `object`, `correlation_id`, and `actor`.

## Idempotency

Creates reuse the A4 `claim_idempotency` / `store_idempotency_response`
transaction primitive. The domain mutation, audit record, and exact Alice
response are committed atomically.

Yandex `message_id` is session-scoped, so it is stable only when
`session_id` is also present. The preferred event identity material is the
canonical JSON object below, with `kind="yandex-message"`,
`application_id` (or an empty string when unavailable), `session_id`, and
`message_id`:

```json
{
  "application_id": "…",
  "kind": "yandex-message",
  "message_id": "…",
  "session_id": "…"
}
```

The database key is `alice:yandex-message:{hex}`, where `hex` is an
HMAC-SHA256 digest using `PLANNING_ALICE_IDEMPOTENCY_SECRET`. If `message_id`
is absent, a `request_id` is used only when an application or session identity
is available, with `kind="yandex-request"` and the same private HMAC shape.
An unscoped `request_id`, and a `message_id` without `session_id`, are not
treated as globally stable; those requests use the private fallback heuristic.
For the fallback, the adapter builds canonical UTF-8 JSON with sorted keys and
compact separators:

```json
{
  "application_id": "…",
  "session_id": "…",
  "normalized_command": "…",
  "timezone": "Europe/Moscow",
  "time_bucket_15s": 119101680
}
```

The key is `alice:hmac:{hex}`, where `hex` is HMAC-SHA256 of that JSON using
`PLANNING_ALICE_IDEMPOTENCY_SECRET`. The secret and raw Yandex identifiers are
neither stored nor exposed. The raw full transcript is not used as a database
key.

The request hash covers stable semantics: authenticated audience and actor,
route, the scoped event identity, normalized command, timezone, locale, intent,
and dialog. It intentionally excludes ephemeral `reference_time_utc` and
derived candidate timestamps. Therefore a retransmission in the same scoped
event identity or fallback 15-second bucket returns the exact first response,
including its original relative due time, object UUID, correlation ID, and
canonical timestamps. A materially different normalized command under the same
stable event identity raises `idempotency_conflict` before a second mutation.

## Queries and speech

A5a supports:

- open tasks due today plus an overdue count;
- active pending/due reminders in due-time order;
- events/plans for an explicit supported local day, with all-day events before
  timed events.

Responses contain at most the first three useful items and say `Ещё N` when
more exist. Speech is capped at 900 characters; titles are capped separately
for speech. UUIDs, correlation IDs, tokens, database errors, provider errors,
and stack traces are not spoken.

## Flags and coexistence

The new route is disabled by default. Enabling it requires both
`PLANNING_HA_SECRET` and `PLANNING_ALICE_IDEMPOTENCY_SECRET`. A4's existing
`PLANNING_API_ENABLED` gate and routes remain independent. The legacy
`/internal/reminders/alice-create` parser/workflow remains registered and
unchanged when the new flag is off. A5a does not dual-write and does not
change Home Assistant configuration; A5b will review HA routing/cutover.

## Scope boundary

No Home Assistant files, automations, `rest_command`, Yandex configuration,
live HA state, production deployment, restart, Telegram delivery, external
calendar, or TickTick integration is part of A5a. Tests use synthetic Alice
requests and local SQLite only. A5b and A6 are intentionally not started.
