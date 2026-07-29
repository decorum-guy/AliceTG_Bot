from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from app.services.app_state import (
    AppStateStore,
    DEFAULT_COFFEE_LONG_RUNNING_ALERT_DELAY_SECONDS,
    DEFAULT_COFFEE_WARMED_UP_ALERT_DELAY_SECONDS,
)
from app.services.home_assistant import HomeAssistantClient, HomeAssistantError

COFFEE_WARMUP_HELPER = "input_number.coffee_warmup_minutes"
COFFEE_LONG_RUNNING_HELPER = "input_number.coffee_long_running_minutes"
COFFEE_LAST_TURNED_ON_HELPER = "input_datetime.coffee_last_turned_on"


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


class CoffeeTimingPolicyService:
    """Canonical coffee timing policy backed by Home Assistant helpers."""

    def __init__(self, ha: HomeAssistantClient | TimingHomeAssistant) -> None:
        self._ha = ha
        self._policy: CoffeeTimingPolicy | None = None
        self._last_error_at: str | None = None

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

    async def refresh(self) -> CoffeeTimingPolicy:
        try:
            warmup_state = await self._ha.get_state(COFFEE_WARMUP_HELPER)
            long_state = await self._ha.get_state(COFFEE_LONG_RUNNING_HELPER)
            policy = CoffeeTimingPolicy(
                warmup_duration_seconds=_minutes_state_to_seconds(warmup_state, COFFEE_WARMUP_HELPER),
                long_running_threshold_seconds=_minutes_state_to_seconds(
                    long_state,
                    COFFEE_LONG_RUNNING_HELPER,
                ),
                fetched_at=_now(),
                revision=_revision(warmup_state, long_state),
            )
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
        policy = await self.refresh()
        actual = (
            policy.warmup_duration_seconds
            if entity_id == COFFEE_WARMUP_HELPER
            else policy.long_running_threshold_seconds
        )
        if actual != seconds:
            raise TimingPolicyError(f"Home Assistant did not confirm {entity_id}")
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

        if app_state.coffee_timing_migrated_to_ha:
            return MigrationResult(status="already_migrated")

        policy = await self.refresh()
        legacy_warmup = app_state.legacy_coffee_warmup_seconds
        legacy_long = app_state.legacy_coffee_long_running_seconds
        defaults = (
            DEFAULT_COFFEE_WARMED_UP_ALERT_DELAY_SECONDS,
            DEFAULT_COFFEE_LONG_RUNNING_ALERT_DELAY_SECONDS,
        )
        current = (
            policy.warmup_duration_seconds,
            policy.long_running_threshold_seconds,
        )
        if current != defaults:
            return MigrationResult(
                status="skipped",
                reason="Home Assistant helpers already contain non-default values",
            )

        requested: list[tuple[str, int]] = []
        if legacy_warmup != defaults[0]:
            requested.append((COFFEE_WARMUP_HELPER, legacy_warmup))
        if legacy_long != defaults[1]:
            requested.append((COFFEE_LONG_RUNNING_HELPER, legacy_long))
        if not requested:
            if apply:
                await app_state.mark_coffee_timing_migrated_to_ha()
            return MigrationResult(status="no_change")
        if not apply:
            return MigrationResult(
                status="dry_run",
                writes=tuple(entity_id for entity_id, _ in requested),
            )

        for entity_id, seconds in requested:
            await self._set_seconds(entity_id, seconds)
        await app_state.mark_coffee_timing_migrated_to_ha()
        return MigrationResult(
            status="migrated",
            writes=tuple(entity_id for entity_id, _ in requested),
        )


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
