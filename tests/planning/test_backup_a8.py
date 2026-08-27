from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from unittest.mock import patch

from app.planning import MutationContext, PlanningDatabase, PlanningRepository
from app.planning.backup import (
    BACKUP_MANIFEST_VERSION,
    PlanningBackupConfigurationError,
    PlanningBackupError,
    PlanningBackupService,
    PlanningBackupVerificationError,
    PlanningBackupVerifier,
    _decrypt_file,
    _encrypt_file,
    _hash_file,
    _write_package_zip,
)
from app.planning.delivery import DeliveryResult
from app.planning.delivery_settings import ReminderDeliveryPreferencesStore
from app.planning.legacy_import import LegacyReminderImporter
from app.planning.models import REMINDER_DELIVERY_JOB_TYPE
from app.planning.scheduler import DurableReminderScheduler
from app.planning.telegram_actions import TelegramActionTokenStore
from app.services.reminder_store import ReminderSettings


KEY = "11" * 32
WRONG_KEY = "22" * 32
NOW = "2026-08-12T08:00:00.000000Z"
CONTEXT = MutationContext(
    audience="operator",
    actor_id="a8-fixture",
    actor_type="operator",
    surface="operator",
)


class A8Clock:
    def __init__(self, value: str = NOW) -> None:
        self.value = datetime.fromisoformat(value[:-1] + "+00:00")

    def __call__(self) -> str:
        return self.value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")

    def advance(self, **kwargs: int) -> str:
        self.value += timedelta(**kwargs)
        return self()


class FakeTelegramTransport:
    channel = "telegram"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def send(self, **kwargs: Any) -> DeliveryResult:
        self.calls.append(kwargs)
        return DeliveryResult.success(provider_receipt="synthetic-receipt")


class PlanningBackupA8Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database_path = self.root / "planning.sqlite3"
        self.backup_dir = self.root / "backups"
        self.clock = A8Clock()
        self.database = PlanningDatabase(self.database_path)
        self.repository = PlanningRepository(self.database, now_fn=self.clock)

    def tearDown(self) -> None:
        self.database.close()
        self.temp.cleanup()

    def service(self, *, retention_count: int = 14, key: str = KEY) -> PlanningBackupService:
        return PlanningBackupService(
            self.database,
            backup_dir=self.backup_dir,
            encryption_key=key,
            retention_count=retention_count,
            application_version="a8-test",
            application_commit="a8-fixture-commit",
            now_fn=self.clock,
        )

    def verifier(self, *, key: str = KEY) -> PlanningBackupVerifier:
        return PlanningBackupVerifier(
            backup_dir=self.backup_dir,
            encryption_key=key,
            now_fn=self.clock,
        )

    def reminder(self, *, due_at: str = NOW, title: str = "A8 PRIVATE FIXTURE"):
        return self.repository.create_reminder(
            title=title,
            due_at_utc=due_at,
            timezone="UTC",
            context=CONTEXT,
            outbox_job_type=REMINDER_DELIVERY_JOB_TYPE,
            outbox_payload={"chat_id": -100000000001},
        )

    def test_online_backup_round_trip_manifest_and_restrictive_permissions(self) -> None:
        self.reminder()
        result = self.service().backup()
        package = self.backup_dir / result.package_name

        self.assertTrue(package.exists())
        self.assertNotIn("A8 PRIVATE FIXTURE", package.name)
        self.assertNotIn(b"A8 PRIVATE FIXTURE", package.read_bytes())
        if hasattr(os, "stat"):
            self.assertEqual(package.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.backup_dir.stat().st_mode & 0o777, 0o700)
        self.assertEqual(len(self.service().list_backups()), 1)

        with tempfile.TemporaryDirectory() as work_name:
            work = Path(work_name)
            payload = work / "payload.zip"
            _decrypt_file(package, payload, bytes.fromhex(KEY))
            with zipfile.ZipFile(payload) as archive:
                manifest_bytes = archive.read("manifest.json")
                manifest = json.loads(manifest_bytes)
                self.assertNotIn(b"A8 PRIVATE FIXTURE", manifest_bytes)
                self.assertEqual(manifest["manifest_version"], BACKUP_MANIFEST_VERSION)
                self.assertEqual(manifest["wal_policy"], "standalone_snapshot_does_not_rely_on_source_wal")
                self.assertEqual(manifest["table_counts"]["reminders"], 1)
                self.assertEqual(manifest["table_counts"]["outbox"], 1)
                restored_path = work / "planning.sqlite3"
                restored_path.write_bytes(archive.read("planning.sqlite3"))
            check = sqlite3.connect(restored_path)
            try:
                self.assertEqual(check.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(check.execute("SELECT COUNT(*) FROM pragma_foreign_key_check").fetchone()[0], 0)
            finally:
                check.close()

        verified = self.verifier().verify(result.package_name)
        self.assertTrue(verified.to_dict()["ok"])
        self.assertEqual(verified.resumable_due_jobs, 1)
        self.assertEqual(verified.table_counts["reminders"], 1)

        # The source remains writable after an online snapshot.
        self.reminder(title="A8 second synthetic fixture", due_at="2026-08-12T09:00:00.000000Z")
        self.assertEqual(self.database.connection.execute("SELECT COUNT(*) FROM reminders").fetchone()[0], 2)

    def test_reminder_delivery_preferences_survive_encrypted_backup_and_isolated_restore(self) -> None:
        preferences = ReminderDeliveryPreferencesStore(self.database, now_fn=self.clock)
        initial = preferences.ensure_from_legacy(
            spoken_endpoint="alice",
            notify_telegram_enabled=True,
            notify_iphone_enabled=False,
        )
        updated = preferences.update(
            expected_revision=initial.revision,
            spoken_endpoint="jarvis",
            phone_channels=("telegram", "home_assistant"),
            context=CONTEXT,
        )
        self.assertEqual(updated.revision, 1)
        self.assertEqual(updated.updated_at, NOW)

        package = self.backup_dir / self.service().backup().package_name
        with tempfile.TemporaryDirectory() as work_name:
            work = Path(work_name)
            payload = work / "payload.zip"
            _decrypt_file(package, payload, bytes.fromhex(KEY))
            with zipfile.ZipFile(payload) as archive:
                manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(manifest["schema_version"], 7)
            self.assertEqual(manifest["table_counts"]["reminder_delivery_preferences"], 1)

        restored_path = self._extract_database(package)
        restored = PlanningDatabase(restored_path)
        try:
            row = restored.connection.execute(
                "SELECT spoken_endpoint, phone_channels_json, revision, updated_at "
                "FROM reminder_delivery_preferences WHERE id = 1"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(
                (row["spoken_endpoint"], json.loads(row["phone_channels_json"]), row["revision"], row["updated_at"]),
                ("jarvis", ["telegram", "home_assistant"], 1, NOW),
            )
        finally:
            restored.close()

        verified = self.verifier().verify(package.name)
        self.assertEqual(verified.verified_schema_version, 7)
        self.assertEqual(verified.table_counts["reminder_delivery_preferences"], 1)

    def test_restore_rejects_arbitrary_unknown_tables_after_schema_v7_allowlist_extension(self) -> None:
        self.reminder()
        package = self.backup_dir / self.service().backup().package_name

        def add_unknown_table(path: Path) -> None:
            connection = sqlite3.connect(path)
            try:
                connection.execute("CREATE TABLE arbitrary_owner_data (id INTEGER)")
                connection.commit()
            finally:
                connection.close()

        unknown = self._rewrite_package(
            package,
            "planning-20260812T080010Z-schema7-444444444444.sqlite3.a8",
            mutate_database=add_unknown_table,
            refresh_hash=True,
        )
        with self.assertRaisesRegex(PlanningBackupVerificationError, "unexpected_table"):
            self.verifier().verify(unknown.name)

    def test_wrong_key_corrupt_ciphertext_and_truncated_package_fail_closed(self) -> None:
        self.reminder()
        package = self.backup_dir / self.service().backup().package_name
        original = package.read_bytes()

        with self.assertRaisesRegex(PlanningBackupVerificationError, "encryption_authentication_failed"):
            self.verifier(key=WRONG_KEY).verify(package.name)

        corrupt_path = self.backup_dir / "planning-20260812T080001Z-schema4-aaaaaaaaaaaa.sqlite3.a8"
        corrupt = bytearray(original)
        corrupt[-1] ^= 0x01
        corrupt_path.write_bytes(corrupt)
        corrupt_path.chmod(0o600)
        with self.assertRaisesRegex(PlanningBackupVerificationError, "encryption_authentication_failed"):
            self.verifier().verify(corrupt_path.name)

        truncated_path = self.backup_dir / "planning-20260812T080002Z-schema4-bbbbbbbbbbbb.sqlite3.a8"
        truncated_path.write_bytes(original[:-10])
        truncated_path.chmod(0o600)
        with self.assertRaises(PlanningBackupVerificationError):
            self.verifier().verify(truncated_path.name)
        self.assertEqual(truncated_path.read_bytes(), original[:-10])

    def test_modified_manifest_hash_corrupt_db_future_schema_and_fk_are_rejected(self) -> None:
        self.reminder()
        package = self.backup_dir / self.service().backup().package_name

        modified_manifest = self._rewrite_package(
            package,
            "planning-20260812T080003Z-schema4-cccccccccccc.sqlite3.a8",
            mutate_manifest=lambda manifest: manifest.update({"database_sha256": "sha256:" + "0" * 64}),
        )
        with self.assertRaisesRegex(PlanningBackupVerificationError, "hash_mismatch"):
            self.verifier().verify(modified_manifest.name)

        future_schema = self._rewrite_package(
            package,
            "planning-20260812T080004Z-schema99-dddddddddddd.sqlite3.a8",
            mutate_manifest=lambda manifest: manifest.update({"schema_version": 99}),
        )
        with self.assertRaisesRegex(PlanningBackupVerificationError, "future_schema"):
            self.verifier().verify(future_schema.name)

        corrupt_db = self._rewrite_package(
            package,
            "planning-20260812T080005Z-schema4-eeeeeeeeeeee.sqlite3.a8",
            mutate_database=lambda path: path.write_bytes(b"not-a-sqlite-database"),
            refresh_hash=True,
        )
        with self.assertRaises(PlanningBackupVerificationError):
            self.verifier().verify(corrupt_db.name)

        truncated_db = self._rewrite_package(
            package,
            "planning-20260812T080006Z-schema4-ffffffffffff.sqlite3.a8",
            mutate_database=lambda path: path.write_bytes(path.read_bytes()[:-64]),
        )
        with self.assertRaisesRegex(PlanningBackupVerificationError, "hash_mismatch"):
            self.verifier().verify(truncated_db.name)

        fk_violation = self._rewrite_package(
            package,
            "planning-20260812T080007Z-schema4-111111111111.sqlite3.a8",
            mutate_database=self._insert_foreign_key_violation,
            refresh_hash=True,
            mutate_manifest=lambda manifest: manifest["table_counts"].update(
                {"delivery_attempts": manifest["table_counts"]["delivery_attempts"] + 1}
            ),
        )
        with self.assertRaisesRegex(PlanningBackupVerificationError, "foreign_key_check_failed"):
            self.verifier().verify(fk_violation.name)

    def test_failed_backup_keeps_previous_valid_artifact_and_cleans_temporary_files(self) -> None:
        self.reminder()
        service = self.service()
        previous = service.backup()

        with patch(
            "app.planning.backup._encrypt_file",
            side_effect=PlanningBackupError("encryption_write_failed", "encryption"),
        ):
            with self.assertRaisesRegex(PlanningBackupError, "encryption_write_failed"):
                service.backup()
        self.assertEqual([entry.package_name for entry in service.list_backups()], [previous.package_name])
        self.assertEqual(list(self.backup_dir.glob("*.tmp")), [])
        self.assertEqual(list(self.backup_dir.glob(".*.tmp")), [])

    def test_retention_is_bounded_and_ignores_unrelated_files(self) -> None:
        unrelated = self.backup_dir / "operator-notes.txt"
        self.backup_dir.mkdir(parents=True)
        unrelated.write_text("not a Planning backup", encoding="utf-8")
        service = self.service(retention_count=2)
        for _ in range(4):
            service.backup()
            self.clock.advance(seconds=1)
        entries = service.list_backups()
        self.assertEqual(len(entries), 2)
        self.assertTrue(unrelated.exists())
        self.assertTrue(all(entry.package_name.startswith("planning-") for entry in entries))

    def test_backup_configuration_rejects_ephemeral_production_destination_and_bad_key(self) -> None:
        with self.assertRaisesRegex(PlanningBackupConfigurationError, "backup_directory_ephemeral"):
            PlanningBackupService(
                self.database,
                backup_dir="/tmp/planning-backups",
                encryption_key=KEY,
                environment="production",
            )
        with self.assertRaisesRegex(PlanningBackupConfigurationError, "invalid_encryption_key"):
            PlanningBackupService(self.database, backup_dir=self.backup_dir, encryption_key="short")
        with self.assertRaisesRegex(PlanningBackupConfigurationError, "invalid_encryption_key"):
            PlanningBackupService(self.database, backup_dir=self.backup_dir, encryption_key=None)
        with self.assertRaisesRegex(PlanningBackupConfigurationError, "invalid_encryption_key"):
            PlanningBackupService(self.database, backup_dir=self.backup_dir, encryption_key="00" * 32)

    def test_verifier_restricts_paths_to_configured_backup_directory_and_preserves_artifact(self) -> None:
        self.reminder()
        service = self.service()
        package = self.backup_dir / service.backup().package_name
        before = package.read_bytes()
        outside = self.root / "planning-20260812T080008Z-schema4-222222222222.sqlite3.a8"
        shutil.copyfile(package, outside)
        with self.assertRaisesRegex(PlanningBackupVerificationError, "outside_configured_directory"):
            self.verifier().verify(outside)
        self.assertEqual(package.read_bytes(), before)

    def test_cli_backup_verify_list_status_and_path_boundary(self) -> None:
        self.reminder()
        environment = os.environ.copy()
        environment.update(
            {
                "PLANNING_ENV": "development",
                "PLANNING_DB_PATH": str(self.database_path),
                "PLANNING_BACKUP_DIR": str(self.backup_dir),
                "PLANNING_BACKUP_ENCRYPTION_KEY": KEY,
                "PLANNING_BACKUP_RETENTION_COUNT": "14",
            }
        )
        root = Path(__file__).parents[2]
        backup = subprocess.run(
            [sys.executable, "-m", "app.planning.backup", "backup"],
            cwd=root,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(backup.returncode, 0, backup.stderr)
        backup_payload = json.loads(backup.stdout)
        package_name = backup_payload["package"]
        listed = subprocess.run(
            [sys.executable, "-m", "app.planning.backup", "list"],
            cwd=root,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual(json.loads(listed.stdout)["backups"][0]["package"], package_name)
        status = subprocess.run(
            [sys.executable, "-m", "app.planning.backup", "status"],
            cwd=root,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(status.returncode, 0, status.stderr)
        status_payload = json.loads(status.stdout)
        self.assertEqual(status_payload["recognized_backup_count"], 1)
        self.assertNotIn(KEY, status.stdout)
        verified = subprocess.run(
            [sys.executable, "-m", "app.planning.backup", "verify", package_name],
            cwd=root,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(verified.returncode, 0, verified.stderr)
        self.assertTrue(json.loads(verified.stdout)["ok"])

        outside = self.root / "planning-20260812T080009Z-schema4-333333333333.sqlite3.a8"
        shutil.copyfile(self.backup_dir / package_name, outside)
        rejected = subprocess.run(
            [sys.executable, "-m", "app.planning.backup", "verify", str(outside)],
            cwd=root,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(rejected.returncode, 2)
        self.assertIn("backup_path_outside_configured_directory", rejected.stderr)

    def test_online_backup_during_uncommitted_external_write_is_a_transactional_snapshot(self) -> None:
        writer_ready = threading.Event()
        release_writer = threading.Event()
        writer_errors: list[BaseException] = []

        def writer() -> None:
            connection = sqlite3.connect(str(self.database_path), isolation_level=None, timeout=5)
            try:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT INTO idempotency_keys(
                        audience, key, request_hash, response_json, response_status,
                        created_at, updated_at, expires_at, correlation_id
                    ) VALUES (?, ?, ?, NULL, NULL, ?, ?, NULL, NULL)
                    """,
                    ("operator", "concurrent-fixture", "sha256:" + "a" * 64, NOW, NOW),
                )
                writer_ready.set()
                if not release_writer.wait(5):
                    raise RuntimeError("writer release barrier timed out")
                connection.commit()
            except BaseException as exc:  # pragma: no cover - assertion below reports unexpected writer failure.
                writer_errors.append(exc)
                try:
                    connection.rollback()
                except sqlite3.Error:
                    pass
            finally:
                connection.close()

        thread = threading.Thread(target=writer)
        thread.start()
        self.assertTrue(writer_ready.wait(5))
        try:
            result = self.service().backup()
        finally:
            release_writer.set()
            thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(writer_errors, [])
        self.assertEqual(self.database.connection.execute("SELECT COUNT(*) FROM idempotency_keys").fetchone()[0], 1)

        with tempfile.TemporaryDirectory() as work_name:
            work = Path(work_name)
            payload = work / "payload.zip"
            _decrypt_file(self.backup_dir / result.package_name, payload, bytes.fromhex(KEY))
            with zipfile.ZipFile(payload) as archive:
                manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(manifest["table_counts"]["idempotency_keys"], 0)
        self.assertEqual(self.verifier().verify(result.package_name).table_counts["idempotency_keys"], 0)

    def test_online_backup_allows_live_repository_commit_before_backup_finishes(self) -> None:
        self.reminder(title="A8 committed seed")
        progress_reached = threading.Event()
        release_backup = threading.Event()
        backup_finished = threading.Event()
        backup_errors: list[BaseException] = []
        result_holder: list[Any] = []

        def progress(_status: int, _remaining: int, _total: int) -> None:
            if progress_reached.is_set():
                return
            progress_reached.set()
            if not release_backup.wait(5):
                raise RuntimeError("backup release barrier timed out")

        actual_online_backup = self.database.online_backup

        def controlled_online_backup(destination: sqlite3.Connection) -> None:
            actual_online_backup(destination, pages=1, sleep=0, progress=progress)

        def backup_worker() -> None:
            try:
                result_holder.append(self.service().backup())
            except BaseException as exc:  # pragma: no cover - assertion below reports unexpected worker failure.
                backup_errors.append(exc)
            finally:
                backup_finished.set()

        with patch.object(self.database, "online_backup", side_effect=controlled_online_backup):
            thread = threading.Thread(target=backup_worker)
            thread.start()
            self.assertTrue(progress_reached.wait(5))

            # This is the real PlanningRepository path, not an independent raw
            # sqlite connection. It must commit while the backup progress hook
            # is holding the native snapshot in flight.
            self.reminder(title="A8 committed during backup")
            self.assertFalse(backup_finished.is_set())

            release_backup.set()
            thread.join(timeout=10)

        self.assertFalse(thread.is_alive())
        self.assertEqual(backup_errors, [])
        self.assertEqual(len(result_holder), 1)
        self.assertEqual(self.database.connection.execute("SELECT COUNT(*) FROM reminders").fetchone()[0], 2)
        self.assertEqual(self.database.integrity_check(), "ok")
        self.assertEqual(self.database.connection.execute("SELECT COUNT(*) FROM pragma_foreign_key_check").fetchone()[0], 0)

        package_name = result_holder[0].package_name
        restored_path = self._extract_database(self.backup_dir / package_name)
        restored = sqlite3.connect(restored_path)
        try:
            self.assertEqual(restored.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(restored.execute("SELECT COUNT(*) FROM pragma_foreign_key_check").fetchone()[0], 0)
            snapshot_counts = (
                restored.execute("SELECT COUNT(*) FROM reminders").fetchone()[0],
                restored.execute("SELECT COUNT(*) FROM outbox").fetchone()[0],
            )
            self.assertIn(snapshot_counts, {(1, 1), (2, 2)})
        finally:
            restored.close()
        self.assertTrue(self.verifier().verify(package_name).to_dict()["ok"])

    def test_restore_accepts_a2_cancelled_not_due_without_active_outbox(self) -> None:
        source_document = json.loads(
            (Path(__file__).parents[1] / "fixtures" / "planning_legacy" / "reminders_valid.json").read_text(
                encoding="utf-8"
            )
        )
        cancelled_source = self.root / "cancelled-reminder.json"
        cancelled_source.write_text(
            json.dumps(
                {
                    "settings": source_document.get("settings", {}),
                    "reminders": [
                        reminder
                        for reminder in source_document["reminders"]
                        if reminder.get("status") == "cancelled"
                    ],
                }
            ),
            encoding="utf-8",
        )
        LegacyReminderImporter(self.database).import_file(cancelled_source)
        cancelled = self.database.connection.execute(
            "SELECT status, delivery_state, deleted_at FROM reminders WHERE status = 'cancelled'"
        ).fetchone()
        self.assertEqual((cancelled["status"], cancelled["delivery_state"]), ("cancelled", "not_due"))
        self.assertIsNotNone(cancelled["deleted_at"])
        self.assertEqual(self.database.connection.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 0)

        package_name = self.service().backup().package_name
        verified = self.verifier().verify(package_name)
        self.assertTrue(verified.to_dict()["ok"])
        self.assertEqual(verified.resumable_due_jobs, 0)

    def test_restore_rejects_active_missing_outbox_relationship(self) -> None:
        reminder = self.reminder()
        with self.database.transaction():
            self.database.connection.execute("DELETE FROM outbox WHERE reminder_id = ?", (reminder.id,))

        package_name = self.service().backup().package_name
        with self.assertRaisesRegex(PlanningBackupVerificationError, "reminder_outbox_relationship"):
            self.verifier().verify(package_name)

    def test_restore_verifier_invalidates_restored_capabilities_without_touching_original(self) -> None:
        reminder = self.reminder()
        token_store = TelegramActionTokenStore(self.database, now_fn=self.clock)
        token_store.issue(
            action="reminder_complete",
            domain="reminder",
            object_id=reminder.id,
            expected_version=reminder.version,
            telegram_user_id=1,
            telegram_chat_id=42,
            now=NOW,
        )
        package = self.backup_dir / self.service().backup().package_name
        before_package = package.read_bytes()
        result = self.verifier().verify(package.name)
        self.assertEqual(result.invalidated_capabilities, 1)
        self.assertEqual(package.read_bytes(), before_package)
        self.assertIsNone(
            self.database.connection.execute(
                "SELECT consumed_at FROM telegram_action_tokens"
            ).fetchone()[0]
        )

    def test_restored_due_job_is_delivered_once_after_verification_and_restart(self) -> None:
        self.reminder()
        package = self.backup_dir / self.service().backup().package_name
        self.assertEqual(self.verifier().verify(package.name).resumable_due_jobs, 1)
        restored_path = self._extract_database(package)
        asyncio.run(self._run_restored_scheduler(restored_path))

    async def _run_restored_scheduler(self, restored_path: Path) -> None:
        restored = PlanningDatabase(restored_path)
        try:
            transport = FakeTelegramTransport()

            async def settings_provider() -> ReminderSettings:
                return ReminderSettings(notify_telegram_enabled=True, notify_iphone_enabled=False)

            scheduler = DurableReminderScheduler(
                restored,
                telegram_transport=transport,
                mobile_transport=None,
                default_chat_id=-100000000001,
                settings_provider=settings_provider,
                interval_seconds=5,
                now_fn=lambda: NOW,
                jitter_fn=lambda _base: 0,
            )
            first = await scheduler.run_once(NOW)
            self.assertEqual(first.processed_jobs, 1)
            self.assertEqual(len(transport.calls), 1)
            stored = scheduler.repository.get_reminder(
                str(restored.connection.execute("SELECT id FROM reminders").fetchone()[0])
            )
            self.assertEqual(stored.delivery_state, "delivered")
            self.assertEqual(restored.connection.execute("SELECT status FROM outbox").fetchone()[0], "succeeded")

            restored.close()
            restored = PlanningDatabase(restored_path)
            restarted_transport = FakeTelegramTransport()
            restarted = DurableReminderScheduler(
                restored,
                telegram_transport=restarted_transport,
                mobile_transport=None,
                default_chat_id=-100000000001,
                settings_provider=settings_provider,
                interval_seconds=5,
                now_fn=lambda: NOW,
                jitter_fn=lambda _base: 0,
            )
            await restarted.run_once(NOW)
            self.assertEqual(restarted_transport.calls, [])
            self.assertEqual(restored.connection.execute("SELECT status FROM outbox").fetchone()[0], "succeeded")
        finally:
            restored.close()

    def _extract_database(self, package: Path) -> Path:
        restored_path = self.root / "restored-planning.sqlite3"
        with tempfile.TemporaryDirectory() as work_name:
            work = Path(work_name)
            payload = work / "payload.zip"
            _decrypt_file(package, payload, bytes.fromhex(KEY))
            with zipfile.ZipFile(payload) as archive:
                restored_path.write_bytes(archive.read("planning.sqlite3"))
        return restored_path

    def _rewrite_package(
        self,
        source: Path,
        name: str,
        *,
        mutate_manifest: Callable[[dict[str, Any]], None] | None = None,
        mutate_database: Callable[[Path], None] | None = None,
        refresh_hash: bool = False,
    ) -> Path:
        with tempfile.TemporaryDirectory() as work_name:
            work = Path(work_name)
            payload = work / "payload.zip"
            _decrypt_file(source, payload, bytes.fromhex(KEY))
            database_path = work / "planning.sqlite3"
            with zipfile.ZipFile(payload) as archive:
                manifest = json.loads(archive.read("manifest.json"))
                database_path.write_bytes(archive.read("planning.sqlite3"))
            if mutate_database is not None:
                mutate_database(database_path)
            if refresh_hash:
                digest, size = _hash_file(database_path)
                manifest["database_sha256"] = digest
                manifest["database_size_bytes"] = size
            if mutate_manifest is not None:
                mutate_manifest(manifest)
            rewritten_payload = work / "rewritten.zip"
            _write_package_zip(rewritten_payload, database_path, manifest)
            destination = self.backup_dir / name
            _encrypt_file(rewritten_payload, destination, bytes.fromhex(KEY))
            destination.chmod(0o600)
            return destination

    @staticmethod
    def _insert_foreign_key_violation(path: Path) -> None:
        connection = sqlite3.connect(path)
        try:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute(
                """
                INSERT INTO delivery_attempts(
                    id, reminder_id, channel, attempt_number, status, started_at,
                    finished_at, error_code, error_message, provider_receipt, created_at,
                    correlation_id, delivery_cycle_id
                ) VALUES (?, ?, 'telegram', 1, 'started', ?, NULL, NULL, NULL, NULL, ?, NULL, NULL)
                """,
                (str(uuid.uuid4()), str(uuid.uuid4()), NOW, NOW),
            )
            connection.commit()
        finally:
            connection.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
