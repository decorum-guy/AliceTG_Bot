from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from app.config import Settings


BASELINE = {
    "TELEGRAM_BOT_TOKEN": "bot",
    "TELEGRAM_WEBHOOK_SECRET": "webhook",
    "TELEGRAM_ALLOWED_USER_IDS": "1",
    "TELEGRAM_ADMIN_CHAT_ID": "42",
    "HA_LONG_LIVED_TOKEN": "ha",
    "INTERNAL_WEBHOOK_SECRET": "internal",
}


class PlanningBackupConfigA8Tests(unittest.TestCase):
    def test_backup_settings_are_disabled_by_default_and_have_bounded_defaults(self) -> None:
        with patch.dict(os.environ, BASELINE, clear=True):
            settings = Settings.from_env()
        self.assertFalse(settings.planning_backup_enabled)
        self.assertEqual(settings.planning_backup_retention_count, 14)
        self.assertEqual(settings.planning_backup_interval_seconds, 86_400)
        self.assertEqual(settings.planning_backup_dir, "/app/data/backups/planning")
        self.assertFalse(settings.planning_icloud_enabled)
        self.assertEqual(settings.planning_icloud_account, "")
        self.assertEqual(settings.planning_icloud_password, "")
        self.assertEqual(settings.planning_icloud_caldav_url, "")
        self.assertEqual(settings.planning_icloud_refresh_interval_seconds, 300)

    def test_icloud_refresh_interval_is_bounded(self) -> None:
        for value in ("59", "3601"):
            with self.subTest(value=value), patch.dict(
                os.environ,
                {**BASELINE, "PLANNING_ICLOUD_REFRESH_INTERVAL_SECONDS": value},
                clear=True,
            ):
                with self.assertRaisesRegex(RuntimeError, "PLANNING_ICLOUD_REFRESH_INTERVAL_SECONDS"):
                    Settings.from_env()

    def test_production_backup_rejects_ephemeral_directory(self) -> None:
        values = {
            **BASELINE,
            "PLANNING_ENV": "production",
            "PLANNING_BACKUP_ENABLED": "true",
            "PLANNING_BACKUP_DIR": "/tmp/planning-backups",
        }
        with patch.dict(os.environ, values, clear=True):
            with self.assertRaisesRegex(RuntimeError, "persisted PLANNING_BACKUP_DIR"):
                Settings.from_env()

    def test_backup_secret_must_not_reuse_existing_secret(self) -> None:
        values = {
            **BASELINE,
            "PLANNING_BACKUP_ENABLED": "true",
            "PLANNING_BACKUP_ENCRYPTION_KEY": "internal",
        }
        with patch.dict(os.environ, values, clear=True):
            with self.assertRaisesRegex(RuntimeError, "dedicated secret"):
                Settings.from_env()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
