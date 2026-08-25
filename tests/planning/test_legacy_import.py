from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from app.planning import (
    PlanningDatabase,
)
from app.planning.audit import AuditWriter
from app.planning.legacy_import import (
    LegacyImportError,
    LegacyImportNotReadyError,
    LegacyReminderImporter,
    LegacySourceChangedError,
    PlanningReminderStoreAdapter,
    build_reminder_store,
)
from app.services.reminder_store import ReminderSettingsStore, ReminderStore


FIXTURE = Path(__file__).parents[1] / "fixtures" / "planning_legacy" / "reminders_valid.json"


class LegacyReminderImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.source = root / "reminders.json"
        shutil.copyfile(FIXTURE, self.source)
        self.db_path = root / "planning.sqlite3"
        self.database = PlanningDatabase(self.db_path)
        self.importer = LegacyReminderImporter(self.database)

    def tearDown(self) -> None:
        self.database.close()
        self.temp_dir.cleanup()

    def test_preflight_reports_counts_mapping_and_settings_boundary(self) -> None:
        report = self.importer.preflight(self.source)
        self.assertEqual(report.total_records, 3)
        self.assertEqual(report.valid_count, 3)
        self.assertEqual(report.invalid_count, 0)
        self.assertEqual(report.status_counts, {"pending": 1, "fired": 1, "cancelled": 1})
        self.assertEqual(report.source_counts, {"alice": 1, "telegram": 2})
        self.assertEqual(report.planning_status_counts, {"pending": 1, "completed": 1, "cancelled": 1})
        self.assertEqual(report.planning_delivery_counts, {"not_due": 2, "delivered": 1})
        self.assertEqual(report.expected_resulting_rows, 3)
        self.assertEqual(report.mapping_count, 3)
        self.assertIsNotNone(report.semantic_hash)
        self.assertTrue(report.settings_present)
        self.assertTrue(report.settings_valid)
        self.assertFalse(report.blockers)

    def test_import_preserves_fired_at_and_inferred_provenance(self) -> None:
        result = self.importer.import_file(self.source)
        self.assertEqual(result.imported_count, 3)
        fired = self.database.connection.execute(
            "SELECT * FROM reminders WHERE status = 'completed'"
        ).fetchone()
        self.assertIsNotNone(fired)
        self.assertEqual(fired["delivery_state"], "delivered")
        self.assertEqual(fired["completed_at"], "2026-08-12T07:01:02.000000Z")
        mapping = self.database.connection.execute(
            "SELECT * FROM legacy_reminder_mappings WHERE legacy_status = 'fired'"
        ).fetchone()
        self.assertEqual(mapping["legacy_fired_at"], "2026-08-12T10:01:02+03:00")
        self.assertEqual(mapping["legacy_fired_at_utc"], fired["completed_at"])
        self.assertEqual(mapping["inferred_semantics"], "legacy_delivery_inferred")
        audit = self.database.connection.execute(
            "SELECT after_json FROM audit_events WHERE action = 'legacy_import' AND object_id = ?",
            (fired["id"],),
        ).fetchone()
        audit_payload = json.loads(audit["after_json"])
        self.assertEqual(audit_payload["markers"], ["legacy_delivery_inferred"])
        self.assertEqual(audit_payload["original_fired_at"], "2026-08-12T10:01:02+03:00")
        self.assertNotIn("Synthetic fired reminder", audit["after_json"])

    def test_same_source_is_a_noop_and_mapping_ids_remain_stable(self) -> None:
        first = self.importer.import_file(self.source)
        first_mapping = {
            row["legacy_id"]: row["planning_id"]
            for row in self.database.connection.execute(
                "SELECT legacy_id, planning_id FROM legacy_reminder_mappings WHERE origin = 'legacy'"
            )
        }
        second = self.importer.import_file(self.source)
        second_mapping = {
            row["legacy_id"]: row["planning_id"]
            for row in self.database.connection.execute(
                "SELECT legacy_id, planning_id FROM legacy_reminder_mappings WHERE origin = 'legacy'"
            )
        }
        self.assertFalse(first.already_imported)
        self.assertTrue(second.already_imported)
        self.assertEqual(second.imported_count, 0)
        self.assertEqual(first_mapping, second_mapping)
        self.assertEqual(self.database.connection.execute("SELECT COUNT(*) FROM reminders").fetchone()[0], 3)

    def test_changed_source_after_completed_marker_stops(self) -> None:
        self.importer.import_file(self.source)
        document = json.loads(self.source.read_text(encoding="utf-8"))
        document["reminders"][0]["delay_seconds"] = 601
        self.source.write_text(json.dumps(document, indent=2), encoding="utf-8")
        with self.assertRaises(LegacySourceChangedError):
            self.importer.import_file(self.source)
        self.assertEqual(self.database.connection.execute("SELECT COUNT(*) FROM reminders").fetchone()[0], 3)

    def test_empty_source_imports_and_marks_complete(self) -> None:
        self.source.write_text('{"settings": {}, "reminders": []}', encoding="utf-8")
        report = self.importer.preflight(self.source)
        self.assertEqual((report.total_records, report.valid_count, report.expected_resulting_rows), (0, 0, 0))
        result = self.importer.import_file(self.source)
        self.assertEqual(result.imported_count, 0)
        self.assertEqual(self.database.connection.execute("SELECT COUNT(*) FROM reminders").fetchone()[0], 0)
        self.assertEqual(
            self.database.connection.execute("SELECT COUNT(*) FROM legacy_reminder_imports").fetchone()[0],
            1,
        )

    def test_malformed_top_level_and_record_are_blockers(self) -> None:
        self.source.write_text("[1, 2, 3]", encoding="utf-8")
        report = self.importer.preflight(self.source)
        self.assertIn("top_level_not_object", report.blockers)
        with self.assertRaises(LegacyImportError):
            self.importer.import_file(self.source)

        self.source.write_text('{"reminders": [{"id": "a1b2c3d4e5f6", "unknown": true}]}', encoding="utf-8")
        report = self.importer.preflight(self.source)
        self.assertEqual(report.invalid_count, 1)
        self.assertIn("record_validation", report.blockers)

    def test_unknown_state_duplicate_id_and_naive_time_are_not_skipped(self) -> None:
        base = json.loads(self.source.read_text(encoding="utf-8"))["reminders"][0]
        unknown = dict(base)
        unknown["status"] = "mystery"
        self.source.write_text(json.dumps({"reminders": [unknown]}), encoding="utf-8")
        report = self.importer.preflight(self.source)
        self.assertEqual(report.invalid_count, 1)
        self.assertIn("record_validation", report.blockers)

        duplicate = dict(base)
        self.source.write_text(json.dumps({"reminders": [base, duplicate]}), encoding="utf-8")
        report = self.importer.preflight(self.source)
        self.assertEqual(report.duplicate_legacy_ids, (base["id"],))
        self.assertEqual(report.invalid_count, 2)
        self.assertIn("duplicate_legacy_id", report.blockers)

        naive = dict(base)
        naive["due_at"] = "2026-08-12T10:00:00"
        self.source.write_text(json.dumps({"reminders": [naive]}), encoding="utf-8")
        report = self.importer.preflight(self.source)
        self.assertIn("timezone_naive_due_at", report.timestamp_problems)
        self.assertIn("record_validation", report.blockers)

    def test_semantically_duplicate_records_with_distinct_ids_are_preserved(self) -> None:
        base = json.loads(self.source.read_text(encoding="utf-8"))["reminders"][0]
        duplicate = dict(base)
        duplicate["id"] = "d1e2f3a4b5c6"
        self.source.write_text(json.dumps({"reminders": [base, duplicate]}), encoding="utf-8")
        report = self.importer.preflight(self.source)
        self.assertEqual(report.semantic_duplicate_count, 1)
        self.assertFalse(report.blockers)
        result = self.importer.import_file(self.source)
        self.assertEqual(result.imported_count, 2)
        self.assertEqual(self.database.connection.execute("SELECT COUNT(*) FROM reminders").fetchone()[0], 2)

    def test_source_is_byte_for_byte_untouched_by_import(self) -> None:
        before = self.source.read_bytes()
        self.importer.import_file(self.source)
        self.assertEqual(self.source.read_bytes(), before)

    def test_audit_failure_rolls_back_rows_mappings_and_marker(self) -> None:
        failing = LegacyReminderImporter(self.database, audit=AuditWriter(self.database, fail=True))
        with self.assertRaises(RuntimeError):
            failing.import_file(self.source)
        self.assertEqual(self.database.connection.execute("SELECT COUNT(*) FROM reminders").fetchone()[0], 0)
        self.assertEqual(
            self.database.connection.execute("SELECT COUNT(*) FROM legacy_reminder_mappings").fetchone()[0], 0
        )
        self.assertEqual(
            self.database.connection.execute("SELECT COUNT(*) FROM legacy_reminder_imports").fetchone()[0], 0
        )
        self.assertEqual(self.database.connection.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0], 0)

    def test_close_reopen_and_integrity_check_preserve_import(self) -> None:
        self.importer.import_file(self.source)
        self.database.close()
        self.database = PlanningDatabase(self.db_path)
        self.assertEqual(self.database.integrity_check(), "ok")
        self.assertEqual(self.database.schema_version(), 6)
        self.assertEqual(self.database.connection.execute("SELECT COUNT(*) FROM reminders").fetchone()[0], 3)

    def test_cutover_gate_requires_marker_and_disabled_mode_keeps_legacy_store(self) -> None:
        legacy_store, no_database = build_reminder_store(
            reminders_state_path=str(self.source),
            planning_db_path=str(self.db_path),
            cutover_enabled=False,
        )
        self.assertIsInstance(legacy_store, ReminderStore)
        self.assertIsNone(no_database)
        with self.assertRaises(LegacyImportNotReadyError):
            build_reminder_store(
                reminders_state_path=str(self.source),
                planning_db_path=str(Path(self.temp_dir.name) / "unimported.sqlite3"),
                cutover_enabled=True,
            )
        self.importer.import_file(self.source)
        enabled_store, enabled_database = build_reminder_store(
            reminders_state_path=str(self.source),
            planning_db_path=str(self.db_path),
            cutover_enabled=True,
        )
        try:
            self.assertIsInstance(enabled_store, PlanningReminderStoreAdapter)
            self.assertIsNotNone(enabled_database)
        finally:
            assert enabled_database is not None
            enabled_database.close()

    def test_cutover_disabled_preserves_legacy_json_record_behavior(self) -> None:
        legacy_path = Path(self.temp_dir.name) / "legacy-only.json"
        legacy_path.write_text('{"settings": {}, "reminders": []}', encoding="utf-8")
        store, database = build_reminder_store(
            reminders_state_path=str(legacy_path),
            planning_db_path=str(Path(self.temp_dir.name) / "disabled.sqlite3"),
            cutover_enabled=False,
        )
        self.assertIsInstance(store, ReminderStore)
        self.assertIsNone(database)
        created = asyncio.run(
            store.create(
                text="Synthetic legacy-only reminder",
                due_at=datetime(2026, 8, 12, 13, 0, tzinfo=timezone.utc),
                delay_seconds=60,
                source="telegram",
                chat_id=-100000000005,
            )
        )
        self.assertEqual(len(asyncio.run(store.list_pending())), 1)
        self.assertTrue(asyncio.run(store.cancel(created.id)))
        self.assertEqual(asyncio.run(store.get(created.id)).status, "cancelled")

    def test_adapter_create_list_cancel_restart_and_native_delivery_stays_active(self) -> None:
        self.importer.import_file(self.source)
        adapter = PlanningReminderStoreAdapter(
            self.database,
            ReminderSettingsStore(str(self.source)),
        )
        imported_pending = asyncio.run(adapter.list_pending())
        self.assertIn("a1b2c3d4e5f6", {reminder.id for reminder in imported_pending})
        self.assertEqual(asyncio.run(adapter.get("a1b2c3d4e5f6")).status, "pending")
        created = asyncio.run(
            adapter.create(
                text="Synthetic native reminder",
                due_at=datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc),
                delay_seconds=120,
                source="telegram",
                chat_id=-100000000004,
            )
        )
        listed = asyncio.run(adapter.list_pending())
        self.assertIn(created.id, {reminder.id for reminder in listed})
        asyncio.run(adapter.mark_fired(created.id))
        native = self.database.connection.execute(
            "SELECT status, delivery_state, completed_at FROM reminders WHERE id = ?", (created.id,)
        ).fetchone()
        self.assertEqual(
            (native["status"], native["delivery_state"], native["completed_at"]),
            ("pending", "delivered", None),
        )
        visible_after_delivery = asyncio.run(adapter.get(created.id))
        self.assertIsNotNone(visible_after_delivery)
        self.assertEqual(visible_after_delivery.status, "fired")
        self.assertTrue(asyncio.run(adapter.cancel(created.id)))
        self.assertEqual(
            self.database.connection.execute("SELECT status FROM reminders WHERE id = ?", (created.id,)).fetchone()[0],
            "cancelled",
        )

        self.database.close()
        self.database = PlanningDatabase(self.db_path)
        reopened_adapter = PlanningReminderStoreAdapter(
            self.database,
            ReminderSettingsStore(str(self.source)),
        )
        self.assertTrue(asyncio.run(reopened_adapter.list_pending()))

    def test_settings_remain_legacy_and_settings_only_change_does_not_change_semantics(self) -> None:
        self.importer.import_file(self.source)
        settings_store = ReminderSettingsStore(str(self.source))
        current = asyncio.run(settings_store.get_settings())
        self.assertTrue(current.voice_enabled)
        asyncio.run(settings_store.update_settings(voice_enabled=False))
        document = json.loads(self.source.read_text(encoding="utf-8"))
        self.assertFalse(document["settings"]["voice_enabled"])
        self.assertEqual(len(document["reminders"]), 3)
        marker = self.importer.require_cutover_ready(self.source)
        self.assertEqual(marker["status"], "completed")


if __name__ == "__main__":
    unittest.main()
