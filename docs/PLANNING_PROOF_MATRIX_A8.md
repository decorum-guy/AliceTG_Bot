# Planning A8 / Plan 1 proof matrix

The execution plan references these exact eight Plan 1 proof scenarios. The
status labels below separate synthetic/component automation from live external
acceptance; no live Telegram, Alice, or Home Assistant pass is fabricated.

| # | Exact Plan 1 scenario | Automated component result | Live acceptance |
|---|---|---|---|
| 1 | Yandex event creates one reminder exactly once and gets Russian response through `yandex_intent_response` | `AUTOMATED_COMPONENT_PROOF_PASSED` by the A5 Alice ingress/parser/idempotency contract tests | `LIVE_ACCEPTANCE_DEFERRED` — requires live Yandex/Home Assistant behavior |
| 2 | Duplicate delivery returns stored response/no second object/job | `AUTOMATED_COMPONENT_PROOF_PASSED` by the A4/A5 idempotency and durable object/outbox tests | `LIVE_ACCEPTANCE_DEFERRED` |
| 3 | Killing bot after lease before Telegram recovers after lease expiry | `AUTOMATED_COMPONENT_PROOF_PASSED` by the A3 lease-reclaim/crash-window tests | `LIVE_ACCEPTANCE_DEFERRED` — real process/Telegram transport not exercised here |
| 4 | Simulated Telegram outage exercises retry + terminal failure without losing reminder | `AUTOMATED_COMPONENT_PROOF_PASSED` by the A3 retry/terminal-delivery tests with fake transports | `LIVE_ACCEPTANCE_DEFERRED` |
| 5 | Telegram list/cancel/complete/snooze reminders and list task/event queries with authorization | `AUTOMATED_COMPONENT_PROOF_PASSED` for A6 list/action/authorization behavior and the existing deferred snooze boundary; no snooze product flow was added | `LIVE_ACCEPTANCE_DEFERRED` — manual Telegram interaction is intentionally deferred |
| 6 | Parser Russian forms, DST/date rollover, ambiguity stops | `AUTOMATED_COMPONENT_PROOF_PASSED` by A5 parser, date, DST, and ambiguity tests | `LIVE_ACCEPTANCE_DEFERRED` — live voice/Alice acceptance |
| 7 | SQLite backup/restore resumes next due job without duplicate | `AUTOMATED_COMPONENT_PROOF_PASSED` by `tests/planning/test_backup_a8.py`: online snapshot, isolated verifier, fake scheduler, one send, reopen, zero second sends | `LIVE_ACCEPTANCE_DEFERRED` — production recovery drill follows merge/review |
| 8 | API rejects unknown/fuzzed service/entity/command/path | `AUTOMATED_COMPONENT_PROOF_PASSED` by A4 route/auth/path allowlist and malformed-request tests | `LIVE_ACCEPTANCE_DEFERRED` — live private-tunnel acceptance is outside this offline phase |

## A8-specific automated evidence

The new focused tests cover:

- native online backup while a separate SQLite connection holds an uncommitted
  write, proving a valid transactional snapshot rather than partial rows;
- encrypted round-trip, wrong key, authentication/tag corruption, modified
  manifest, hash mismatch, truncated/corrupt database, future schema, and FK
  failure;
- manifest aggregate counts, content-free metadata, filename privacy,
  permissions, atomic failure preservation, bounded retention, and unrelated
  file preservation;
- isolated capability invalidation and due-job recovery through a fake
  Telegram transport with exactly one intended delivery across restart;
- scheduler disabled/unknown/healthy/stale states, queued/leased age,
  terminal failure, delivered-but-open exclusion from stuck delivery,
  backup/restore incident states, content-free status, and transition log
  suppression;
- configuration defaults, production destination validation, and dedicated key
  separation.

The full A0–A7 regression suite remains required before merge. Live external
acceptance is explicitly outstanding and is not part of the A8 branch claim.

```text
SNOOZE_PRESET_PRODUCT_DECISION_PENDING
TELEGRAM_TASK_EVENT_CREATION_PRODUCT_DECISION_DEFERRED
```
