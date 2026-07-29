from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.services.app_state import AppStateStore
from app.services.coffee_timing_policy import (
    COFFEE_LONG_RUNNING_HELPER,
    COFFEE_WARMUP_HELPER,
    CoffeeTimingPolicyService,
    TimingPolicyError,
)
from app.services.home_assistant import HomeAssistantError


class FakeHomeAssistant:
    def __init__(self, warmup: str = "13", long_running: str = "60") -> None:
        self.states = {
            COFFEE_WARMUP_HELPER: {
                "state": warmup,
                "last_updated": "2026-07-29T10:00:00Z",
            },
            COFFEE_LONG_RUNNING_HELPER: {
                "state": long_running,
                "last_updated": "2026-07-29T10:00:00Z",
            },
        }
        self.calls: list[tuple[str, str, dict]] = []
        self.available = True

    async def get_state(self, entity_id: str) -> dict | None:
        if not self.available:
            raise HomeAssistantError("offline")
        return self.states.get(entity_id)

    async def call_service(self, domain: str, service: str, payload: dict) -> None:
        if not self.available:
            raise HomeAssistantError("offline")
        self.calls.append((domain, service, payload))
        self.states[payload["entity_id"]]["state"] = str(payload["value"])


class CoffeeTimingPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_reads_and_verifies_helpers(self) -> None:
        ha = FakeHomeAssistant()
        service = CoffeeTimingPolicyService(ha)

        policy = await service.refresh()
        self.assertEqual(policy.warmup_duration_seconds, 13 * 60)
        self.assertEqual(policy.long_running_threshold_seconds, 60 * 60)

        await service.set_warmup_duration_seconds(17 * 60)
        self.assertEqual(service.warmup_duration_seconds, 17 * 60)
        self.assertEqual(
            ha.calls[-1],
            (
                "input_number",
                "set_value",
                {"entity_id": COFFEE_WARMUP_HELPER, "value": 17},
            ),
        )

    async def test_ha_outage_does_not_create_or_write_a_default(self) -> None:
        ha = FakeHomeAssistant()
        ha.available = False
        service = CoffeeTimingPolicyService(ha)

        with self.assertRaises(HomeAssistantError):
            await service.refresh()
        self.assertIsNone(service.policy)
        self.assertEqual(ha.calls, [])

    async def test_migration_is_dry_run_then_idempotent(self) -> None:
        ha = FakeHomeAssistant()
        service = CoffeeTimingPolicyService(ha)
        with tempfile.TemporaryDirectory() as directory:
            state = AppStateStore(str(Path(directory) / "state.json"))
            await state.set_coffee_warmed_up_alert_delay_seconds(17 * 60)

            dry_run = await service.migrate_legacy(state)
            self.assertEqual(dry_run.status, "dry_run")
            self.assertEqual(ha.calls, [])

            applied = await service.migrate_legacy(state, apply=True)
            self.assertEqual(applied.status, "migrated")
            self.assertEqual(service.warmup_duration_seconds, 17 * 60)

            repeated = await service.migrate_legacy(state, apply=True)
            self.assertEqual(repeated.status, "already_migrated")

    async def test_migration_never_overwrites_non_default_ha_values(self) -> None:
        ha = FakeHomeAssistant(warmup="21")
        service = CoffeeTimingPolicyService(ha)
        with tempfile.TemporaryDirectory() as directory:
            state = AppStateStore(str(Path(directory) / "state.json"))
            await state.set_coffee_warmed_up_alert_delay_seconds(17 * 60)

            result = await service.migrate_legacy(state, apply=True)
            self.assertEqual(result.status, "skipped")
            self.assertEqual(ha.calls, [])

    async def test_invalid_helper_is_rejected(self) -> None:
        service = CoffeeTimingPolicyService(FakeHomeAssistant(warmup="unavailable"))
        with self.assertRaises(TimingPolicyError):
            await service.refresh()
