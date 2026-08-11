# Planning Telegram monitoring and safe actions (A6)

Status: A6 implementation. The surface is an admin-only, feature-gated
monitoring pilot. Production flags remain disabled and no live Telegram
acceptance test or deployment is part of this phase.

## Gate and authority

The UI is disabled by default:

```text
PLANNING_TELEGRAM_UI_ENABLED=false
PLANNING_TELEGRAM_ACTION_TOKEN_TTL_SECONDS=900
PLANNING_TELEGRAM_CALLBACK_RATE_LIMIT_PER_MINUTE=30
```

Enabling the UI fails closed unless both
`PLANNING_REMINDER_CUTOVER_ENABLED=true` and
`PLANNING_DURABLE_SCHEDULER_ENABLED=true` are also enabled. This keeps the
Telegram reminder view on the same canonical SQLite authority used by the A3
scheduler. `PLANNING_API_ENABLED` is not required because the UI is an
in-process Planning client.

Only `Settings.is_admin_user(...)` / `TELEGRAM_ALLOWED_USER_IDS` can open the
new Planning menu or perform an action. Sonya/other allowed-user classes are
not broadened. Authorization is checked again for every callback.

## Menu and views

When enabled, the admin main menu gains `📋 Дела`:

- `🔔 Напоминания`: active `pending`/`due` reminders, including queued,
  retrying, failed, and `delivery_state=delivered` reminders that are still
  open;
- `✅ Задачи`: open tasks for today, overdue, or upcoming, with completion;
- `📅 Календарь`: local canonical events for today, tomorrow, or upcoming.

Views use bounded six-item pages with fixed, allowlisted navigation callbacks.
Titles are truncated only for display and HTML-escaped. Notes, UUIDs, audit
IDs, receipts, correlation internals, and provider diagnostics are not shown.

Reminder delivery and user completion remain separate. A successfully
delivered reminder is displayed as `доставлено · ждёт выполнения` until the
admin explicitly completes or cancels it. The notification sent by A3 keeps
its existing delete-only markup: A6 does not attach a pre-send version token
to an in-flight delivery message. Fresh list views issue tokens after reading
the current canonical version.

Tasks keep date-only semantics and never render a fake midnight. Timed tasks
show their stored local wall-clock time. Events are read from local Planning
SQLite only; all-day events use exclusive end-date semantics and sort before
timed events on the same local day. `local_only` events are labelled
`локально · не синхронизировано`. No provider call or external-calendar sync
is implied.

## Callback-token architecture

Mutation callbacks have the form:

```text
planning:a:<opaque-token>
```

The token is 32 random bytes encoded with `secrets.token_urlsafe`, and the
callback is checked to remain within Telegram's 64-byte limit. SQLite stores
only the SHA-256 digest in migration `004_telegram_action_tokens.sql` plus:

- one closed action (`reminder_complete`, `reminder_cancel`,
  `reminder_retry`, or `task_complete`);
- domain and canonical object ID;
- expected object version;
- Telegram user ID and optional chat ID binding;
- creation, expiry, and consumption timestamps.

The raw token is never stored or logged. The default TTL is 15 minutes and is
bounded to a sane 60-minute maximum range. Expired token rows are cleaned in
bounded batches.

Token lookup, expiry/binding checks, expected-version domain mutation, audit,
and token consumption run in one `BEGIN IMMEDIATE` transaction. A second
click therefore sees a consumed token and cannot repeat the mutation. Unknown,
tampered, expired, consumed, wrong-user/chat, wrong-action, stale-version,
and missing-object cases fail without mutation and receive bounded Russian
feedback. If a domain mutation rolls back, token consumption rolls back too.

The local per-user callback limiter is modest abuse protection; it is not a
distributed rate-limit claim. Single-use tokens, admin authorization, chat
binding, and repository expected-version checks remain the primary controls.

## Reminder actions

`✅ Выполнить` and `❌ Отменить` use the existing Planning repository/audit
semantics and increment the canonical version. `🔁 Повторить` is available
only for terminal delivery failure and calls the A3 manual-retry primitive.
It reuses the deduplicated delivery job, starts a fresh retry cycle, preserves
historical delivery attempts, leaves the reminder active, and never marks it
complete.

## Deferred product decisions and non-goals

There are no Telegram task/event creation flows in A6. There are no user-facing
snooze presets or buttons; the snooze product decision is still pending.
TickTick, calendar providers, Home Assistant actions, Control Center,
deployment, production flags, and live Telegram calls are outside this PR.

`SNOOZE_PRESET_PRODUCT_DECISION_PENDING`

`TELEGRAM_TASK_EVENT_CREATION_PRODUCT_DECISION_DEFERRED`

A7 has not started.
