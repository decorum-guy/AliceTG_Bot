from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

import json

from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from app.services.coffee_timing_policy import (
    COFFEE_LONG_RUNNING_HELPER,
    COFFEE_TIMING_INITIALIZED_HELPER,
    COFFEE_WARMUP_HELPER,
    CoffeeTimingPolicyService,
)
from app.services.home_assistant import HomeAssistantError
from app.web.internal_routes import health_details, health_live, health_ready


class FakeHomeAssistant:
    def __init__(self, *, initialized: bool = True, available: bool = True) -> None:
        self.initialized = initialized
        self.available = available

    async def get_state(self, entity_id: str) -> dict | None:
        if not self.available:
            raise HomeAssistantError("offline")
        if entity_id == COFFEE_WARMUP_HELPER:
            return {"state": "13", "last_updated": "2026-07-29T10:00:00Z"}
        if entity_id == COFFEE_LONG_RUNNING_HELPER:
            return {"state": "60", "last_updated": "2026-07-29T10:00:00Z"}
        if entity_id == COFFEE_TIMING_INITIALIZED_HELPER:
            return {
                "state": "on" if self.initialized else "off",
                "last_updated": "2026-07-29T10:00:00Z",
            }
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

    async def test_ready_requires_fresh_initialized_timing(self) -> None:
        self.app["coffee_timing_policy"]._stale_after_seconds = 0.001
        await asyncio.sleep(0.002)
        # Ready actively refreshes canonical timing, so a healthy HA becomes fresh.
        ready = await health_ready(make_mocked_request("GET", "/health/ready", app=self.app))
        self.assertEqual(ready.status, 200)

        self.app["ha"].initialized = False
        not_initialized = await health_ready(
            make_mocked_request("GET", "/health/ready", app=self.app)
        )
        self.assertEqual(not_initialized.status, 503)
        self.assertEqual(
            json.loads(not_initialized.body)["timing_helpers"],
            "not_ready",
        )

    async def test_stale_or_cached_policy_is_not_ready_when_refresh_fails(self) -> None:
        policy = self.app["coffee_timing_policy"]
        policy._stale_after_seconds = 0.001
        await asyncio.sleep(0.002)
        self.app["ha"].available = False
        not_ready = await health_ready(
            make_mocked_request("GET", "/health/ready", app=self.app)
        )
        self.assertEqual(not_ready.status, 503)
        payload = json.loads(not_ready.body)
        self.assertEqual(payload["home_assistant"], "not_ready")
        self.assertEqual(payload["timing_helpers"], "not_ready")

        live = await health_live(make_mocked_request("GET", "/health/live", app=self.app))
        self.assertEqual(live.status, 200)
