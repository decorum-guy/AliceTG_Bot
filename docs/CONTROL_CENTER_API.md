# Control Center coffee API

This feature branch adds a narrow internal API for Artem Control Center. It is
not a generic Home Assistant proxy and is not deployed by this pull request.

## Authentication

All routes below require:

```text
Authorization: Bearer <CONTROL_CENTER_API_TOKEN>
```

`CONTROL_CENTER_API_TOKEN` is independent from `SHORTCUTS_SECRET_TOKEN`,
Telegram credentials, Home Assistant credentials and the existing internal
webhook secret. It is loaded only from runtime environment and compared with
`hmac.compare_digest`.

The existing `/shortcut/espresso` endpoint and its personal token are
unchanged.

## Routes

```text
GET   /internal/notification-settings/coffee
PATCH /internal/notification-settings/coffee
GET   /internal/control-center/coffee/timing
PATCH /internal/control-center/coffee/timing
POST  /internal/control-center/coffee/action
```

Notification settings contain only warm-up/long-running enable flags and their
Telegram/iPhone channel flags. Timing and delivery receipts are excluded.
PATCH is partial, uses `expectedRevision`, persists atomically and reschedules
active alerts exactly once only when the effective settings changed.
The candidate state is flushed and atomically replaced before in-memory state
changes. Persistent opaque revision and UTC `updatedAt` metadata change only
for an effective mutation. This prevents ABA revision reuse (`A → B → A`).
Legacy state receives metadata without changing effective notification values.
Write/replace failures return a sanitized `503`, preserve the prior file and
memory, and do not reschedule alerts.

Timing is read from and written to:

- `input_number.coffee_warmup_minutes`;
- `input_number.coffee_long_running_minutes`.

Writes use the actual helper `min`, `max` and `step`, require whole minutes,
perform HA read-back, return `409` on a stale revision and never change the
initialization marker. Telegram and Control Center both use
`CoffeeTimingPolicyService`; its managed refresher makes changes visible
without a bot restart.

Timing mutations are serialized by one `asyncio.Lock`. Canonical HA state and
the initialization marker are read again inside the lock before checking
`expectedRevision`. Each helper write is read back before the next write.
Partial failure triggers rollback followed by exact two-helper read-back. An
unconfirmed rollback clears the cached policy and returns the stable
`timing_state_unknown` error; the next GET must read Home Assistant again.

Coffee actions accept only `turn_on` and `turn_off`. `requestId` provides
in-process idempotency, already-satisfied state is a successful no-op, and a
response is successful only after Home Assistant confirms the target state.
There is no arbitrary domain, service or entity input.

## Status mapping

- `400` — invalid schema or helper-compatible value;
- `401` — missing Bearer header;
- `403` — invalid Control Center token;
- `409` — stale revision or idempotency conflict;
- `429` — bounded action rate limit;
- `503` — API disabled, HA unavailable or confirmation failed.

Mutation responses use `Cache-Control: no-store`. Tokens, user identifiers,
notification target names and raw upstream errors are absent from responses.

## Health semantics

`/health/live` depends only on the running process. `/health/ready` returns
`200` only when Telegram transport is ready, the coffee entity is readable,
both timing helpers exist, `input_boolean.coffee_timing_initialized` is on and
the canonical timing policy was freshly confirmed. Cached, stale,
uninitialized or unavailable timing produces sanitized `503`.

## Local verification

```text
/Users/aartemida/Documents/artem-control-panel-proj/.tooling/alice-bot-venv/bin/python \
  -m unittest discover -s tests -v
/Users/aartemida/Documents/artem-control-panel-proj/.tooling/alice-bot-venv/bin/python \
  -m compileall app tests
git diff --check
```

Deployment requires the HA helper package to be applied and initialized first,
then the same new random Control Center token to be installed independently in
the bot and Panel Agent runtime environments. No migration or device action is
part of deployment verification.
