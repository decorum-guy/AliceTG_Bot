from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.planning import (
    AuditWriter,
    MutationContext,
    PlanningDatabase,
    PlanningDatabaseConfig,
    PlanningConfigurationError,
    PlanningIdempotencyConflictError,
    PlanningMigrationError,
    PlanningNewerSchemaError,
    PlanningRepository,
    PlanningTransactionRequiredError,
    PlanningValidationError,
    PlanningVersionConflictError,
    bounded_redacted_json,
    reject_secret_fields,
)
from app.planning.migrations import Migration, MigrationRunner


REQUEST_HASH = "sha256:" + "a" * 64
OTHER_REQUEST_HASH = "sha256:" + "b" * 64
CREATED_AT = "2026-08-15T10:00:00Z"
CONTEXT = MutationContext(
    audience="operator",
    actor_id="fixture-operator",
    actor_type="operator",
    surface="operator",
)


class PlanningStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "planning.sqlite3"
        self.database = PlanningDatabase(self.path)
        self.repository = PlanningRepository(self.database)

    def tearDown(self) -> None:
        self.database.close()
        self.temp_dir.cleanup()

    def test_empty_database_migrates_repeatably_and_has_required_pragmas(self) -> None:
        self.assertEqual(self.database.schema_version(), 1)
        self.assertEqual(self.database.migrate(), 1)
        self.assertEqual(self.database.connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(self.database.connection.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
        tables = {
            row[0]
            for row in self.database.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        self.assertTrue(
            {
                "schema_migrations",
                "reminders",
                "tasks",
                "projects",
                "calendar_events",
                "idempotency_keys",
                "outbox",
                "delivery_attempts",
                "audit_events",
                "provider_mappings",
                "sync_cursors",
                "sync_conflicts",
            }.issubset(tables)
        )
        self.assertEqual(self.database.connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0], 1)
        self.assertEqual(self.database.integrity_check(), "ok")

    def test_newer_schema_is_refused(self) -> None:
        with self.database.transaction():
            self.database.connection.execute(
                "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
                (99, "future_schema", CREATED_AT),
            )
        with self.assertRaises(PlanningNewerSchemaError):
            self.database.migrate()

    def test_concurrent_empty_database_initialization_is_safe(self) -> None:
        concurrent_path = Path(self.temp_dir.name) / "concurrent.sqlite3"
        barrier = threading.Barrier(2)

        def initialize() -> int:
            barrier.wait()
            database = PlanningDatabase(concurrent_path)
            try:
                return database.schema_version()
            finally:
                database.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            versions = list(executor.map(lambda _: initialize(), (1, 2)))
        self.assertEqual(versions, [1, 1])
        check = PlanningDatabase(concurrent_path)
        try:
            self.assertEqual(check.connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0], 1)
        finally:
            check.close()

    def test_migration_failure_rolls_back_all_ddl(self) -> None:
        failed_path = Path(self.temp_dir.name) / "failed.sqlite3"
        database = PlanningDatabase(failed_path, auto_migrate=False)
        try:
            migration = Migration(
                version=1,
                name="broken",
                sql="CREATE TABLE should_rollback (id INTEGER); INSERT INTO missing_table VALUES (1);",
            )
            with self.assertRaises(PlanningMigrationError):
                MigrationRunner(database, (migration,)).apply()
            self.assertEqual(database.schema_version(), 0)
            self.assertIsNone(
                database.connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'should_rollback'"
                ).fetchone()
            )
        finally:
            database.close()

    def test_production_configuration_rejects_ephemeral_paths(self) -> None:
        with self.assertRaises(PlanningConfigurationError):
            PlanningDatabaseConfig.from_env(
                {"APP_ENV": "production", "PLANNING_DB_PATH": "/tmp/planning.sqlite3"}
            )
        config = PlanningDatabaseConfig.from_env(
            {"APP_ENV": "production", "PLANNING_DB_PATH": "/app/data/planning.sqlite3"}
        )
        self.assertTrue(config.is_production)

    def test_close_reopen_wal_persists_typed_objects(self) -> None:
        task = self.repository.create_task(
            title="Date-only storage test",
            due_date="2026-08-15",
            priority="normal",
            context=CONTEXT,
        )
        self.database.close()
        reopened = PlanningDatabase(self.path)
        try:
            restored = PlanningRepository(reopened).get_task(task.id)
            self.assertEqual(restored, task)
            self.assertEqual(reopened.integrity_check(), "ok")
        finally:
            reopened.close()
            self.database = PlanningDatabase(self.path)
            self.repository = PlanningRepository(self.database)

    def test_reminder_lifecycle_keeps_delivery_separate_and_uses_tombstone(self) -> None:
        reminder = self.repository.create_reminder(
            title="Reminder fixture",
            due_at_utc=CREATED_AT,
            timezone="Europe/Moscow",
            context=CONTEXT,
        )
        self.assertEqual(reminder.status, "pending")
        self.assertEqual(reminder.delivery_state, "not_due")
        self.assertIsNone(reminder.deleted_at)
        completed = self.repository.complete_reminder(
            reminder.id,
            expected_version=reminder.version,
            context=CONTEXT,
        )
        self.assertEqual(completed.status, "completed")
        self.assertIsNone(completed.deleted_at)
        cancelled = self.repository.cancel_reminder(
            completed.id,
            expected_version=completed.version,
            context=CONTEXT,
        )
        self.assertEqual(cancelled.status, "cancelled")
        self.assertIsNotNone(cancelled.deleted_at)
        self.assertEqual(self.database.connection.execute("SELECT COUNT(*) FROM reminders").fetchone()[0], 1)

    def test_date_only_and_timed_tasks_remain_distinct(self) -> None:
        date_only = self.repository.create_task(
            title="Date only",
            due_date="2026-08-15",
            priority="low",
            context=CONTEXT,
        )
        self.assertEqual(date_only.due_date, "2026-08-15")
        self.assertIsNone(date_only.due_time)
        self.assertIsNone(date_only.timezone)
        timed = self.repository.create_task(
            title="Timed task",
            due_date="2026-08-15",
            due_time="09:30",
            timezone="Europe/Moscow",
            priority="high",
            context=CONTEXT,
        )
        self.assertEqual((timed.due_date, timed.due_time, timed.timezone), ("2026-08-15", "09:30", "Europe/Moscow"))
        with self.assertRaises(PlanningValidationError):
            self.repository.create_task(
                title="Invalid timed task",
                due_time="09:30",
                priority="normal",
                context=CONTEXT,
            )

    def test_calendar_event_representations_and_exclusive_end(self) -> None:
        all_day = self.repository.create_calendar_event(
            title="All day",
            all_day=True,
            start_date="2026-08-15",
            end_date_exclusive="2026-08-16",
            timezone="Europe/Moscow",
            context=CONTEXT,
        )
        self.assertTrue(all_day.all_day)
        self.assertEqual((all_day.start_date, all_day.end_date_exclusive), ("2026-08-15", "2026-08-16"))
        timed = self.repository.create_calendar_event(
            title="Timed event",
            all_day=False,
            start_at_utc="2026-08-15T07:00:00Z",
            end_at_utc="2026-08-15T08:00:00Z",
            timezone="Europe/Moscow",
            context=CONTEXT,
        )
        self.assertFalse(timed.all_day)
        self.assertEqual(timed.start_date, None)
        with self.assertRaises(PlanningValidationError):
            self.repository.create_calendar_event(
                title="Mixed event",
                all_day=True,
                start_date="2026-08-15",
                end_date_exclusive="2026-08-16",
                start_at_utc="2026-08-15T07:00:00Z",
                timezone="Europe/Moscow",
                context=CONTEXT,
            )
        with self.assertRaises(PlanningValidationError):
            self.repository.create_calendar_event(
                title="Empty range",
                all_day=True,
                start_date="2026-08-16",
                end_date_exclusive="2026-08-16",
                timezone="Europe/Moscow",
                context=CONTEXT,
            )

    def test_sql_constraints_protect_shape_and_foreign_keys(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.database.connection.execute(
                """
                INSERT INTO tasks(
                    id, title, due_time, priority, status, source, version,
                    created_at, updated_at, audit_correlation_id
                ) VALUES (?, ?, ?, 'none', 'open', 'operator', 1, ?, ?, ?)
                """,
                (str(uuid.uuid4()), "invalid", "09:00", CREATED_AT, CREATED_AT, str(uuid.uuid4())),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.database.connection.execute(
                """
                INSERT INTO calendar_events(
                    id, title, all_day, start_at_utc, end_at_utc, timezone,
                    sync_state, source, version, created_at, updated_at, audit_correlation_id
                ) VALUES (?, 'mixed', 1, ?, ?, 'Europe/Moscow', 'local_only', 'operator', 1, ?, ?, ?)
                """,
                (str(uuid.uuid4()), CREATED_AT, CREATED_AT, CREATED_AT, CREATED_AT, str(uuid.uuid4())),
            )
        task = self.repository.create_task(title="Foreign key target", context=CONTEXT)
        self.assertIsNotNone(task)
        with self.assertRaises(sqlite3.IntegrityError):
            self.database.connection.execute(
                "INSERT INTO tasks(id, title, priority, project_id, status, source, version, created_at, updated_at, audit_correlation_id) VALUES (?, ?, 'none', ?, 'open', 'operator', 1, ?, ?, ?)",
                (str(uuid.uuid4()), "bad project", str(uuid.uuid4()), CREATED_AT, CREATED_AT, str(uuid.uuid4())),
            )

    def test_expected_version_update_and_stale_conflict(self) -> None:
        task = self.repository.create_task(title="Versioned task", context=CONTEXT)
        updated = self.repository.update_task(
            task.id,
            expected_version=1,
            title="Updated task",
            context=CONTEXT,
        )
        self.assertEqual(updated.version, 2)
        with self.assertRaises(PlanningVersionConflictError) as raised:
            self.repository.update_task(task.id, expected_version=1, title="stale", context=CONTEXT)
        self.assertEqual(raised.exception.actual_version, 2)

    def test_competing_updates_allow_one_guarded_winner(self) -> None:
        task = self.repository.create_task(title="Concurrent task", context=CONTEXT)
        other_database = PlanningDatabase(self.path)
        other_repository = PlanningRepository(other_database)
        barrier = threading.Barrier(2)

        def update(repository: PlanningRepository, title: str) -> object:
            barrier.wait()
            try:
                return repository.update_task(task.id, expected_version=1, title=title, context=CONTEXT)
            except Exception as exc:  # returned for deterministic winner inspection
                return exc

        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                first_future = executor.submit(update, self.repository, "winner one")
                second_future = executor.submit(update, other_repository, "winner two")
                results = [first_future.result(), second_future.result()]
            winners = [result for result in results if not isinstance(result, Exception)]
            conflicts = [result for result in results if isinstance(result, PlanningVersionConflictError)]
            self.assertEqual(len(winners), 1)
            self.assertEqual(len(conflicts), 1)
            self.assertEqual(self.repository.get_task(task.id).version, 2)
        finally:
            other_database.close()

    def test_tombstone_archive_and_event_delete_are_not_physical_deletes(self) -> None:
        task = self.repository.create_task(title="Archive me", due_date="2026-08-15", context=CONTEXT)
        archived = self.repository.archive_task(task.id, expected_version=1, context=CONTEXT)
        self.assertEqual(archived.status, "archived")
        self.assertIsNotNone(archived.deleted_at)
        self.assertEqual(self.repository.list_tasks_due(on_or_before="2026-08-15"), [])
        event = self.repository.create_calendar_event(
            title="Delete me",
            all_day=True,
            start_date="2026-08-15",
            end_date_exclusive="2026-08-16",
            timezone="Europe/Moscow",
            context=CONTEXT,
        )
        deleted = self.repository.delete_calendar_event(event.id, expected_version=1, context=CONTEXT)
        self.assertIsNotNone(deleted.deleted_at)
        self.assertEqual(self.database.connection.execute("SELECT COUNT(*) FROM calendar_events").fetchone()[0], 1)
        project = self.repository.create_project(name="Project tombstone", context=CONTEXT)
        deleted_project = self.repository.delete_project(project.id, expected_version=1, context=CONTEXT)
        self.assertIsNotNone(deleted_project.deleted_at)
        self.assertEqual(self.database.connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 1)

    def test_audit_is_transactional_with_domain_mutation(self) -> None:
        task = self.repository.create_task(title="Audited task", context=CONTEXT)
        audit = self.database.connection.execute(
            "SELECT action, object_domain, object_id, old_version, new_version FROM audit_events WHERE object_id = ?",
            (task.id,),
        ).fetchone()
        self.assertEqual(tuple(audit), ("create", "task", task.id, None, 1))

    def test_audit_failure_rolls_back_domain_mutation(self) -> None:
        failing_repository = PlanningRepository(self.database, AuditWriter(self.database, fail=True))
        with self.assertRaises(RuntimeError):
            failing_repository.create_task(title="Must roll back", context=CONTEXT)
        self.assertEqual(self.database.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)
        self.assertEqual(self.database.connection.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0], 0)

    def test_audit_redacts_sensitive_values_and_bounds_content(self) -> None:
        representation = bounded_redacted_json(
            {
                "token": "do-not-store",
                "transcript": "raw voice data",
                "notes": "x" * 2_000,
            }
        )
        self.assertNotIn("do-not-store", representation)
        self.assertNotIn("raw voice data", representation)
        self.assertLessEqual(len(representation), 8_192)
        with self.assertRaises(PlanningValidationError):
            reject_secret_fields({"provider_token": "must not persist"}, field="fixture")

    def test_idempotency_first_replay_and_hash_conflict(self) -> None:
        mutation_count = 0

        def mutation() -> dict[str, str]:
            nonlocal mutation_count
            mutation_count += 1
            return {"status": "created", "id": "canonical-fixture"}

        first = self.repository.execute_idempotent(
            audience="operator",
            key="fixture-key-1",
            request_hash=REQUEST_HASH,
            mutation=mutation,
        )
        replay = self.repository.execute_idempotent(
            audience="operator",
            key="fixture-key-1",
            request_hash=REQUEST_HASH,
            mutation=lambda: {"status": "wrong"},
        )
        self.assertEqual(first, replay)
        self.assertEqual(mutation_count, 1)
        with self.assertRaises(PlanningIdempotencyConflictError):
            self.repository.execute_idempotent(
                audience="operator",
                key="fixture-key-1",
                request_hash=OTHER_REQUEST_HASH,
                mutation=lambda: {"status": "must not run"},
            )

    def test_low_level_idempotency_claim_requires_transaction(self) -> None:
        with self.assertRaises(PlanningTransactionRequiredError):
            self.repository.claim_idempotency(audience="operator", key="key", request_hash=REQUEST_HASH)
        with self.database.transaction():
            claim = self.repository.claim_idempotency(
                audience="operator", key="key", request_hash=REQUEST_HASH
            )
            self.assertTrue(claim.is_new)
            self.repository.store_idempotency_response(
                audience="operator",
                key="key",
                request_hash=REQUEST_HASH,
                response={"ok": True},
            )

    def test_concurrent_idempotency_claim_runs_mutation_once(self) -> None:
        other_database = PlanningDatabase(self.path)
        other_repository = PlanningRepository(other_database)
        barrier = threading.Barrier(2)
        count_lock = threading.Lock()
        count = 0

        def mutation() -> dict[str, str]:
            nonlocal count
            with count_lock:
                count += 1
            return {"result": "canonical"}

        def execute(repository: PlanningRepository) -> object:
            barrier.wait()
            return repository.execute_idempotent(
                audience="operator",
                key="concurrent-key",
                request_hash=REQUEST_HASH,
                mutation=mutation,
            )

        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(execute, (self.repository, other_repository)))
            self.assertEqual(results[0], results[1])
            self.assertEqual(count, 1)
        finally:
            other_database.close()

    def test_domain_and_outbox_rollback_together(self) -> None:
        def failing_mutation() -> dict[str, str]:
            task = self.repository.create_task(title="Atomic outbox task", context=CONTEXT)
            self.repository.enqueue_outbox(
                job_type="planning.test.v1",
                payload={"task_id": task.id},
                correlation_id=str(uuid.uuid4()),
            )
            raise RuntimeError("force rollback")

        with self.assertRaises(RuntimeError):
            self.repository.execute_idempotent(
                audience="operator",
                key="rollback-key",
                request_hash=REQUEST_HASH,
                mutation=failing_mutation,
            )
        self.assertEqual(self.database.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)
        self.assertEqual(self.database.connection.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 0)
        self.assertEqual(self.database.connection.execute("SELECT COUNT(*) FROM idempotency_keys").fetchone()[0], 0)

    def test_outbox_enqueue_is_durable_and_inside_domain_transaction(self) -> None:
        task = None
        with self.database.transaction():
            task = self.repository.create_task(title="Outbox task", context=CONTEXT)
            job = self.repository.enqueue_outbox(
                job_type="planning.test.v1",
                payload={"task_id": task.id, "kind": "fixture"},
                correlation_id=str(uuid.uuid4()),
            )
        restored = self.repository.get_outbox(job.id)
        self.assertEqual(restored.payload["task_id"], task.id)
        self.assertEqual(restored.status, "queued")

    def test_provider_mapping_uniqueness_and_sync_foundations(self) -> None:
        task = self.repository.create_task(title="Mapped task", context=CONTEXT)
        mapping = self.repository.create_provider_mapping(
            domain="task",
            object_id=task.id,
            provider="fixture-provider",
            external_id="external-1",
        )
        self.assertEqual(mapping.object_id, task.id)
        with self.assertRaises(sqlite3.IntegrityError):
            self.repository.create_provider_mapping(
                domain="task",
                object_id=task.id,
                provider="fixture-provider",
                external_id="external-1",
            )
        cursor = self.repository.create_sync_cursor(provider="fixture-provider", scope="fixture")
        self.assertEqual(cursor.scope, "fixture")
        conflict = self.repository.create_sync_conflict(
            domain="task",
            object_id=task.id,
            provider="fixture-provider",
            external_id="external-1",
            details={"reason": "fixture"},
        )
        self.assertEqual(conflict.status, "open")

    def test_query_plans_use_due_and_outbox_indexes(self) -> None:
        due_plan = self.database.connection.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM reminders WHERE deleted_at IS NULL AND status IN ('pending', 'due') AND due_at_utc <= ?",
            (CREATED_AT,),
        ).fetchall()
        outbox_plan = self.database.connection.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM outbox WHERE status = 'queued' AND available_at <= ? ORDER BY available_at",
            (CREATED_AT,),
        ).fetchall()
        due_details = " ".join(str(row[3]) for row in due_plan)
        outbox_details = " ".join(str(row[3]) for row in outbox_plan)
        self.assertIn("idx_reminders_due", due_details)
        self.assertIn("idx_outbox_available", outbox_details)


if __name__ == "__main__":
    unittest.main()
