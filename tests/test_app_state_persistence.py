from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.app_state import AppStatePersistenceError, AppStateStore


class AppStatePersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.json"
        self.store = AppStateStore(str(self.path))
        await self.store.ensure_coffee_notification_metadata()

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    async def test_effective_change_is_durable_and_aba_has_unique_revision(self) -> None:
        revision_a = self.store.coffee_notification_revision()
        updated_a = self.store.coffee_notification_settings_updated_at

        _, revision_b, changed = await self.store.patch_coffee_notification_settings(
            expected_revision=revision_a,
            values={"warmup.channels.telegram": False},
        )
        self.assertTrue(changed)
        self.assertNotEqual(revision_b, revision_a)
        self.assertNotEqual(
            self.store.coffee_notification_settings_updated_at,
            updated_a,
        )

        _, revision_c, changed = await self.store.patch_coffee_notification_settings(
            expected_revision=revision_b,
            values={"warmup.channels.telegram": True},
        )
        self.assertTrue(changed)
        self.assertNotIn(revision_c, {revision_a, revision_b})

        restarted = AppStateStore(str(self.path))
        self.assertEqual(restarted.coffee_notification_revision(), revision_c)
        self.assertEqual(
            restarted.coffee_notification_settings_updated_at,
            self.store.coffee_notification_settings_updated_at,
        )
        self.assertTrue(restarted.coffee_warmed_up_notify_telegram)

    async def test_unchanged_patch_preserves_revision_and_updated_at(self) -> None:
        revision = self.store.coffee_notification_revision()
        updated_at = self.store.coffee_notification_settings_updated_at
        _, result_revision, changed = await self.store.patch_coffee_notification_settings(
            expected_revision=revision,
            values={"warmup.channels.telegram": True},
        )
        self.assertFalse(changed)
        self.assertEqual(result_revision, revision)
        self.assertEqual(
            self.store.coffee_notification_settings_updated_at,
            updated_at,
        )

    async def test_telegram_setters_share_durable_revision_and_aba_semantics(
        self,
    ) -> None:
        revision_a = self.store.coffee_notification_revision()
        updated_a = self.store.coffee_notification_settings_updated_at

        changed = await self.store.set_coffee_warmed_up_alert_enabled(False)
        revision_b = self.store.coffee_notification_revision()
        updated_b = self.store.coffee_notification_settings_updated_at
        self.assertTrue(changed)
        self.assertNotEqual(revision_b, revision_a)
        self.assertNotEqual(updated_b, updated_a)

        changed = await self.store.set_coffee_warmed_up_alert_enabled(False)
        self.assertFalse(changed)
        self.assertEqual(self.store.coffee_notification_revision(), revision_b)
        self.assertEqual(
            self.store.coffee_notification_settings_updated_at,
            updated_b,
        )

        changed = await self.store.set_coffee_warmed_up_alert_enabled(True)
        revision_c = self.store.coffee_notification_revision()
        self.assertTrue(changed)
        self.assertNotIn(revision_c, {revision_a, revision_b})

        channel_revision = revision_c
        changed = await self.store.set_coffee_long_running_notify_iphone(False)
        self.assertTrue(changed)
        self.assertNotEqual(
            self.store.coffee_notification_revision(),
            channel_revision,
        )

    async def test_legacy_state_gets_metadata_without_changing_values(self) -> None:
        legacy_path = Path(self.temp.name) / "legacy.json"
        legacy_path.write_text(
            json.dumps(
                {
                    "coffee_warmed_up_alert_enabled": False,
                    "coffee_warmed_up_notify_telegram": False,
                }
            ),
            encoding="utf-8",
        )
        legacy = AppStateStore(str(legacy_path))
        self.assertFalse(legacy.coffee_warmed_up_alert_enabled)
        self.assertFalse(legacy.coffee_warmed_up_notify_telegram)
        await legacy.ensure_coffee_notification_metadata()

        persisted = json.loads(legacy_path.read_text(encoding="utf-8"))
        self.assertIn("coffee_notification_settings_revision", persisted)
        self.assertIn("coffee_notification_settings_updated_at", persisted)
        self.assertFalse(persisted["coffee_warmed_up_alert_enabled"])
        self.assertFalse(persisted["coffee_warmed_up_notify_telegram"])

    async def test_temporary_write_failure_keeps_memory_and_file_unchanged(self) -> None:
        original_memory = dict(self.store._state)
        original_file = self.path.read_bytes()
        revision = self.store.coffee_notification_revision()

        with patch(
            "app.services.app_state.tempfile.NamedTemporaryFile",
            side_effect=OSError("private path omitted"),
        ):
            with self.assertRaises(AppStatePersistenceError):
                await self.store.patch_coffee_notification_settings(
                    expected_revision=revision,
                    values={"warmup.channels.telegram": False},
                )

        self.assertEqual(self.store._state, original_memory)
        self.assertEqual(self.path.read_bytes(), original_file)

    async def test_atomic_replace_failure_keeps_memory_and_file_unchanged(self) -> None:
        original_memory = dict(self.store._state)
        original_file = self.path.read_bytes()
        revision = self.store.coffee_notification_revision()

        with patch(
            "app.services.app_state.os.replace",
            side_effect=OSError("private path omitted"),
        ):
            with self.assertRaises(AppStatePersistenceError):
                await self.store.patch_coffee_notification_settings(
                    expected_revision=revision,
                    values={"warmup.channels.iphone": False},
                )

        self.assertEqual(self.store._state, original_memory)
        self.assertEqual(self.path.read_bytes(), original_file)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    async def test_telegram_setter_failure_keeps_metadata_memory_and_file(
        self,
    ) -> None:
        original_memory = dict(self.store._state)
        original_file = self.path.read_bytes()
        revision = self.store.coffee_notification_revision()
        updated_at = self.store.coffee_notification_settings_updated_at

        with patch(
            "app.services.app_state.os.replace",
            side_effect=OSError("private path omitted"),
        ):
            with self.assertRaises(AppStatePersistenceError):
                await self.store.set_coffee_long_running_notify_telegram(False)

        self.assertEqual(self.store._state, original_memory)
        self.assertEqual(self.path.read_bytes(), original_file)
        self.assertEqual(self.store.coffee_notification_revision(), revision)
        self.assertEqual(
            self.store.coffee_notification_settings_updated_at,
            updated_at,
        )
