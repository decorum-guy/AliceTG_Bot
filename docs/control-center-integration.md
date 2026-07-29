# Artem Control Center integration

Home Assistant is the canonical authority for coffee state, activation time,
timing policy and command confirmation. This bot remains the Telegram user
interface for editing the policy.

## Timing helpers

- `input_number.coffee_warmup_minutes`
- `input_number.coffee_long_running_minutes`
- `input_datetime.coffee_last_turned_on` (read-only to the bot)

The bot reads the helpers at startup. Timing-dependent alerts pause when the
helpers cannot be read; local defaults are never written over Home Assistant
during an outage. Telegram changes call `input_number.set_value` and are
accepted only after a confirming helper read.

## One-time migration

Migration is deliberately not part of application startup:

```bash
python scripts/migrate_coffee_timing_to_ha.py
python scripts/migrate_coffee_timing_to_ha.py --apply
```

The first command is read-only. `--apply` writes only when HA still contains
the known initial 13/60-minute values, legacy values differ, and the local
migration marker is absent. Non-default HA values always win. Successful
migration writes a local idempotency marker. Neither command was run against
production in this change.

## Health

- `GET /health/live`: process liveness, public-safe.
- `GET /health/ready`: Telegram transport, HA and timing-helper readiness.
- `GET /health/details`: sanitized version/commit/dependency details; requires
  the existing `X-Internal-Secret`.

The details response never includes Telegram/HA tokens, chat IDs, usernames,
messages, webhook URLs, raw environment values or stack traces.
