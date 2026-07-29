# Artem Control Center integration

Home Assistant is canonical for coffee state, activation time, timing policy
and command confirmation. This bot remains the Telegram user interface for
editing the policy and schedules timing-dependent notifications.

## Timing helpers and refresh

- `input_number.coffee_warmup_minutes`
- `input_number.coffee_long_running_minutes`
- `input_boolean.coffee_timing_initialized`
- `input_datetime.coffee_last_turned_on` (read-only to the bot)

The helpers contain no permanent YAML `initial` values. A managed,
non-overlapping refresh loop reads them every 30 seconds by default, uses
bounded retry backoff, retains the last confirmed revision and reports cached
or stale state honestly. It recovers automatically after an HA outage without a
bot restart.

When the revision changes, active coffee alert timers are rescheduled exactly
once. An unchanged revision creates no duplicate tasks. Shutdown cancels the
refresh task. `/health/ready` reports state but does not drive recovery.

Configuration:

```text
COFFEE_TIMING_REFRESH_INTERVAL_SECONDS=30
COFFEE_TIMING_STALE_AFTER_SECONDS=90
COFFEE_TIMING_REFRESH_MAX_BACKOFF_SECONDS=120
```

Telegram changes call `input_number.set_value` and are accepted only after a
confirming helper read. HA outage never writes local defaults to HA.

## One-time initialization/migration

Migration is deliberately not part of HA or application startup:

```bash
python scripts/migrate_coffee_timing_to_ha.py status
python scripts/migrate_coffee_timing_to_ha.py dry-run
python scripts/migrate_coffee_timing_to_ha.py apply
```

`status` and `dry-run` are read-only. Explicit `apply` prefers stored legacy bot
values when present, otherwise preserves already configured non-default HA
values or uses bootstrap defaults of 13 minutes warm-up and 60 minutes
long-running. It writes both helpers, verifies read-back and only then enables
`input_boolean.coffee_timing_initialized`. Repeated runs do nothing once the HA
marker is on.

The 60-minute value means “работает слишком долго”; it is neither warm-up
duration nor a physical overheat signal. No production migration was run in
this change.

## Health

- `GET /health/live`: process liveness, public-safe.
- `GET /health/ready`: current Telegram, HA and timing-helper readiness.
- `GET /health/details`: sanitized version/commit/dependency and timing
  freshness details; requires the existing `X-Internal-Secret`.

The details response never includes Telegram/HA tokens, chat IDs, usernames,
messages, webhook URLs, raw environment values or stack traces.
