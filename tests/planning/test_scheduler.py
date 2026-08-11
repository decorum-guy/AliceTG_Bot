from __future__ import annotations

import asyncio
import shutil
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.planning import (
    AuditWriter,
    MutationContext,
    PlanningDatabase,
    PlanningRepository,
    PlanningValidationError,
    PlanningVersionConflictError,
)
from app.planning.delivery import DeliveryResult
from app.planning.legacy_import import LegacyReminderImporter
from app.planning.models import REMINDER_DELIVERY_JOB_TYPE
from app.planning.scheduler import (
    DurableReminderScheduler,
    MAX_DELIVERY_ATTEMPTS,
    RETRY_DELAYS_SECONDS,
    validate_scheduler_modes,
)
from app.services.reminder_store import ReminderSettings


class FakeClock:
    def __init__(self, value: str = "2026-08-11T10:00:00.000000Z") -> None:
        self.value = datetime.fromisoformat(value[:-1] + "+00:00")

    def __call__(self) -> str:
        return self.value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")

    def advance(self, **kwargs: int) -> str:
        self.value += timedelta(**kwargs)
        return self()

    def set(self, value: str) -> None:
        self.value = datetime.fromisoformat(value[:-1] + "+00:00")


class FakeTransport:
    def __init__(self, channel: str, outcomes: list[DeliveryResult | BaseException] | None = None) -> None:
        self.channel = channel
        self.outcomes = list(outcomes or [DeliveryResult.success(provider_receipt=f"{channel}:receipt")])
        self.calls: list[dict[str, Any]] = []

    async def send(self, **kwargs: Any) -> DeliveryResult:
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class SimulatedCrash(Exception):
    pass


CONTEXT = MutationContext(
    audience="operator",
    actor_id="fixture-operator",
    actor_type="operator",
    surface="telegram",
)
MANUAL_CONTEXT = MutationContext(
    audience="operator",
    actor_id="fixture-admin",
    actor_type="operator",
    surface="operator",
)
FIXTURE = Path(__file__).parents[1] / "fixtures" / "planning_legacy" / "reminders_valid.json"


class DurableReminderSchedulerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "planning.sqlite3"
        self.clock = FakeClock()
        self.database = PlanningDatabase(self.db_path)
        self.repository = PlanningRepository(self.database, now_fn=self.clock)
        self.settings = ReminderSettings()
        self.telegram = FakeTransport("telegram")
        self.mobile = FakeTransport("iphone")

    def tearDown(self) -> None:
        self.database.close()
        self.temp_dir.cleanup()

    def reminder(self, *, due_at: str | None = None, durable: bool = True):
        due_at = due_at or self.clock()
        return self.repository.create_reminder(
            title="Synthetic A3 reminder",
            due_at_utc=due_at,
            timezone="UTC",
            context=CONTEXT,
            outbox_job_type=REMINDER_DELIVERY_JOB_TYPE if durable else None,
            outbox_payload={"chat_id": -100000000001} if durable else None,
        )

    def scheduler(
        self,
        *,
        telegram: FakeTransport | None = None,
        mobile: FakeTransport | None = None,
        settings_provider: Any | None = None,
        **kwargs: Any,
    ):
        async def default_settings_provider() -> ReminderSettings:
            return self.settings

        return DurableReminderScheduler(
            self.database,
            telegram_transport=telegram or self.telegram,
            mobile_transport=mobile,
            default_chat_id=-100000000099,
            settings_provider=settings_provider or default_settings_provider,
            now_fn=self.clock,
            jitter_fn=lambda _base: 0,
            **kwargs,
        )

    async def run_once(self, scheduler: DurableReminderScheduler, value: str | None = None):
        return await scheduler.run_once(value or self.clock())

    async def test_future_reminder_has_no_premature_send(self) -> None:
        reminder = self.reminder(due_at="2026-08-11T10:01:00.000000Z")
        scheduler = self.scheduler()
        await self.run_once(scheduler)
        stored = self.repository.get_reminder(reminder.id)
        self.assertEqual((stored.status, stored.delivery_state), ("pending", "not_due"))
        self.assertEqual(len(self.telegram.calls), 0)

    async def test_due_reminder_becomes_due_and_queued_before_worker_claim(self) -> None:
        reminder = self.reminder(due_at="2026-08-11T10:01:00.000000Z")
        scheduler = self.scheduler()
        self.clock.advance(minutes=1)
        self.assertEqual(scheduler.reconcile_due(), 1)
        stored = self.repository.get_reminder(reminder.id)
        self.assertEqual((stored.status, stored.delivery_state), ("due", "queued"))
        self.assertEqual(len(self.telegram.calls), 0)

    async def test_reminder_creation_and_outbox_are_atomic(self) -> None:
        reminder = self.reminder()
        self.assertEqual(self.database.connection.execute("SELECT COUNT(*) FROM reminders").fetchone()[0], 1)
        self.assertEqual(
            self.database.connection.execute(
                "SELECT COUNT(*) FROM outbox WHERE reminder_id = ?", (reminder.id,)
            ).fetchone()[0],
            1,
        )

        failing_repository = PlanningRepository(
            self.database,
            audit=AuditWriter(self.database, fail=True, now_fn=self.clock),
            now_fn=self.clock,
        )
        with self.assertRaises(RuntimeError):
            failing_repository.create_reminder(
                title="Atomic failure",
                due_at_utc=self.clock(),
                timezone="UTC",
                context=CONTEXT,
                outbox_job_type=REMINDER_DELIVERY_JOB_TYPE,
                outbox_payload={"chat_id": -100000000002},
            )
        self.assertEqual(
            self.database.connection.execute("SELECT COUNT(*) FROM reminders WHERE title = 'Atomic failure'").fetchone()[0],
            0,
        )

    async def test_imported_pending_reminder_gets_idempotent_durable_work(self) -> None:
        source = Path(self.temp_dir.name) / "reminders.json"
        shutil.copyfile(FIXTURE, source)
        LegacyReminderImporter(self.database).import_file(source)
        scheduler = self.scheduler()
        self.assertEqual(scheduler.reconcile_due(), 1)
        self.assertEqual(scheduler.reconcile_due(), 0)
        self.assertEqual(
            self.database.connection.execute("SELECT COUNT(*) FROM outbox").fetchone()[0],
            1,
        )
        fired_id = self.database.connection.execute(
            "SELECT planning_id FROM legacy_reminder_mappings WHERE legacy_status = 'fired'"
        ).fetchone()[0]
        self.assertEqual(
            self.database.connection.execute(
                "SELECT COUNT(*) FROM outbox WHERE reminder_id = ?", (fired_id,)
            ).fetchone()[0],
            0,
        )

    async def test_restart_reconciliation_does_not_duplicate_jobs(self) -> None:
        reminder = self.reminder(due_at="2026-08-12T10:00:00.000000Z")
        self.database.close()
        self.database = PlanningDatabase(self.db_path)
        self.repository = PlanningRepository(self.database, now_fn=self.clock)
        scheduler = self.scheduler()
        self.assertEqual(scheduler.reconcile_due(), 0)
        self.assertEqual(
            self.database.connection.execute(
                "SELECT COUNT(*) FROM outbox WHERE reminder_id = ?", (reminder.id,)
            ).fetchone()[0],
            1,
        )

    async def test_one_worker_leases_one_job(self) -> None:
        reminder = self.reminder()
        scheduler = self.scheduler()
        job = self.repository.claim_outbox(
            job_type=REMINDER_DELIVERY_JOB_TYPE,
            lease_owner="worker-a",
            now=self.clock(),
            lease_expires_at="2026-08-11T10:01:00.000000Z",
        )
        self.assertIsNotNone(job)
        self.assertEqual(job.reminder_id, reminder.id)
        self.assertEqual(job.lease_owner, "worker-a")
        self.assertIsNone(
            self.repository.claim_outbox(
                job_type=REMINDER_DELIVERY_JOB_TYPE,
                lease_owner=scheduler.worker_id,
                now=self.clock(),
                lease_expires_at="2026-08-11T10:01:00.000000Z",
            )
        )

    async def test_two_workers_do_not_claim_same_live_lease(self) -> None:
        self.reminder()
        other_database = PlanningDatabase(self.db_path)
        other_repository = PlanningRepository(other_database, now_fn=self.clock)
        try:
            def claim(repository: PlanningRepository, owner: str):
                return repository.claim_outbox(
                    job_type=REMINDER_DELIVERY_JOB_TYPE,
                    lease_owner=owner,
                    now=self.clock(),
                    lease_expires_at="2026-08-11T10:01:00.000000Z",
                )

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(
                    executor.map(
                        lambda pair: claim(*pair),
                        ((self.repository, "worker-a"), (other_repository, "worker-b")),
                    )
                )
            self.assertEqual(sum(result is not None for result in results), 1)
        finally:
            other_database.close()

    async def test_expired_lease_is_reclaimed(self) -> None:
        self.reminder()
        first = self.repository.claim_outbox(
            job_type=REMINDER_DELIVERY_JOB_TYPE,
            lease_owner="worker-a",
            now=self.clock(),
            lease_expires_at="2026-08-11T10:01:00.000000Z",
        )
        self.assertIsNotNone(first)
        reclaimed = self.repository.claim_outbox(
            job_type=REMINDER_DELIVERY_JOB_TYPE,
            lease_owner="worker-b",
            now="2026-08-11T10:01:00.000000Z",
            lease_expires_at="2026-08-11T10:02:00.000000Z",
        )
        self.assertIsNotNone(reclaimed)
        self.assertNotEqual(first.lease_token, reclaimed.lease_token)
        self.assertEqual(reclaimed.lease_owner, "worker-b")

    async def test_successful_telegram_delivery_is_delivered_but_not_completed(self) -> None:
        reminder = self.reminder()
        await self.run_once(self.scheduler())
        stored = self.repository.get_reminder(reminder.id)
        self.assertEqual((stored.status, stored.delivery_state), ("due", "delivered"))
        self.assertIsNone(stored.completed_at)
        self.assertEqual(self.telegram.calls[0]["chat_id"], -100000000001)
        attempt = self.database.connection.execute(
            "SELECT status, channel, provider_receipt, correlation_id FROM delivery_attempts"
        ).fetchone()
        self.assertEqual((attempt["status"], attempt["channel"], attempt["provider_receipt"]), ("succeeded", "telegram", "telegram:receipt"))
        self.assertTrue(attempt["correlation_id"])

    async def test_telegram_failure_and_iphone_success_remain_overall_not_delivered(self) -> None:
        self.settings.notify_iphone_enabled = True
        telegram = FakeTransport("telegram", [DeliveryResult.retryable("telegram_timeout", diagnostic="timeout")])
        mobile = FakeTransport("iphone", [DeliveryResult.success(provider_receipt="ha-mobile")])
        reminder = self.reminder()
        await self.run_once(self.scheduler(telegram=telegram, mobile=mobile))
        stored = self.repository.get_reminder(reminder.id)
        self.assertEqual((stored.status, stored.delivery_state), ("due", "retrying"))
        self.assertEqual(len(mobile.calls), 1)
        self.assertEqual(
            [tuple(row) for row in self.database.connection.execute(
                "SELECT channel, status FROM delivery_attempts ORDER BY channel"
            ).fetchall()],
            [("iphone", "succeeded"), ("telegram", "failed")],
        )

    async def test_retryable_telegram_timeout_persists_retrying_state(self) -> None:
        reminder = self.reminder()
        telegram = FakeTransport("telegram", [DeliveryResult.retryable("telegram_timeout", diagnostic="timeout")])
        await self.run_once(self.scheduler(telegram=telegram))
        stored = self.repository.get_reminder(reminder.id)
        self.assertEqual(stored.delivery_state, "retrying")
        self.assertEqual(stored.next_attempt_at, "2026-08-11T10:00:30.000000Z")

    async def test_telegram_429_uses_provider_retry_after_without_losing_policy(self) -> None:
        reminder = self.reminder()
        telegram = FakeTransport(
            "telegram",
            [DeliveryResult.retryable("telegram_rate_limited", retry_after_seconds=90)],
        )
        await self.run_once(self.scheduler(telegram=telegram))
        self.assertEqual(self.repository.get_reminder(reminder.id).next_attempt_at, "2026-08-11T10:01:30.000000Z")

    async def test_permanent_telegram_failure_is_terminal_and_keeps_reminder_due(self) -> None:
        reminder = self.reminder()
        telegram = FakeTransport("telegram", [DeliveryResult.permanent("telegram_forbidden")])
        await self.run_once(self.scheduler(telegram=telegram))
        stored = self.repository.get_reminder(reminder.id)
        self.assertEqual((stored.status, stored.delivery_state), ("due", "failed"))
        self.assertEqual(stored.final_failure_at, self.clock())
        self.assertIsNone(stored.completed_at)
        self.assertEqual(self.database.connection.execute("SELECT status FROM outbox").fetchone()[0], "failed")

    async def test_retry_schedule_sequence_is_exact_without_sleep(self) -> None:
        reminder = self.reminder()
        telegram = FakeTransport(
            "telegram",
            [DeliveryResult.retryable("telegram_timeout") for _ in range(MAX_DELIVERY_ATTEMPTS)],
        )
        observed: list[int] = []
        scheduler = self.scheduler(telegram=telegram)
        await self.run_once(scheduler)
        for expected_delay in RETRY_DELAYS_SECONDS:
            stored = self.repository.get_reminder(reminder.id)
            observed.append(int((_as_dt(stored.next_attempt_at) - _as_dt(self.clock())).total_seconds()))
            self.clock.set(stored.next_attempt_at)
            await self.run_once(scheduler)
            if self.repository.get_reminder(reminder.id).delivery_state == "failed":
                break
        self.assertEqual(observed, list(RETRY_DELAYS_SECONDS))
        self.assertEqual(len(telegram.calls), MAX_DELIVERY_ATTEMPTS)

    async def test_jitter_is_bounded_and_injected_deterministically(self) -> None:
        reminder = self.reminder()
        telegram = FakeTransport("telegram", [DeliveryResult.retryable("telegram_timeout")])
        scheduler = DurableReminderScheduler(
            self.database,
            telegram_transport=telegram,
            mobile_transport=None,
            default_chat_id=-100000000099,
            now_fn=self.clock,
            jitter_fn=lambda _base: 4,
            jitter_bound_seconds=5,
        )
        await self.run_once(scheduler)
        self.assertEqual(self.repository.get_reminder(reminder.id).next_attempt_at, "2026-08-11T10:00:34.000000Z")

    async def test_eight_attempt_cap_and_24_hour_window(self) -> None:
        reminder = self.reminder()
        telegram = FakeTransport("telegram", [DeliveryResult.retryable("telegram_timeout") for _ in range(8)])
        scheduler = self.scheduler(telegram=telegram)
        await self.run_once(scheduler)
        for _ in range(7):
            next_attempt = self.repository.get_reminder(reminder.id).next_attempt_at
            self.clock.set(next_attempt)
            await self.run_once(scheduler)
        stored = self.repository.get_reminder(reminder.id)
        self.assertEqual(stored.delivery_state, "failed")
        self.assertEqual(len(telegram.calls), 8)
        window_start = self.database.connection.execute(
            "SELECT attempt_window_started_at FROM outbox"
        ).fetchone()[0]
        self.assertLessEqual(
            _as_dt(stored.final_failure_at) - _as_dt(window_start),
            timedelta(days=1),
        )

    async def test_terminal_failure_has_final_failure_at_and_audit_incident(self) -> None:
        reminder = self.reminder()
        await self.run_once(self.scheduler(telegram=FakeTransport("telegram", [DeliveryResult.permanent("telegram_chat_not_found")])))
        self.assertIsNotNone(self.repository.get_reminder(reminder.id).final_failure_at)
        actions = [row[0] for row in self.database.connection.execute("SELECT action FROM audit_events WHERE object_id = ?", (reminder.id,))]
        self.assertIn("terminal_failure", actions)

    async def test_manual_retry_requeues_safely(self) -> None:
        reminder = self.reminder()
        await self.run_once(self.scheduler(telegram=FakeTransport("telegram", [DeliveryResult.permanent("telegram_forbidden")])))
        failed = self.repository.get_reminder(reminder.id)
        retried = self.repository.manual_retry_reminder(
            reminder.id,
            expected_version=failed.version,
            context=MANUAL_CONTEXT,
            now=self.clock(),
        )
        self.assertEqual((retried.status, retried.delivery_state), ("due", "queued"))
        self.assertEqual(self.database.connection.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 1)
        self.assertEqual(
            tuple(self.database.connection.execute("SELECT status, attempt_count FROM outbox").fetchone()),
            ("queued", 0),
        )

    async def test_manual_retry_requires_trusted_context_and_version(self) -> None:
        reminder = self.reminder()
        await self.run_once(self.scheduler(telegram=FakeTransport("telegram", [DeliveryResult.permanent("telegram_forbidden")])))
        failed = self.repository.get_reminder(reminder.id)
        with self.assertRaises(PlanningValidationError):
            self.repository.manual_retry_reminder(
                reminder.id,
                expected_version=failed.version,
                context=CONTEXT,
                now=self.clock(),
            )
        with self.assertRaises(PlanningVersionConflictError):
            self.repository.manual_retry_reminder(
                reminder.id,
                expected_version=failed.version - 1,
                context=MANUAL_CONTEXT,
                now=self.clock(),
            )

    async def test_manual_retry_preserves_historical_attempts(self) -> None:
        reminder = self.reminder()
        outcomes = [DeliveryResult.retryable("telegram_timeout") for _ in range(8)] + [DeliveryResult.success()]
        telegram = FakeTransport("telegram", outcomes)
        scheduler = self.scheduler(telegram=telegram)
        await self.run_once(scheduler)
        for _ in range(7):
            self.clock.set(self.repository.get_reminder(reminder.id).next_attempt_at)
            await self.run_once(scheduler)
        failed = self.repository.get_reminder(reminder.id)
        self.assertEqual(self.database.connection.execute("SELECT COUNT(*) FROM delivery_attempts").fetchone()[0], 8)
        historical_cycle = self.database.connection.execute(
            "SELECT DISTINCT delivery_cycle_id FROM delivery_attempts WHERE channel = 'telegram'"
        ).fetchone()[0]
        self.repository.manual_retry_reminder(reminder.id, expected_version=failed.version, context=MANUAL_CONTEXT, now=self.clock())
        self.assertIsNone(self.database.connection.execute("SELECT delivery_cycle_id FROM outbox").fetchone()[0])
        await self.run_once(scheduler)
        self.assertEqual(self.database.connection.execute("SELECT COUNT(*) FROM delivery_attempts").fetchone()[0], 9)
        numbers = [row[0] for row in self.database.connection.execute("SELECT attempt_number FROM delivery_attempts ORDER BY attempt_number")]
        self.assertEqual(numbers, list(range(1, 10)))
        new_cycle = self.database.connection.execute(
            "SELECT delivery_cycle_id FROM outbox"
        ).fetchone()[0]
        self.assertNotEqual(new_cycle, historical_cycle)
        self.assertEqual(
            self.database.connection.execute(
                "SELECT COUNT(*) FROM delivery_attempts WHERE delivery_cycle_id = ?", (new_cycle,)
            ).fetchone()[0],
            1,
        )

    async def test_cancellation_before_send_suppresses_delivery(self) -> None:
        reminder = self.reminder()
        self.repository.cancel_reminder(reminder.id, expected_version=reminder.version, context=MANUAL_CONTEXT)
        await self.run_once(self.scheduler())
        self.assertEqual(len(self.telegram.calls), 0)
        self.assertEqual(self.repository.get_reminder(reminder.id).status, "cancelled")
        self.assertEqual(self.database.connection.execute("SELECT status FROM outbox").fetchone()[0], "cancelled")

    async def test_completion_before_send_suppresses_delivery(self) -> None:
        reminder = self.reminder()
        self.repository.complete_reminder(reminder.id, expected_version=reminder.version, context=MANUAL_CONTEXT)
        await self.run_once(self.scheduler())
        self.assertEqual(len(self.telegram.calls), 0)
        self.assertEqual(self.repository.get_reminder(reminder.id).status, "completed")

    async def test_cancellation_during_retry_lifecycle_suppresses_later_retry(self) -> None:
        reminder = self.reminder()
        await self.run_once(self.scheduler(telegram=FakeTransport("telegram", [DeliveryResult.retryable("telegram_timeout")])))
        retrying = self.repository.get_reminder(reminder.id)
        self.repository.cancel_reminder(reminder.id, expected_version=retrying.version, context=MANUAL_CONTEXT)
        self.clock.set(retrying.next_attempt_at)
        await self.run_once(self.scheduler())
        self.assertEqual(len(self.telegram.calls), 0)
        self.assertEqual(self.repository.get_reminder(reminder.id).status, "cancelled")

    async def test_overdue_startup_processes_work_without_second_restart(self) -> None:
        source = Path(self.temp_dir.name) / "reminders.json"
        shutil.copyfile(FIXTURE, source)
        LegacyReminderImporter(self.database).import_file(source)
        self.clock.set("2026-08-12T10:00:00.000000Z")
        scheduler = self.scheduler()
        await self.run_once(scheduler)
        self.assertEqual(len(self.telegram.calls), 1)
        pending_id = self.database.connection.execute(
            "SELECT planning_id FROM legacy_reminder_mappings WHERE legacy_id = 'a1b2c3d4e5f6'"
        ).fetchone()[0]
        self.assertEqual(self.repository.get_reminder(pending_id).delivery_state, "delivered")

    async def test_clock_jump_from_future_to_due_is_reconciled(self) -> None:
        reminder = self.reminder(due_at="2026-08-11T11:00:00.000000Z")
        scheduler = self.scheduler()
        await self.run_once(scheduler)
        self.assertEqual(len(self.telegram.calls), 0)
        self.clock.set("2026-08-11T11:00:00.000000Z")
        await self.run_once(scheduler)
        self.assertEqual(len(self.telegram.calls), 1)
        self.assertEqual(self.repository.get_reminder(reminder.id).status, "due")

    async def test_crash_before_provider_send_reclaims_after_lease_expiry(self) -> None:
        reminder = self.reminder()

        async def crash_before_send(_reminder, _channel):
            raise SimulatedCrash("before provider")

        crashing = self.scheduler(before_provider_send=crash_before_send, lease_seconds=10)
        with self.assertRaises(SimulatedCrash):
            await self.run_once(crashing)
        self.assertEqual(len(self.telegram.calls), 0)
        self.assertEqual(self.database.connection.execute("SELECT status FROM delivery_attempts").fetchone()[0], "started")
        self.clock.advance(seconds=11)
        recovered = self.scheduler(lease_seconds=10)
        await self.run_once(recovered)
        self.assertEqual(len(self.telegram.calls), 1)
        self.assertEqual(self.repository.get_reminder(reminder.id).delivery_state, "delivered")

    async def test_crash_before_telegram_attempt_does_not_consume_retry_ordinal(self) -> None:
        reminder = self.reminder()

        async def crash_before_attempt(_reminder, _channel):
            raise SimulatedCrash("before attempt persistence")

        crashing = self.scheduler(before_attempt_persist=crash_before_attempt, lease_seconds=10)
        with self.assertRaises(SimulatedCrash):
            await self.run_once(crashing)
        self.assertEqual(self.database.connection.execute("SELECT COUNT(*) FROM delivery_attempts").fetchone()[0], 0)
        self.clock.advance(seconds=11)
        telegram = FakeTransport("telegram", [DeliveryResult.retryable("telegram_timeout")])
        await self.run_once(self.scheduler(telegram=telegram, lease_seconds=10))
        self.assertEqual(self.repository.get_reminder(reminder.id).next_attempt_at, "2026-08-11T10:00:41.000000Z")
        self.assertEqual(
            self.database.connection.execute(
                "SELECT attempt_number FROM delivery_attempts WHERE channel = 'telegram'"
            ).fetchone()[0],
            1,
        )

    async def test_settings_failure_before_telegram_attempt_does_not_consume_budget(self) -> None:
        reminder = self.reminder()
        settings_calls = 0

        async def failing_settings() -> ReminderSettings:
            nonlocal settings_calls
            settings_calls += 1
            if settings_calls == 1:
                raise SimulatedCrash("settings unavailable")
            return self.settings

        with self.assertRaises(SimulatedCrash):
            await self.run_once(self.scheduler(settings_provider=failing_settings, lease_seconds=10))
        self.assertEqual(self.database.connection.execute("SELECT COUNT(*) FROM delivery_attempts").fetchone()[0], 0)
        self.clock.advance(seconds=11)
        telegram = FakeTransport("telegram", [DeliveryResult.retryable("telegram_timeout")])
        await self.run_once(self.scheduler(telegram=telegram, settings_provider=failing_settings, lease_seconds=10))
        self.assertEqual(self.repository.get_reminder(reminder.id).next_attempt_at, "2026-08-11T10:00:41.000000Z")

    async def test_lease_reclaim_without_telegram_attempt_keeps_first_retry_delay(self) -> None:
        reminder = self.reminder()
        self.repository.claim_outbox(
            job_type=REMINDER_DELIVERY_JOB_TYPE,
            lease_owner="crashed-worker",
            now=self.clock(),
            lease_expires_at="2026-08-11T10:00:10.000000Z",
        )
        self.clock.set("2026-08-11T10:00:11.000000Z")
        telegram = FakeTransport("telegram", [DeliveryResult.retryable("telegram_timeout")])
        await self.run_once(self.scheduler(telegram=telegram, lease_seconds=10))
        self.assertEqual(self.repository.get_reminder(reminder.id).next_attempt_at, "2026-08-11T10:00:41.000000Z")
        self.assertEqual(self.database.connection.execute("SELECT COUNT(*) FROM delivery_attempts").fetchone()[0], 1)

    async def test_persisted_telegram_attempt_consumes_retry_ordinal(self) -> None:
        reminder = self.reminder()

        async def crash_after_attempt(_reminder, _channel):
            raise SimulatedCrash("after attempt persistence")

        crashing = self.scheduler(before_provider_send=crash_after_attempt, lease_seconds=10)
        with self.assertRaises(SimulatedCrash):
            await self.run_once(crashing)
        self.assertEqual(
            self.database.connection.execute(
                "SELECT status, attempt_number FROM delivery_attempts WHERE channel = 'telegram'"
            ).fetchone()[0:2],
            ("started", 1),
        )
        self.clock.advance(seconds=11)
        telegram = FakeTransport("telegram", [DeliveryResult.retryable("telegram_timeout")])
        await self.run_once(self.scheduler(telegram=telegram, lease_seconds=10))
        self.assertEqual(self.repository.get_reminder(reminder.id).next_attempt_at, "2026-08-11T10:02:11.000000Z")
        self.assertEqual(self.database.connection.execute("SELECT COUNT(*) FROM delivery_attempts").fetchone()[0], 2)

    async def test_failure_during_provider_send_is_audited_and_retryable(self) -> None:
        reminder = self.reminder()
        telegram = FakeTransport("telegram", [RuntimeError("network failure")])
        await self.run_once(self.scheduler(telegram=telegram))
        attempt = self.database.connection.execute("SELECT status, error_code FROM delivery_attempts").fetchone()
        self.assertEqual((attempt["status"], attempt["error_code"]), ("failed", "transport_exception"))
        self.assertEqual(self.repository.get_reminder(reminder.id).delivery_state, "retrying")

    async def test_optional_success_is_not_repeated_while_telegram_retries(self) -> None:
        self.settings.notify_iphone_enabled = True
        reminder = self.reminder()
        telegram = FakeTransport(
            "telegram",
            [DeliveryResult.retryable("telegram_timeout"), DeliveryResult.success()],
        )
        mobile = FakeTransport("iphone", [DeliveryResult.success(provider_receipt="ha-mobile")])
        scheduler = self.scheduler(telegram=telegram, mobile=mobile)
        await self.run_once(scheduler)
        retry_at = self.repository.get_reminder(reminder.id).next_attempt_at
        self.assertEqual((len(telegram.calls), len(mobile.calls)), (1, 1))
        self.clock.set(retry_at)
        await self.run_once(scheduler)
        self.assertEqual((len(telegram.calls), len(mobile.calls)), (2, 1))
        self.assertEqual(
            self.database.connection.execute(
                "SELECT COUNT(*) FROM delivery_attempts WHERE reminder_id = ? AND channel = 'iphone' AND status = 'succeeded'",
                (reminder.id,),
            ).fetchone()[0],
            1,
        )
        stored = self.repository.get_reminder(reminder.id)
        self.assertEqual((stored.status, stored.delivery_state, stored.completed_at), ("due", "delivered", None))

    async def test_crash_after_remote_success_before_local_commit_demonstrates_at_least_once(self) -> None:
        reminder = self.reminder()

        async def crash_after_send(_reminder, _channel):
            raise SimulatedCrash("after remote success")

        crashing = self.scheduler(after_provider_send=crash_after_send, lease_seconds=10)
        with self.assertRaises(SimulatedCrash):
            await self.run_once(crashing)
        self.assertEqual(len(self.telegram.calls), 1)
        self.clock.advance(seconds=11)
        recovered = self.scheduler(lease_seconds=10)
        await self.run_once(recovered)
        self.assertEqual(len(self.telegram.calls), 2)
        self.assertEqual(self.repository.get_reminder(reminder.id).delivery_state, "delivered")
        self.assertEqual(
            self.database.connection.execute("SELECT COUNT(*) FROM delivery_attempts").fetchone()[0],
            2,
        )

    async def test_close_reopen_persists_retry_state(self) -> None:
        reminder = self.reminder()
        await self.run_once(self.scheduler(telegram=FakeTransport("telegram", [DeliveryResult.retryable("telegram_timeout")])))
        retry_at = self.repository.get_reminder(reminder.id).next_attempt_at
        self.database.close()
        self.database = PlanningDatabase(self.db_path)
        self.repository = PlanningRepository(self.database, now_fn=self.clock)
        self.clock.set(retry_at)
        await self.run_once(self.scheduler())
        self.assertEqual(self.repository.get_reminder(reminder.id).delivery_state, "delivered")

    async def test_no_duplicate_logical_outbox_job(self) -> None:
        reminder = self.reminder(durable=False)
        with self.database.transaction():
            first = self.repository.ensure_reminder_outbox(
                reminder_id=reminder.id,
                job_type=REMINDER_DELIVERY_JOB_TYPE,
                payload={"chat_id": -100000000001},
                available_at=reminder.due_at_utc,
                dedupe_key=f"planning.reminder:{reminder.id}",
            )
            second = self.repository.ensure_reminder_outbox(
                reminder_id=reminder.id,
                job_type=REMINDER_DELIVERY_JOB_TYPE,
                payload={"chat_id": -100000000001},
                available_at=reminder.due_at_utc,
                dedupe_key=f"planning.reminder:{reminder.id}",
            )
        self.assertEqual(first.id, second.id)
        self.assertEqual(self.database.connection.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 1)

    async def test_old_scheduler_only_when_durable_disabled_and_invalid_modes_fail_closed(self) -> None:
        validate_scheduler_modes(
            durable_scheduler_enabled=False,
            legacy_scheduler_enabled=True,
            reminder_cutover_enabled=False,
        )
        with self.assertRaises(RuntimeError):
            validate_scheduler_modes(
                durable_scheduler_enabled=True,
                legacy_scheduler_enabled=True,
                reminder_cutover_enabled=True,
            )
        with self.assertRaises(RuntimeError):
            validate_scheduler_modes(
                durable_scheduler_enabled=True,
                legacy_scheduler_enabled=False,
                reminder_cutover_enabled=False,
            )

    async def test_a2_fired_records_are_not_redelivered(self) -> None:
        source = Path(self.temp_dir.name) / "reminders.json"
        shutil.copyfile(FIXTURE, source)
        LegacyReminderImporter(self.database).import_file(source)
        self.clock.set("2026-08-12T10:00:00.000000Z")
        scheduler = self.scheduler()
        await self.run_once(scheduler)
        fired_id = self.database.connection.execute(
            "SELECT planning_id FROM legacy_reminder_mappings WHERE legacy_status = 'fired'"
        ).fetchone()[0]
        self.assertEqual(
            self.database.connection.execute(
                "SELECT COUNT(*) FROM delivery_attempts WHERE reminder_id = ?", (fired_id,)
            ).fetchone()[0],
            0,
        )
        self.assertEqual(self.repository.get_reminder(fired_id).status, "completed")

    async def test_native_delivered_reminder_remains_active_and_job_is_terminal(self) -> None:
        reminder = self.reminder()
        delivered = self.repository.update_reminder(
            reminder.id,
            expected_version=reminder.version,
            context=MANUAL_CONTEXT,
            delivery_state="delivered",
        )
        await self.run_once(self.scheduler())
        current = self.repository.get_reminder(reminder.id)
        self.assertEqual((current.status, current.delivery_state, current.completed_at), ("due", "delivered", None))
        self.assertEqual(self.database.connection.execute("SELECT status FROM outbox").fetchone()[0], "succeeded")
        self.assertEqual(delivered.status, "pending")

    async def test_sqlite_integrity_check_after_scheduler_scenarios(self) -> None:
        reminder = self.reminder()
        await self.run_once(self.scheduler())
        self.repository.complete_reminder(reminder.id, expected_version=self.repository.get_reminder(reminder.id).version, context=MANUAL_CONTEXT)
        self.assertEqual(self.database.integrity_check(), "ok")


def _as_dt(value: str | None) -> datetime:
    assert value is not None
    return datetime.fromisoformat(value[:-1] + "+00:00")


if __name__ == "__main__":
    unittest.main()
