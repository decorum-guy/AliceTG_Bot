from __future__ import annotations

import asyncio
import json
import logging
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from app.planning import MutationContext, PlanningDatabase, PlanningHealthService, PlanningOperationsStateStore, PlanningRepository
from app.planning.api.service import PlanningApiService
from app.planning.models import REMINDER_DELIVERY_JOB_TYPE
from app.planning.scheduler import DurableReminderScheduler, SchedulerHeartbeat, SchedulerRun


NOW = "2026-08-12T08:00:00.000000Z"
CONTEXT = MutationContext(
    audience="operator",
    actor_id="a8-health-fixture",
    actor_type="operator",
    surface="operator",
)


class HealthClock:
    def __init__(self, value: str = NOW) -> None:
        self.value = datetime.fromisoformat(value[:-1] + "+00:00")

    def __call__(self) -> str:
        return self.value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")

    def advance(self, **kwargs: int) -> str:
        self.value += timedelta(**kwargs)
        return self()


class PlanningHealthA8Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = PlanningDatabase(self.root / "planning.sqlite3")
        self.database_closed = False
        self.repository = PlanningRepository(self.database, now_fn=lambda: NOW)
        self.clock = HealthClock()
        self.state_store = PlanningOperationsStateStore(self.root / "backups")
        self.scheduler = SimpleNamespace(
            heartbeat=SchedulerHeartbeat(
                heartbeat_at=None,
                last_iteration_finished_at=None,
                last_iteration_succeeded=None,
            )
        )

    def tearDown(self) -> None:
        if not self.database_closed:
            self.database.close()
        self.temp.cleanup()

    def health(
        self,
        *,
        scheduler_enabled: bool = True,
        backup_enabled: bool = False,
        backup_service_ready: bool = False,
        logger: logging.Logger | None = None,
    ) -> PlanningHealthService:
        return PlanningHealthService(
            self.database,
            scheduler=self.scheduler,
            scheduler_enabled=scheduler_enabled,
            scheduler_heartbeat_stale_after_seconds=15,
            backup_dir=str(self.root / "backups"),
            backup_enabled=backup_enabled,
            backup_service_ready=backup_service_ready,
            backup_interval_seconds=300,
            application_version="a8-test",
            application_commit="a8-commit",
            now_fn=self.clock,
            state_store=self.state_store,
            logger=logger,
        )

    def reminder(self, *, due_at: str = NOW, title: str = "A8 health fixture"):
        return self.repository.create_reminder(
            title=title,
            due_at_utc=due_at,
            timezone="UTC",
            context=CONTEXT,
            outbox_job_type=REMINDER_DELIVERY_JOB_TYPE,
            outbox_payload={"chat_id": -100000000002},
        )

    def test_schema_db_and_disabled_scheduler_facts_are_content_free(self) -> None:
        snapshot = self.health(scheduler_enabled=False).snapshot(correlation_id="health-correlation")
        self.assertEqual(snapshot["planningSchemaVersion"], 7)
        self.assertTrue(snapshot["dbAvailable"])
        self.assertEqual(snapshot["dbIntegrityStatus"], "ok")
        self.assertFalse(snapshot["durableSchedulerEnabled"])
        self.assertEqual(snapshot["schedulerHealth"], "disabled")
        self.assertEqual(snapshot["providerStatus"], "not_configured")
        self.assertIsNone(snapshot["providerLastSyncAt"])
        encoded = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
        for forbidden in ("A8 health fixture", "notes", "Telegram", "token", "/"):
            self.assertNotIn(forbidden, encoded)

    def test_scheduler_heartbeat_unknown_healthy_and_stale_states(self) -> None:
        service = self.health()
        initial = service.snapshot()
        self.assertEqual(initial["schedulerHealth"], "unknown")
        self.assertNotIn("planning.scheduler_heartbeat_stale", {item["code"] for item in initial["incidents"]})

        self.scheduler.heartbeat = SchedulerHeartbeat(
            heartbeat_at=self.clock(),
            last_iteration_finished_at=self.clock(),
            last_iteration_succeeded=True,
        )
        healthy = service.snapshot()
        self.assertEqual(healthy["schedulerHealth"], "healthy")
        self.assertEqual(healthy["schedulerHeartbeatAgeSeconds"], 0)

        stale_at = self.clock.value - timedelta(seconds=60)
        stale = stale_at.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        self.scheduler.heartbeat = SchedulerHeartbeat(stale, stale, True)
        degraded = service.snapshot()
        self.assertEqual(degraded["schedulerHealth"], "degraded")
        self.assertIn(
            "planning.scheduler_heartbeat_stale",
            {item["code"] for item in degraded["incidents"]},
        )

    def test_real_scheduler_loop_updates_heartbeat_lifecycle(self) -> None:
        async def exercise() -> None:
            scheduler = DurableReminderScheduler(
                self.database,
                telegram_transport=SimpleNamespace(channel="telegram"),
                mobile_transport=None,
                default_chat_id=-100000000003,
                interval_seconds=5,
                now_fn=lambda: NOW,
                jitter_fn=lambda _base: 0,
            )
            scheduler.run_once = AsyncMock(  # type: ignore[method-assign]
                return_value=SchedulerRun(processed_jobs=0, reconciled_reminders=0)
            )
            task = asyncio.create_task(scheduler.run_forever())
            scheduler._task = task  # type: ignore[attr-defined]
            for _ in range(20):
                await asyncio.sleep(0)
                if scheduler.heartbeat.heartbeat_at is not None:
                    break
            self.assertIsNotNone(scheduler.heartbeat.heartbeat_at)
            self.assertIsNotNone(scheduler.heartbeat.last_iteration_finished_at)
            self.assertTrue(scheduler.heartbeat.last_iteration_succeeded)
            await scheduler.close()
            self.assertTrue(task.done())

        asyncio.run(exercise())

    def test_outbox_age_terminal_failure_and_delivered_open_are_distinguished(self) -> None:
        stuck = self.reminder(due_at="2026-08-12T07:00:00.000000Z", title="stuck fixture")
        failed = self.reminder(due_at="2026-08-12T07:00:00.000000Z", title="failed fixture")
        delivered = self.reminder(due_at="2026-08-12T07:00:00.000000Z", title="delivered open fixture")
        with self.database.transaction():
            self.database.connection.execute(
                "UPDATE reminders SET status = 'due', delivery_state = 'failed', final_failure_at = ? WHERE id = ?",
                (NOW, failed.id),
            )
            self.database.connection.execute(
                "UPDATE outbox SET status = 'failed', available_at = ? WHERE reminder_id = ?",
                ("2026-08-12T07:00:00.000000Z", failed.id),
            )
            self.database.connection.execute(
                "UPDATE reminders SET status = 'due', delivery_state = 'delivered' WHERE id = ?",
                (delivered.id,),
            )
            self.database.connection.execute(
                "UPDATE outbox SET status = 'succeeded' WHERE reminder_id = ?",
                (delivered.id,),
            )
        # The first reminder remains queued and eligible; the other two are
        # used to prove terminal and delivered-but-open distinctions.
        snapshot = self.health().snapshot()
        self.assertEqual(snapshot["queuedOutboxCount"], 1)
        self.assertEqual(snapshot["terminalFailedReminderCount"], 1)
        self.assertEqual(snapshot["eligibleQueuedOrLeasedOutboxCount"], 1)
        self.assertGreaterEqual(snapshot["oldestQueuedOrLeasedOutboxAgeSeconds"], 3_000)
        codes = {item["code"] for item in snapshot["incidents"]}
        self.assertIn("planning.outbox_stuck", codes)
        self.assertIn("planning.delivery_terminal_failure", codes)

        # A delivered reminder is old and due, but its outbox is successful;
        # it does not add a delivery-stuck condition.
        self.assertEqual(
            next(item["aggregateCount"] for item in snapshot["incidents"] if item["code"] == "planning.outbox_stuck"),
            1,
        )
        del stuck

    def test_backup_fresh_overdue_failed_and_restore_failed_states(self) -> None:
        service = self.health(backup_enabled=True, backup_service_ready=True)
        self.state_store.update(
            last_backup_attempt_at=self.clock(),
            last_successful_backup_at=self.clock(),
            last_backup_status="success",
            last_backup_error_code=None,
        )
        self.assertEqual(service.snapshot()["backupStatus"], "fresh")

        self.clock.advance(seconds=301)
        overdue = service.snapshot()
        self.assertEqual(overdue["backupStatus"], "overdue")
        self.assertIn("planning.backup_overdue", {item["code"] for item in overdue["incidents"]})

        self.state_store.update(last_backup_status="failed", last_backup_error_code="backup_failed")
        failed = service.snapshot()
        self.assertEqual(failed["backupStatus"], "failed")
        self.assertIn("planning.backup_failed", {item["code"] for item in failed["incidents"]})

        self.state_store.update(
            last_restore_verification_at=self.clock(),
            last_restore_verification_status="failed",
            last_restore_verification_error_code="hash_mismatch",
        )
        restore_failed = service.snapshot()
        self.assertIn(
            "planning.restore_verification_failed",
            {item["code"] for item in restore_failed["incidents"]},
        )

    def test_database_unavailable_and_integrity_failure_are_degraded_not_process_crashes(self) -> None:
        service = self.health()
        with patch.object(self.database, "integrity_check", return_value="not ok"):
            failed = service.snapshot()
        self.assertTrue(failed["dbAvailable"])
        self.assertEqual(failed["dbIntegrityStatus"], "failed")
        self.assertIn(
            "planning.database_integrity_failure",
            {item["code"] for item in failed["incidents"]},
        )

        self.database.close()
        self.database_closed = True
        unavailable = service.snapshot()
        self.assertFalse(unavailable["dbAvailable"])
        self.assertEqual(unavailable["dbIntegrityStatus"], "unknown")

    def test_incident_transition_logging_is_suppressed_for_unchanged_state(self) -> None:
        self.reminder(due_at="2026-08-12T07:00:00.000000Z", title="logging fixture")
        records: list[str] = []
        logger = logging.getLogger("a8-health-test")
        logger.handlers.clear()
        logger.setLevel(logging.WARNING)
        logger.propagate = False
        handler = _MessageHandler(records)
        logger.addHandler(handler)
        try:
            service = self.health(logger=logger)
            first = service.snapshot(correlation_id="one")
            self.assertEqual(len(records), 1)
            service.snapshot(correlation_id="two")
            self.assertEqual(len(records), 1)

            second = self.reminder(due_at="2026-08-12T07:00:00.000000Z", title="second logging fixture")
            del second
            service.snapshot(correlation_id="three")
            self.assertEqual(len(records), 2)
            self.assertIn("planning.outbox_stuck", records[0])
            self.assertNotIn("logging fixture", " ".join(records))
            self.assertNotEqual(first["incidents"], [])
        finally:
            logger.removeHandler(handler)

    def test_a4_status_envelope_and_capability_metadata_are_preserved(self) -> None:
        health = self.health(scheduler_enabled=False)
        api = PlanningApiService(self.database, now_fn=self.clock, health_service=health)
        response = api.status(audience="operator", correlation_id="status-correlation")
        self.assertEqual(response["schemaVersion"], "planning.v1")
        self.assertEqual(response["kind"], "status")
        self.assertEqual(response["apiVersion"], "v1")
        self.assertIn("capabilities", response)
        self.assertIn("capabilityMetadata", response)
        self.assertIn("planningHealth", response)
        encoded = json.dumps(response, ensure_ascii=False, sort_keys=True)
        for forbidden in ("A8 health fixture", "notes", "Telegram", "token", str(self.root)):
            self.assertNotIn(forbidden, encoded)


class _MessageHandler(logging.Handler):
    def __init__(self, records: list[str]) -> None:
        super().__init__()
        self.records = records

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record.getMessage())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
