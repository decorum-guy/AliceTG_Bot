from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from app.services.app_state import AppStateStore
from app.services.coffee_timing_policy import (
    COFFEE_LONG_RUNNING_HELPER,
    COFFEE_TIMING_INITIALIZED_HELPER,
    COFFEE_WARMUP_HELPER,
    CoffeeTimingPolicyRefresher,
    CoffeeTimingPolicyService,
    TimingPartialUpdateError,
    TimingPolicyError,
    TimingRevisionConflict,
    TimingStateUnknownError,
)
from app.services.home_assistant import HomeAssistantError


class FakeHomeAssistant:
    def __init__(
        self,
        warmup: str = "13",
        long_running: str = "60",
        *,
        initialized: bool = True,
    ) -> None:
        self.states = {
            COFFEE_WARMUP_HELPER: {
                "state": warmup,
                "last_updated": "2026-07-29T10:00:00Z",
                "attributes": {"min": 1, "max": 120, "step": 1},
            },
            COFFEE_LONG_RUNNING_HELPER: {
                "state": long_running,
                "last_updated": "2026-07-29T10:00:00Z",
                "attributes": {"min": 1, "max": 240, "step": 1},
            },
            COFFEE_TIMING_INITIALIZED_HELPER: {
                "state": "on" if initialized else "off",
                "last_updated": "2026-07-29T10:00:00Z",
            },
        }
        self.calls: list[tuple[str, str, dict]] = []
        self.available = True
        self.active_reads = 0
        self.max_active_reads = 0
        self.fail_write_numbers: set[int] = set()
        self.ignore_write_numbers: set[int] = set()
        self.write_started = asyncio.Event()
        self.release_first_write: asyncio.Event | None = None
        self.go_offline_on_write_failure = False

    async def get_state(self, entity_id: str) -> dict | None:
        if not self.available:
            raise HomeAssistantError("offline")
        self.active_reads += 1
        self.max_active_reads = max(self.max_active_reads, self.active_reads)
        try:
            await asyncio.sleep(0)
            return self.states.get(entity_id)
        finally:
            self.active_reads -= 1

    async def call_service(self, domain: str, service: str, payload: dict) -> None:
        if not self.available:
            raise HomeAssistantError("offline")
        self.calls.append((domain, service, payload))
        call_number = len(self.calls)
        if call_number == 1 and self.release_first_write is not None:
            self.write_started.set()
            await self.release_first_write.wait()
        if call_number in self.fail_write_numbers:
            if self.go_offline_on_write_failure:
                self.available = False
            raise HomeAssistantError("write failed")
        entity_id = payload["entity_id"]
        if call_number in self.ignore_write_numbers:
            return
        if domain == "input_boolean":
            self.states[entity_id]["state"] = "on"
        else:
            self.states[entity_id]["state"] = str(payload["value"])
        self.states[entity_id]["last_updated"] = (
            f"2026-07-29T10:00:{len(self.calls):02d}Z"
        )


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
        ha = FakeHomeAssistant(initialized=False)
        ha.available = False
        service = CoffeeTimingPolicyService(ha)

        with self.assertRaises(HomeAssistantError):
            await service.refresh()
        self.assertIsNone(service.policy)
        self.assertEqual(ha.calls, [])

    async def test_migration_is_dry_run_then_idempotent(self) -> None:
        ha = FakeHomeAssistant(initialized=False)
        service = CoffeeTimingPolicyService(ha)
        with tempfile.TemporaryDirectory() as directory:
            state = AppStateStore(str(Path(directory) / "state.json"))
            await state.set_coffee_warmed_up_alert_delay_seconds(17 * 60)

            dry_run = await service.migrate_legacy(state)
            self.assertEqual(dry_run.status, "dry_run")
            self.assertEqual(ha.calls, [])

            applied = await service.migrate_legacy(state, apply=True)
            self.assertEqual(applied.status, "initialized")
            self.assertEqual(service.warmup_duration_seconds, 17 * 60)

            repeated = await service.migrate_legacy(state, apply=True)
            self.assertEqual(repeated.status, "already_initialized")

    async def test_migration_never_overwrites_non_default_ha_values(self) -> None:
        ha = FakeHomeAssistant(warmup="21", initialized=False)
        service = CoffeeTimingPolicyService(ha)
        with tempfile.TemporaryDirectory() as directory:
            state = AppStateStore(str(Path(directory) / "state.json"))

            result = await service.migrate_legacy(state, apply=True)
            self.assertEqual(result.status, "initialized")
            number_calls = [call for call in ha.calls if call[0] == "input_number"]
            self.assertEqual(number_calls, [])
            self.assertEqual(result.warmup_duration_seconds, 21 * 60)

    async def test_first_initialization_uses_defaults_and_sets_marker_last(self) -> None:
        ha = FakeHomeAssistant(warmup="1", long_running="1", initialized=False)
        service = CoffeeTimingPolicyService(ha)
        with tempfile.TemporaryDirectory() as directory:
            state = AppStateStore(str(Path(directory) / "state.json"))
            result = await service.migrate_legacy(state, apply=True)

        self.assertEqual(result.warmup_duration_seconds, 13 * 60)
        self.assertEqual(result.long_running_threshold_seconds, 60 * 60)
        self.assertEqual(ha.calls[-1][0:2], ("input_boolean", "turn_on"))
        self.assertEqual(
            ha.calls[-1][2]["entity_id"],
            COFFEE_TIMING_INITIALIZED_HELPER,
        )

    async def test_invalid_helper_is_rejected(self) -> None:
        service = CoffeeTimingPolicyService(FakeHomeAssistant(warmup="unavailable"))
        with self.assertRaises(TimingPolicyError):
            await service.refresh()

    async def test_patch_requires_initialized_marker(self) -> None:
        ha = FakeHomeAssistant(initialized=False)
        service = CoffeeTimingPolicyService(ha)
        current, _ = await service._read_policy_with_states()
        with self.assertRaises(TimingPolicyError):
            await service.patch_minutes(
                expected_revision=current.revision,
                warmup_minutes=15,
            )
        self.assertEqual(ha.calls, [])

    async def test_concurrent_same_revision_allows_one_writer(self) -> None:
        ha = FakeHomeAssistant()
        ha.release_first_write = asyncio.Event()
        service = CoffeeTimingPolicyService(ha)
        current = await service.refresh()

        first = asyncio.create_task(
            service.patch_minutes(
                expected_revision=current.revision,
                warmup_minutes=15,
            )
        )
        await ha.write_started.wait()
        second = asyncio.create_task(
            service.patch_minutes(
                expected_revision=current.revision,
                long_running_minutes=61,
            )
        )
        await asyncio.sleep(0)
        ha.release_first_write.set()

        result = await first
        self.assertEqual(result.warmup_duration_seconds, 15 * 60)
        with self.assertRaises(TimingRevisionConflict):
            await second
        self.assertEqual(len(ha.calls), 1)

    async def test_partial_failure_rolls_back_and_confirms_original_pair(self) -> None:
        ha = FakeHomeAssistant()
        ha.fail_write_numbers = {2}
        service = CoffeeTimingPolicyService(ha)
        current = await service.refresh()

        with self.assertRaises(TimingPartialUpdateError):
            await service.patch_minutes(
                expected_revision=current.revision,
                warmup_minutes=15,
                long_running_minutes=61,
            )

        self.assertEqual(ha.states[COFFEE_WARMUP_HELPER]["state"], "13")
        self.assertEqual(ha.states[COFFEE_LONG_RUNNING_HELPER]["state"], "60")
        self.assertIsNotNone(service.policy)
        self.assertEqual(service.status, "ready")

    async def test_failed_rollback_marks_timing_state_unknown(self) -> None:
        ha = FakeHomeAssistant()
        ha.fail_write_numbers = {2, 3}
        service = CoffeeTimingPolicyService(ha)
        current = await service.refresh()

        with self.assertRaises(TimingStateUnknownError):
            await service.patch_minutes(
                expected_revision=current.revision,
                warmup_minutes=15,
                long_running_minutes=61,
            )

        self.assertIsNone(service.policy)
        self.assertEqual(service.status, "unavailable")

    async def test_rollback_readback_mismatch_marks_state_unknown(self) -> None:
        ha = FakeHomeAssistant()
        ha.fail_write_numbers = {2}
        ha.ignore_write_numbers = {4}
        service = CoffeeTimingPolicyService(ha)
        current = await service.refresh()

        with self.assertRaises(TimingStateUnknownError):
            await service.patch_minutes(
                expected_revision=current.revision,
                warmup_minutes=15,
                long_running_minutes=61,
            )
        self.assertIsNone(service.policy)

    async def test_ha_unavailable_during_rollback_marks_state_unknown(self) -> None:
        ha = FakeHomeAssistant()
        ha.fail_write_numbers = {2}
        ha.go_offline_on_write_failure = True
        service = CoffeeTimingPolicyService(ha)
        current = await service.refresh()

        with self.assertRaises(TimingStateUnknownError):
            await service.patch_minutes(
                expected_revision=current.revision,
                warmup_minutes=15,
                long_running_minutes=61,
            )
        self.assertIsNone(service.policy)

    async def test_unchanged_and_partial_patch_use_exact_readback(self) -> None:
        ha = FakeHomeAssistant()
        service = CoffeeTimingPolicyService(ha)
        current = await service.refresh()
        unchanged = await service.patch_minutes(
            expected_revision=current.revision,
            warmup_minutes=13,
        )
        self.assertEqual(ha.calls, [])

        updated = await service.patch_minutes(
            expected_revision=unchanged.revision,
            long_running_minutes=75,
        )
        self.assertEqual(updated.warmup_duration_seconds, 13 * 60)
        self.assertEqual(updated.long_running_threshold_seconds, 75 * 60)

    async def test_refresh_recovers_reschedules_once_and_cancels_cleanly(self) -> None:
        ha = FakeHomeAssistant()
        ha.available = False
        service = CoffeeTimingPolicyService(ha, stale_after_seconds=0.02)
        revisions: list[str] = []
        refresher = CoffeeTimingPolicyRefresher(
            service,
            interval_seconds=0.01,
            max_backoff_seconds=0.02,
            on_policy_change=lambda policy: revisions.append(policy.revision),
        )
        refresher.start()
        await asyncio.sleep(0.025)
        self.assertIsNone(service.policy)

        ha.available = True
        await asyncio.sleep(0.06)
        self.assertIsNotNone(service.policy)
        self.assertEqual(len(revisions), 1)
        self.assertEqual(ha.max_active_reads, 1)

        await asyncio.sleep(0.03)
        self.assertEqual(len(revisions), 1)
        ha.states[COFFEE_WARMUP_HELPER]["state"] = "17"
        ha.states[COFFEE_WARMUP_HELPER]["last_updated"] = "2026-07-29T10:01:00Z"
        await asyncio.sleep(0.04)
        self.assertEqual(len(revisions), 2)

        ha.available = False
        await asyncio.sleep(0.04)
        self.assertTrue(service.stale)
        await refresher.close()
        self.assertFalse(refresher.running)
