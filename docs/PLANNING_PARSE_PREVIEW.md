# Planning parse preview

This document defines the narrow, read-only parser surface used by the
Control Center Panel Agent. It extends the existing authenticated Planning
HTTP boundary described in [PLANNING_API_A4.md](PLANNING_API_A4.md) without
changing the A4 CRUD or A5a Alice contracts.

## Route and audience

The fixed route is available when `PLANNING_API_ENABLED=true`:

```http
POST /internal/planning/v1/parse
```

It accepts only the authenticated `panel-agent` audience using the existing
`X-Internal-Secret`, `X-Planning-Audience`, and `X-Planning-Secret` headers.
Home Assistant and operator audiences are denied. Credentials remain on the
Panel Agent side; no browser receives them.

## Request

The body is a strict JSON object. Unknown fields, duplicate JSON keys, nested
execution fields, and server-owned identity fields are rejected.

```json
{
  "text": "Напомни завтра в 15:00 позвонить врачу",
  "reference_time_utc": "2026-08-12T09:00:00Z",
  "timezone": "Europe/Moscow",
  "locale": "ru-RU"
}
```

`text`, `reference_time_utc`, and `timezone` are required. `locale` defaults
to `ru-RU`; it is bounded and only `ru-RU` is currently accepted. The
reference timestamp must be an explicit RFC3339 UTC value with a `Z` suffix,
and `timezone` must be a valid IANA timezone. Text is bounded to the existing
2,000-character parser input limit. The existing 64 KiB Planning request
body limit and authenticated per-audience rate limiter apply.

No actor, source, audience, object ID, URL, service/entity/path/command,
upstream endpoint, or parser-module selector is accepted in the body.

## Response

The route returns HTTP 200 for a valid parser request, including parser
ambiguities and parser error results. The response is a strict
`planning.v1` preview envelope:

```json
{
  "schemaVersion": "planning.v1",
  "kind": "parse_preview",
  "candidate": {
    "domain": "reminder",
    "operation": "create",
    "fields": {
      "title": "позвонить врачу",
      "due_at_utc": "2026-08-13T12:00:00Z",
      "timezone": "Europe/Moscow"
    },
    "normalized_paraphrase": "Напоминание «позвонить врачу» на 2026-08-13 в 15:00 (Europe/Moscow)."
  },
  "confidence": "high",
  "ambiguities": [],
  "requires_confirmation": false,
  "normalized_text": "Напомни завтра в 15:00 позвонить врачу",
  "error_code": null,
  "correlation_id": "…"
}
```

`candidate` is either `null` or the canonical typed `Candidate`. Its domain
is `reminder`, `task`, or `calendar_event`; its operation remains `create` or
`query`; and its fields and normalized paraphrase are produced by the shared
parser. Query candidates are returned as previews and are never executed.

Ambiguous phrases preserve the canonical `ambiguities[]` and
`requires_confirmation` values. In particular, `вечером`, `на следующей
неделе`, and an unqualified range such as `с пяти до семи` remain unresolved
according to the existing A5a parser semantics. The endpoint does not choose
a new meaning. Parser errors preserve the safe `error_code`; raw exception
messages are not returned.

## Pure and non-mutating boundary

The handler calls `PlanningParser.parse(ParserInput(...))` directly. It does
not call `AliceInterpretationService.interpret()` and never creates, edits,
completes, cancels, archives, deletes, schedules, or executes a Planning
object. It does not claim or write an idempotency key, audit event, outbox row,
or any other durable state. `Idempotency-Key` is not required. Repeated
identical inputs have equivalent parser semantics for the same text, explicit
reference time, timezone, and locale.

The parser has no Home Assistant, provider, URL, filesystem, shell, or LLM
execution boundary. Hostile-looking utterances are still only parser input.

## Privacy and consumer boundary

Raw utterances, candidate titles, and parse request/response bodies are not
logged or persisted for observability. Failure logs follow the existing
Planning API convention and contain only method, fixed route, correlation ID,
and exception class. Responses contain no credentials, database path, audit
state, source reference, provider payload, or stack trace.

This endpoint is a prerequisite for Control Center B4 preview rendering. B4
may use the candidate, restatement, confidence, ambiguities, and confirmation
flag to drive its UI and keep Save disabled while ambiguity remains. B4 is the
consumer boundary; this endpoint does not implement Control Center CRUD or
execution.
