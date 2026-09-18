from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.services.app_state import AppStateStore
from app.services.coffee_alerts import CoffeeAlertScheduler
from app.services.coffee_timing_policy import COFFEE_LAST_TURNED_ON_HELPER
from app.services.home_assistant import HomeAssistantError
from app.services.pushward_widgets import build_coffee_widget_content


CANONICAL_TIMESTAMP = 1_789_720_607.026817
CANONICAL_ON_SINCE = "2026-09-18T08:36:47.026817+00:00"
OLDER_ON_SINCE = "2026-09-18T08:30:47+00:00"


class FakeHomeAssistant:
    def __init__(self, *, switch_state: str = "on", timestamp: float = CANONICAL_TIMESTAMP) -> None:
        self.available = True
        self.states = {
            "switch.kofemashina": {"state": switch_state},
            COFFEE_LAST_TURNED_ON_HELPER: {
                "state": "2026-09-18 11:36:47",
                "attributes": {"timestamp": timestamp},
            },
        }

    async def get_state(self, entity_id: str) -> dict | None:
        if not self.available:
            raise HomeAssistantError("unavailable")
        return self.states.get(entity_id)


class ActivitySpy:
    def __init__(self) -> None:
        self.starts = 0
        self.stops = 0

    async def start_or_restore(self) -> None:
        self.starts += 1

    async def stop(self) -> None:
        self.stops += 1


class WidgetSpy:
    def __init__(self) -> None:
        self.starts = 0
        self.stops = 0

    def start(self) -> None:
        self.starts += 1

    def stop(self) -> None:
        self.stops += 1


class CoffeeCanonicalActivationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.json"
        self.state = AppStateStore(str(self.path))
        self.ha = FakeHomeAssistant()
        self.activity = ActivitySpy()
        self.widget = WidgetSpy()
        self.scheduler = self._scheduler(self.state)

    async def asyncTearDown(self) -> None:
        self.scheduler._cancel_tasks()
        self.temp.cleanup()

    def _scheduler(self, state: AppStateStore) -> CoffeeAlertScheduler:
        return CoffeeAlertScheduler(
            SimpleNamespace(coffee_switch_entity="switch.kofemashina", coffee_warmup_gif_url=""),
            state,
            SimpleNamespace(),
            self.ha,
            SimpleNamespace(warmup_duration_seconds=None, long_running_threshold_seconds=None),
            self.activity,
            self.widget,
        )

    async def test_normal_off_to_on_uses_ha_canonical_activation(self) -> None:
        await self.scheduler.handle_state("on", changed_at="2026-09-18T08:30:47Z")

        self.assertEqual(self.state.coffee_machine_state, "on")
        self.assertEqual(self.state.coffee_on_since, CANONICAL_ON_SINCE)
        self.assertEqual(self.activity.starts, 1)
        self.assertEqual(self.widget.starts, 1)

    async def test_duplicate_on_same_cycle_preserves_timestamp_and_alert_idempotency(self) -> None:
        await self.scheduler.handle_state("on")
        await self.state.mark_coffee_warmed_up_alert_sent()
        await self.state.mark_coffee_long_running_alert_sent()

        await self.scheduler.handle_state("on", changed_at="2026-09-18T09:00:00Z")

        self.assertEqual(self.state.coffee_on_since, CANONICAL_ON_SINCE)
        self.assertTrue(self.state.coffee_warmed_up_alert_sent)
        self.assertTrue(self.state.coffee_long_running_alert_sent)

    async def test_stale_persisted_on_cycle_reconciles_to_new_ha_activation(self) -> None:
        await self.state.mark_coffee_machine_on(OLDER_ON_SINCE)
        await self.state.mark_coffee_warmed_up_alert_sent()

        await self.scheduler.handle_state("on")

        self.assertEqual(self.state.coffee_on_since, CANONICAL_ON_SINCE)
        self.assertFalse(self.state.coffee_warmed_up_alert_sent)

    async def test_restart_during_active_cycle_retains_canonical_elapsed_start(self) -> None:
        await self.scheduler.handle_state("on")
        restarted_state = AppStateStore(str(self.path))
        restarted_scheduler = self._scheduler(restarted_state)
        try:
            await restarted_scheduler.restore()
            self.assertEqual(restarted_state.coffee_on_since, CANONICAL_ON_SINCE)
        finally:
            restarted_scheduler._cancel_tasks()

    async def test_ha_unavailable_never_invents_new_activation_time(self) -> None:
        self.ha.available = False
        await self.scheduler.handle_state("on", changed_at="2026-09-18T08:30:47Z")
        self.assertEqual(self.state.coffee_machine_state, "off")
        self.assertIsNone(self.state.coffee_on_since)

        self.ha.available = True
        await self.scheduler.handle_state("on")
        self.ha.available = False
        await self.scheduler.handle_state("on", changed_at="2026-09-18T09:00:00Z")
        self.assertEqual(self.state.coffee_on_since, CANONICAL_ON_SINCE)

    async def test_restore_clears_stale_local_cycle_when_ha_is_off(self) -> None:
        await self.state.mark_coffee_machine_on(OLDER_ON_SINCE)
        self.ha.states["switch.kofemashina"] = {"state": "off"}

        await self.scheduler.restore()

        self.assertEqual(self.state.coffee_machine_state, "off")
        self.assertIsNone(self.state.coffee_on_since)
        self.assertEqual(self.activity.stops, 1)
        self.assertEqual(self.widget.stops, 1)

    def test_pushward_widget_progress_uses_the_reconciled_canonical_start(self) -> None:
        self.state._state["coffee_machine_state"] = "on"
        self.state._state["coffee_on_since"] = CANONICAL_ON_SINCE
        policy = SimpleNamespace(warmup_duration_seconds=900, long_running_threshold_seconds=3600)

        with patch("app.services.pushward_widgets._elapsed_seconds", return_value=600):
            content = build_coffee_widget_content(self.state, policy)

        self.assertEqual(content["stat_rows"][1]["value"], "10 мин")

