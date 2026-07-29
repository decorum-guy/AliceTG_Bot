from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.handlers.coffee import (
    toggle_coffee_alert,
    toggle_coffee_alert_channel,
)
from app.services.app_state import AppStateStore


class FakeCallback:
    def __init__(self, data: str) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=1)
        self.message = None
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(
        self,
        text: str | None = None,
        *,
        show_alert: bool = False,
    ) -> None:
        self.answers.append((text, show_alert))


class SchedulerSpy:
    def __init__(self) -> None:
        self.reschedules = 0

    def reschedule_active_alerts(self) -> None:
        self.reschedules += 1


class CoffeeNotificationHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.json"
        self.state = AppStateStore(str(self.path))
        await self.state.ensure_coffee_notification_metadata()
        self.settings = SimpleNamespace(is_admin_user=lambda _: True)
        self.timing = SimpleNamespace()
        self.scheduler = SchedulerSpy()

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    async def test_telegram_toggle_updates_revision_and_reschedules_once(
        self,
    ) -> None:
        revision = self.state.coffee_notification_revision()
        updated_at = self.state.coffee_notification_settings_updated_at
        callback = FakeCallback("coffee_alert_toggle:warmed_up")

        await toggle_coffee_alert(
            callback,
            self.settings,
            self.state,
            self.scheduler,
            self.timing,
        )

        self.assertFalse(self.state.coffee_warmed_up_alert_enabled)
        self.assertNotEqual(self.state.coffee_notification_revision(), revision)
        self.assertNotEqual(
            self.state.coffee_notification_settings_updated_at,
            updated_at,
        )
        self.assertEqual(self.scheduler.reschedules, 1)
        self.assertEqual(callback.answers, [("Выключено", False)])

    async def test_telegram_channel_toggle_updates_revision(self) -> None:
        revision = self.state.coffee_notification_revision()
        callback = FakeCallback(
            "coffee_alert_channel_toggle:long_running:iphone"
        )

        await toggle_coffee_alert_channel(
            callback,
            self.settings,
            self.state,
            self.scheduler,
            self.timing,
        )

        self.assertFalse(self.state.coffee_long_running_notify_iphone)
        self.assertNotEqual(self.state.coffee_notification_revision(), revision)
        self.assertEqual(self.scheduler.reschedules, 1)

    async def test_persistence_failure_is_sanitized_and_not_rescheduled(
        self,
    ) -> None:
        callback = FakeCallback("coffee_alert_toggle:warmed_up")
        original_file = self.path.read_bytes()
        original_state = dict(self.state._state)

        with patch(
            "app.services.app_state.os.replace",
            side_effect=OSError("private path omitted"),
        ):
            await toggle_coffee_alert(
                callback,
                self.settings,
                self.state,
                self.scheduler,
                self.timing,
            )

        self.assertEqual(callback.answers, [("Не удалось сохранить настройку", True)])
        self.assertEqual(self.scheduler.reschedules, 0)
        self.assertEqual(self.state._state, original_state)
        self.assertEqual(self.path.read_bytes(), original_file)
