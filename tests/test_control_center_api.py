from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app.services.app_state import AppStateStore
from app.services.coffee_timing_policy import (
    COFFEE_LONG_RUNNING_HELPER,
    COFFEE_TIMING_INITIALIZED_HELPER,
    COFFEE_WARMUP_HELPER,
    CoffeeTimingPolicyService,
)
from app.services.control_center_coffee import ControlCenterCoffeeActions
from app.services.home_assistant import HomeAssistantError
from app.web.internal_routes import setup_internal_routes


class FakeHomeAssistant:
    def __init__(self) -> None:
        self.available = True
        self.switch_calls: list[str] = []
        self.calls: list[tuple[str, str, dict]] = []
        self.states = {
            COFFEE_WARMUP_HELPER: self._number("15", "00"),
            COFFEE_LONG_RUNNING_HELPER: self._number("60", "01", maximum=240),
            COFFEE_TIMING_INITIALIZED_HELPER: {
                "state": "on",
                "last_updated": "2026-07-29T16:00:02Z",
                "attributes": {},
            },
            "switch.kofemashina": {
                "state": "off",
                "last_changed": "2026-07-29T16:00:00Z",
                "last_updated": "2026-07-29T16:00:00Z",
                "attributes": {},
            },
        }

    @staticmethod
    def _number(value: str, suffix: str, *, maximum: int = 120) -> dict:
        return {
            "state": value,
            "last_updated": f"2026-07-29T16:00:{suffix}Z",
            "attributes": {"min": 1, "max": maximum, "step": 1},
        }

    async def get_state(self, entity_id: str) -> dict | None:
        if not self.available:
            raise HomeAssistantError("offline")
        return self.states.get(entity_id)

    async def call_service(self, domain: str, service: str, payload: dict) -> None:
        if not self.available:
            raise HomeAssistantError("offline")
        self.calls.append((domain, service, payload))
        entity_id = payload["entity_id"]
        self.states[entity_id]["state"] = str(payload["value"])
        self.states[entity_id]["last_updated"] = (
            f"2026-07-29T16:01:{len(self.calls):02d}Z"
        )

    async def switch_turn_on(self, entity_id: str) -> None:
        self.switch_calls.append("turn_on")
        self.states[entity_id]["state"] = "on"
        self.states[entity_id]["last_updated"] = "2026-07-29T16:02:00Z"

    async def switch_turn_off(self, entity_id: str) -> None:
        self.switch_calls.append("turn_off")
        self.states[entity_id]["state"] = "off"
        self.states[entity_id]["last_updated"] = "2026-07-29T16:03:00Z"


class SchedulerSpy:
    def __init__(self) -> None:
        self.reschedules = 0

    def reschedule_active_alerts(self) -> None:
        self.reschedules += 1

    async def handle_state(self, _: str) -> None:
        return None


class ControlCenterApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.ha = FakeHomeAssistant()
        self.state = AppStateStore(str(Path(self.temp.name) / "state.json"))
        self.scheduler = SchedulerSpy()
        self.settings = SimpleNamespace(
            control_center_api_token="control-center-token",
            shortcuts_secret_token="personal-shortcut-token",
            coffee_switch_entity="switch.kofemashina",
            ha_mobile_notify_services=(),
        )
        self.timing = CoffeeTimingPolicyService(self.ha)
        await self.timing.refresh()
        app = web.Application()
        app["settings"] = self.settings
        app["ha"] = self.ha
        app["app_state"] = self.state
        app["coffee_timing_policy"] = self.timing
        app["coffee_alert_scheduler"] = self.scheduler
        app["control_center_coffee_actions"] = ControlCenterCoffeeActions(
            self.ha,
            self.settings,
            confirmation_timeout_seconds=0.05,
        )
        setup_internal_routes(app)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.auth = {"Authorization": "Bearer control-center-token"}

    async def asyncTearDown(self) -> None:
        await self.client.close()
        self.temp.cleanup()

    async def test_control_center_token_is_separate_from_shortcut_token(self) -> None:
        missing = await self.client.get("/internal/control-center/coffee/timing")
        self.assertEqual(missing.status, 401)
        invalid = await self.client.get(
            "/internal/control-center/coffee/timing",
            headers={"Authorization": "Bearer personal-shortcut-token"},
        )
        self.assertEqual(invalid.status, 403)
        valid = await self.client.get(
            "/internal/control-center/coffee/timing",
            headers=self.auth,
        )
        self.assertEqual(valid.status, 200)

        shortcut_with_control_token = await self.client.post(
            "/shortcut/espresso",
            headers=self.auth,
            json={"action": "turn_on"},
        )
        self.assertEqual(shortcut_with_control_token.status, 403)
        shortcut_with_personal_token = await self.client.post(
            "/shortcut/espresso",
            headers={"Authorization": "Bearer personal-shortcut-token"},
            json={"action": "turn_on"},
        )
        self.assertEqual(shortcut_with_personal_token.status, 200)

    async def test_notification_get_patch_conflict_and_single_reschedule(self) -> None:
        response = await self.client.get(
            "/internal/notification-settings/coffee",
            headers=self.auth,
        )
        current = await response.json()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIsInstance(current["updatedAt"], str)
        self.assertNotIn("delivery", current)
        self.assertNotIn("timing", current)

        unchanged = await self.client.patch(
            "/internal/notification-settings/coffee",
            headers=self.auth,
            json={
                "expectedRevision": current["revision"],
                "warmup": {"channels": {"telegram": True}},
            },
        )
        self.assertEqual(unchanged.status, 200)
        self.assertEqual(self.scheduler.reschedules, 0)

        changed = await self.client.patch(
            "/internal/notification-settings/coffee",
            headers=self.auth,
            json={
                "expectedRevision": current["revision"],
                "warmup": {"channels": {"telegram": False}},
            },
        )
        changed_payload = await changed.json()
        self.assertEqual(changed.status, 200)
        self.assertFalse(changed_payload["warmup"]["channels"]["telegram"])
        self.assertEqual(self.scheduler.reschedules, 1)

        stale = await self.client.patch(
            "/internal/notification-settings/coffee",
            headers=self.auth,
            json={
                "expectedRevision": current["revision"],
                "longRunning": {"enabled": False},
            },
        )
        self.assertEqual(stale.status, 409)
        unknown = await self.client.patch(
            "/internal/notification-settings/coffee",
            headers=self.auth,
            json={"expectedRevision": changed_payload["revision"], "timing": 15},
        )
        self.assertEqual(unknown.status, 400)

    async def test_notification_persistence_failure_is_sanitized_and_not_rescheduled(self) -> None:
        response = await self.client.get(
            "/internal/notification-settings/coffee",
            headers=self.auth,
        )
        current = await response.json()
        with patch(
            "app.services.app_state.os.replace",
            side_effect=OSError("private path omitted"),
        ):
            failed = await self.client.patch(
                "/internal/notification-settings/coffee",
                headers=self.auth,
                json={
                    "expectedRevision": current["revision"],
                    "warmup": {"channels": {"telegram": False}},
                },
            )
        payload = await failed.json()
        self.assertEqual(failed.status, 503)
        self.assertEqual(payload["error"], "notification_settings_unavailable")
        self.assertNotIn("path", str(payload).lower())
        self.assertTrue(self.state.coffee_warmed_up_notify_telegram)
        self.assertEqual(self.scheduler.reschedules, 0)

    async def test_telegram_mutation_invalidates_control_center_revision(self) -> None:
        response = await self.client.get(
            "/internal/notification-settings/coffee",
            headers=self.auth,
        )
        current = await response.json()

        changed = await self.state.set_coffee_warmed_up_notify_telegram(False)
        self.assertTrue(changed)
        self.assertNotEqual(
            self.state.coffee_notification_revision(),
            current["revision"],
        )

        stale = await self.client.patch(
            "/internal/notification-settings/coffee",
            headers=self.auth,
            json={
                "expectedRevision": current["revision"],
                "longRunning": {"enabled": False},
            },
        )
        self.assertEqual(stale.status, 409)

    async def test_timing_get_patch_readback_conflict_bounds_and_outage(self) -> None:
        response = await self.client.get(
            "/internal/control-center/coffee/timing",
            headers=self.auth,
        )
        current = await response.json()
        self.assertEqual((current["warmupMinutes"], current["longRunningMinutes"]), (15, 60))

        updated = await self.client.patch(
            "/internal/control-center/coffee/timing",
            headers=self.auth,
            json={"expectedRevision": current["revision"], "warmupMinutes": 13},
        )
        updated_payload = await updated.json()
        self.assertEqual(updated.status, 200)
        self.assertEqual(updated_payload["warmupMinutes"], 13)
        self.assertEqual(self.ha.states[COFFEE_WARMUP_HELPER]["state"], "13")

        stale = await self.client.patch(
            "/internal/control-center/coffee/timing",
            headers=self.auth,
            json={"expectedRevision": current["revision"], "warmupMinutes": 14},
        )
        self.assertEqual(stale.status, 409)
        invalid = await self.client.patch(
            "/internal/control-center/coffee/timing",
            headers=self.auth,
            json={"expectedRevision": updated_payload["revision"], "warmupMinutes": 999},
        )
        self.assertEqual(invalid.status, 400)

        self.ha.available = False
        unavailable = await self.client.get(
            "/internal/control-center/coffee/timing",
            headers=self.auth,
        )
        self.assertEqual(unavailable.status, 503)

    async def test_action_allowlist_idempotency_and_confirmed_state(self) -> None:
        invalid = await self.client.post(
            "/internal/control-center/coffee/action",
            headers=self.auth,
            json={"action": "toggle", "requestId": "request-0001"},
        )
        self.assertEqual(invalid.status, 400)

        first = await self.client.post(
            "/internal/control-center/coffee/action",
            headers=self.auth,
            json={"action": "turn_on", "requestId": "request-0001"},
        )
        first_payload = await first.json()
        self.assertEqual(first.status, 200)
        self.assertEqual(first_payload["confirmedState"], "on")
        self.assertEqual(self.ha.switch_calls, ["turn_on"])

        duplicate = await self.client.post(
            "/internal/control-center/coffee/action",
            headers=self.auth,
            json={"action": "turn_on", "requestId": "request-0001"},
        )
        self.assertEqual(duplicate.status, 200)
        self.assertEqual(self.ha.switch_calls, ["turn_on"])

        already_on = await self.client.post(
            "/internal/control-center/coffee/action",
            headers=self.auth,
            json={"action": "turn_on", "requestId": "request-0002"},
        )
        self.assertTrue((await already_on.json())["alreadyInState"])
        self.assertEqual(self.ha.switch_calls, ["turn_on"])

        off = await self.client.post(
            "/internal/control-center/coffee/action",
            headers=self.auth,
            json={"action": "turn_off", "requestId": "request-0003"},
        )
        self.assertEqual((await off.json())["confirmedState"], "off")
        self.assertEqual(self.ha.switch_calls, ["turn_on", "turn_off"])
