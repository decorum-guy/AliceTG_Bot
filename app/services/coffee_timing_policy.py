from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable, Protocol

from app.services.app_state import (
    AppStateStore,
    DEFAULT_COFFEE_LONG_RUNNING_ALERT_DELAY_SECONDS,
    DEFAULT_COFFEE_WARMED_UP_ALERT_DELAY_SECONDS,
)
from app.services.home_assistant import HomeAssistantClient, HomeAssistantError

COFFEE_WARMUP_HELPER = "input_number.coffee_warmup_minutes"
COFFEE_LONG_RUNNING_HELPER = "input_number.coffee_long_running_minutes"
COFFEE_LAST_TURNED_ON_HELPER = "input_datetime.coffee_last_turned_on"
COFFEE_TIMING_INITIALIZED_HELPER = "input_boolean.coffee_timing_initialized"


class TimingPolicyError(RuntimeError):
    pass


class TimingHomeAssistant(Protocol):
    async def get_state(self, entity_id: str) -> dict | None: ...

    async def call_service(self, domain: str, service: str, payload: dict) -> None: ...


@dataclass(frozen=True)
class CoffeeTimingPolicy:
    warmup_duration_seconds: int
    long_running_threshold_seconds: int
    fetched_at: str
    revision: str


@dataclass(frozen=True)
class MigrationResult:
    status: str
    writes: tuple[str, ...] = ()
    reason: str | None = None
    warmup_duration_seconds: int | None = None
    long_running_threshold_seconds: int | None = None


class CoffeeTimingPolicyService:
    """Canonical coffee timing policy backed by Home Assistant helpers."""

    def __init__(
        self,
        ha: HomeAssistantClient | TimingHomeAssistant,
        *,
        stale_after_seconds: float = 90,
    ) -> None:
        self._ha = ha
        self._policy: CoffeeTimingPolicy | None = None
        self._last_error_at: str | None = None
        self._stale_after_seconds = max(0.01, stale_after_seconds)

    @property
    def policy(self) -> CoffeeTimingPolicy | None:
        return self._policy

    @property
    def warmup_duration_seconds(self) -> int | None:
        return self._policy.warmup_duration_seconds if self._policy else None

    @property
    def long_running_threshold_seconds(self) -> int | None:
        return self._policy.long_running_threshold_seconds if self._policy else None

    @property
    def last_error_at(self) -> str | None:
        return self._last_error_at

    @property
    def stale(self) -> bool:
        if self._policy is None:
            return False
        try:
            fetched_at = datetime.fromisoformat(self._policy.fetched_at.replace("Z", "+00:00"))
        except ValueError:
            return True
        return (datetime.now(timezone.utc) - fetched_at).total_seconds() > self._stale_after_seconds

    @property
    def status(self) -> str:
        if self._policy is None:
            return "unavailable"
        if self.stale:
            return "stale"
        if self._last_error_at is not None:
            return "cached"
        return "ready"

    async def refresh(self) -> CoffeeTimingPolicy:
        try:
            if not await self._is_initialized():
                raise TimingPolicyError("Home Assistant timing policy is not initialized")
            policy = await self._read_policy()
        except (HomeAssistantError, TimingPolicyError):
            self._last_error_at = _now()
            raise
        self._policy = policy
        self._last_error_at = None
        return policy

    async def set_warmup_duration_seconds(self, seconds: int) -> CoffeeTimingPolicy:
        return await self._set_seconds(COFFEE_WARMUP_HELPER, seconds)

    async def set_long_running_threshold_seconds(self, seconds: int) -> CoffeeTimingPolicy:
        return await self._set_seconds(COFFEE_LONG_RUNNING_HELPER, seconds)

    async def _set_seconds(self, entity_id: str, seconds: int) -> CoffeeTimingPolicy:
        if seconds < 60 or seconds % 60:
            raise TimingPolicyError("Timing value must be a positive whole number of minutes")
        await self._ha.call_service(
            "input_number",
            "set_value",
            {"entity_id": entity_id, "value": seconds // 60},
        )
        policy = await self._read_policy()
        actual = (
            policy.warmup_duration_seconds
            if entity_id == COFFEE_WARMUP_HELPER
            else policy.long_running_threshold_seconds
        )
        if actual != seconds:
            raise TimingPolicyError(f"Home Assistant did not confirm {entity_id}")
        if await self._is_initialized():
            self._policy = policy
            self._last_error_at = None
        return policy

    async def migrate_legacy(
        self,
        app_state: AppStateStore,
        *,
        apply: bool = False,
    ) -> MigrationResult:
        """Plan or apply an explicit, idempotent legacy-state migration.

        This method is never called automatically at startup. Non-default HA
        values always win and are never overwritten.
        """

        if await self._is_initialized():
            policy = await self.refresh()
            return MigrationResult(
                status="already_initialized",
                warmup_duration_seconds=policy.warmup_duration_seconds,
                long_running_threshold_seconds=policy.long_running_threshold_seconds,
            )

        defaults = (
            DEFAULT_COFFEE_WARMED_UP_ALERT_DELAY_SECONDS,
            DEFAULT_COFFEE_LONG_RUNNING_ALERT_DELAY_SECONDS,
        )
        legacy = app_state.explicit_legacy_coffee_timing
        current = await self._read_existing_pair()
        if legacy is not None:
            target = legacy
            reason = "legacy_bot_values"
        elif current is not None and current != (60, 60):
            target = current
            reason = "existing_home_assistant_values"
        else:
            target = defaults
            reason = "defaults"

        write_helpers = reason != "existing_home_assistant_values"
        requested = (
            (
                COFFEE_WARMUP_HELPER,
                COFFEE_LONG_RUNNING_HELPER,
                COFFEE_TIMING_INITIALIZED_HELPER,
            )
            if write_helpers
            else (COFFEE_TIMING_INITIALIZED_HELPER,)
        )
        if not apply:
            return MigrationResult(
                status="dry_run",
                writes=requested,
                reason=reason,
                warmup_duration_seconds=target[0],
                long_running_threshold_seconds=target[1],
            )

        if write_helpers:
            for entity_id, seconds in (
                (COFFEE_WARMUP_HELPER, target[0]),
                (COFFEE_LONG_RUNNING_HELPER, target[1]),
            ):
                await self._set_seconds(entity_id, seconds)
        verified = await self._read_policy()
        if (
            verified.warmup_duration_seconds,
            verified.long_running_threshold_seconds,
        ) != target:
            raise TimingPolicyError("Home Assistant did not confirm both timing helpers")
        await self._ha.call_service(
            "input_boolean",
            "turn_on",
            {"entity_id": COFFEE_TIMING_INITIALIZED_HELPER},
        )
        if not await self._is_initialized():
            raise TimingPolicyError("Home Assistant did not confirm initialization marker")
        self._policy = verified
        self._last_error_at = None
        await app_state.mark_coffee_timing_migrated_to_ha()
        return MigrationResult(
            status="initialized",
            writes=requested,
            reason=reason,
            warmup_duration_seconds=target[0],
            long_running_threshold_seconds=target[1],
        )

    async def migration_status(self) -> MigrationResult:
        if await self._is_initialized():
            policy = await self.refresh()
            return MigrationResult(
                status="initialized",
                warmup_duration_seconds=policy.warmup_duration_seconds,
                long_running_threshold_seconds=policy.long_running_threshold_seconds,
            )
        current = await self._read_existing_pair()
        return MigrationResult(
            status="not_initialized",
            warmup_duration_seconds=current[0] if current else None,
            long_running_threshold_seconds=current[1] if current else None,
        )

    async def _read_policy(self) -> CoffeeTimingPolicy:
        warmup_state = await self._ha.get_state(COFFEE_WARMUP_HELPER)
        long_state = await self._ha.get_state(COFFEE_LONG_RUNNING_HELPER)
        return CoffeeTimingPolicy(
            warmup_duration_seconds=_minutes_state_to_seconds(
                warmup_state,
                COFFEE_WARMUP_HELPER,
            ),
            long_running_threshold_seconds=_minutes_state_to_seconds(
                long_state,
                COFFEE_LONG_RUNNING_HELPER,
            ),
            fetched_at=_now(),
            revision=_revision(warmup_state, long_state),
        )

    async def _read_existing_pair(self) -> tuple[int, int] | None:
        try:
            policy = await self._read_policy()
        except TimingPolicyError:
            return None
        return policy.warmup_duration_seconds, policy.long_running_threshold_seconds

    async def _is_initialized(self) -> bool:
        state = await self._ha.get_state(COFFEE_TIMING_INITIALIZED_HELPER)
        if not state or state.get("state") in {None, "unknown", "unavailable"}:
            return False
        return str(state.get("state")).lower() == "on"


class CoffeeTimingPolicyRefresher:
    """Managed, non-overlapping refresh loop with bounded recovery backoff."""

    def __init__(
        self,
        service: CoffeeTimingPolicyService,
        *,
        interval_seconds: float = 30,
        max_backoff_seconds: float = 120,
        on_policy_change: Callable[
            [CoffeeTimingPolicy],
            Awaitable[None] | None,
        ]
        | None = None,
    ) -> None:
        self._service = service
        self._interval_seconds = max(0.01, interval_seconds)
        self._max_backoff_seconds = max(
            self._interval_seconds,
            max_backoff_seconds,
        )
        self._on_policy_change = on_policy_change
        self._task: asyncio.Task[None] | None = None
        self._last_revision = service.policy.revision if service.policy else None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.running:
            return
        self._task = asyncio.create_task(self._run())

    async def close(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _run(self) -> None:
        delay = self._interval_seconds
        while True:
            try:
                await asyncio.sleep(delay)
                policy = await self._service.refresh()
                changed = policy.revision != self._last_revision
                self._last_revision = policy.revision
                delay = self._interval_seconds
                if changed and self._on_policy_change is not None:
                    callback_result = self._on_policy_change(policy)
                    if inspect.isawaitable(callback_result):
                        await callback_result
            except asyncio.CancelledError:
                raise
            except (HomeAssistantError, TimingPolicyError):
                delay = min(max(delay * 2, self._interval_seconds), self._max_backoff_seconds)


def _minutes_state_to_seconds(state: dict | None, entity_id: str) -> int:
    if not state or state.get("state") in {None, "unknown", "unavailable"}:
        raise TimingPolicyError(f"Home Assistant timing helper is unavailable: {entity_id}")
    try:
        minutes = float(state["state"])
    except (KeyError, TypeError, ValueError) as exc:
        raise TimingPolicyError(f"Home Assistant timing helper is invalid: {entity_id}") from exc
    if minutes <= 0 or not minutes.is_integer():
        raise TimingPolicyError(f"Home Assistant timing helper must contain whole minutes: {entity_id}")
    return int(minutes * 60)


def _revision(warmup_state: dict | None, long_state: dict | None) -> str:
    values = (
        str((warmup_state or {}).get("state", "")),
        str((long_state or {}).get("state", "")),
        str((warmup_state or {}).get("last_updated", "")),
        str((long_state or {}).get("last_updated", "")),
    )
    return "|".join(values)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
