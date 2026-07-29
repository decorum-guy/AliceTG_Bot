from __future__ import annotations

import unittest
from types import SimpleNamespace

import json

from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from app.services.coffee_timing_policy import (
    COFFEE_LONG_RUNNING_HELPER,
    COFFEE_WARMUP_HELPER,
    CoffeeTimingPolicyService,
)
from app.web.internal_routes import health_details, health_live, health_ready


class FakeHomeAssistant:
    async def get_state(self, entity_id: str) -> dict | None:
        if entity_id == COFFEE_WARMUP_HELPER:
            return {"state": "13", "last_updated": "2026-07-29T10:00:00Z"}
        if entity_id == COFFEE_LONG_RUNNING_HELPER:
            return {"state": "60", "last_updated": "2026-07-29T10:00:00Z"}
        return {"state": "off", "last_updated": "2026-07-29T10:00:00Z"}


class HealthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        ha = FakeHomeAssistant()
        policy = CoffeeTimingPolicyService(ha)
        await policy.refresh()
        app = web.Application()
        app["settings"] = SimpleNamespace(
            coffee_switch_entity="switch.kofemashina",
            internal_webhook_secret="test-secret",
            app_version="1.2.3",
            app_commit="abc123",
        )
        app["ha"] = ha
        app["coffee_timing_policy"] = policy
        app["bot"] = SimpleNamespace(session=SimpleNamespace(closed=False))
        self.app = app

    async def test_live_and_ready_are_sanitized(self) -> None:
        live = await health_live(make_mocked_request("GET", "/health/live", app=self.app))
        self.assertEqual(live.status, 200)
        self.assertEqual(json.loads(live.body)["status"], "live")

        ready = await health_ready(make_mocked_request("GET", "/health/ready", app=self.app))
        payload = json.loads(ready.body)
        self.assertEqual(ready.status, 200)
        self.assertEqual(payload["timing_helpers"], "ready")
        self.assertNotIn("token", str(payload).lower())

    async def test_details_requires_existing_internal_secret(self) -> None:
        with self.assertRaises(web.HTTPUnauthorized):
            await health_details(make_mocked_request("GET", "/health/details", app=self.app))

        response = await health_details(
            make_mocked_request(
                "GET",
                "/health/details",
                app=self.app,
                headers={"X-Internal-Secret": "test-secret"},
            )
        )
        payload = json.loads(response.body)
        self.assertEqual(response.status, 200)
        self.assertEqual(payload["version"], "1.2.3")
        self.assertNotIn("test-secret", str(payload))
