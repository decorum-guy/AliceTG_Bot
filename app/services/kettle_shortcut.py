from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from app.config import Settings
from app.services.home_assistant import (
    HomeAssistantClient,
    HomeAssistantDispatchUncertain,
    HomeAssistantError,
)


class KettleShortcutError(RuntimeError):
    def __init__(self, code: str, status: int) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


def _updated_at(state: dict) -> datetime | None:
    value = state.get("last_updated")
    if not isinstance(value, str):
        return None
    try:
        updated = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if updated.tzinfo is None or updated.utcoffset() is None:
            return None
        return updated.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def _target_is_100(state: dict) -> bool:
    temperature = state["attributes"].get("temperature")
    return type(temperature) in (int, float) and temperature == 100


def _is_boiling(state: dict) -> bool:
    return (state["state"] == "on"
            and state["attributes"].get("operation_mode") == "on"
            and _target_is_100(state))


def _is_stopped(state: dict) -> bool:
    return state["state"] == "off" and state["attributes"].get("operation_mode") == "off"


class KettleShortcutService:
    """One configured kettle, fixed boil/stop, and authoritative REST confirmation."""

    def __init__(
        self,
        ha: HomeAssistantClient,
        settings: Settings,
        *,
        verification_timeout: float = 5.0,
        poll_interval: float = 0.25,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        utcnow: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._ha = ha
        self._entity = settings.kettle_entity
        self._verification_timeout = verification_timeout
        self._poll_interval = poll_interval
        self._clock = clock
        self._sleep = sleep
        self._utcnow = utcnow

    async def boil(self) -> str:
        if (not isinstance(self._entity, str)
                or re.fullmatch(r"water_heater\.[a-z0-9_]+", self._entity) is None):
            raise KettleShortcutError("kettle_invalid_target", 503)

        before = await self._read(5.0)
        if _is_boiling(before):
            return "already_boiling"
        previous_update = _updated_at(before)
        if previous_update is None:
            raise KettleShortcutError("kettle_invalid_state", 503)
        started_at = self._utcnow()

        await self._dispatch("set_temperature", {"temperature": 100}, 5.0)
        # Share a single verification window. Do not activate an unconfirmed target.
        deadline = self._clock() + self._verification_timeout
        await self._verify(_target_is_100, deadline)
        await self._dispatch("set_operation_mode", {"operation_mode": "on"},
                             self._remaining(deadline))

        def confirmed(state: dict) -> bool:
            updated = _updated_at(state)
            return (_is_boiling(state) and updated is not None
                    and updated > previous_update and updated >= started_at)

        await self._verify(confirmed, deadline)
        return "boiling"

    async def stop(self) -> str:
        if (not isinstance(self._entity, str)
                or re.fullmatch(r"water_heater\.[a-z0-9_]+", self._entity) is None):
            raise KettleShortcutError("kettle_invalid_target", 503)

        before = await self._read(5.0)
        if _is_stopped(before):
            return "already_stopped"
        previous_update = _updated_at(before)
        if previous_update is None:
            raise KettleShortcutError("kettle_invalid_state", 503)

        await self._dispatch("set_operation_mode", {"operation_mode": "off"}, 5.0)
        deadline = self._clock() + self._verification_timeout

        def confirmed(state: dict) -> bool:
            updated = _updated_at(state)
            return (_is_stopped(state) and updated is not None
                    and updated > previous_update)

        await self._verify(confirmed, deadline)
        return "stopped"

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise KettleShortcutError("ha_verification_timeout", 504)
        return remaining

    async def _read(self, timeout: float, *, verifying: bool = False) -> dict:
        try:
            state = await asyncio.wait_for(self._ha.get_state_once(self._entity), timeout)
        except asyncio.TimeoutError:
            code = "ha_verification_timeout" if verifying else "home_assistant_unavailable"
            raise KettleShortcutError(code, 504 if verifying else 503) from None
        except (HomeAssistantError, RuntimeError, ValueError):
            raise KettleShortcutError("home_assistant_unavailable", 503) from None
        if (not isinstance(state, dict) or state.get("state") not in ("on", "off")
                or not isinstance(state.get("attributes"), dict)):
            raise KettleShortcutError("kettle_invalid_state", 503)
        return state

    async def _dispatch(self, service: str, values: dict, timeout: float) -> None:
        try:
            await asyncio.wait_for(self._ha.call_service_once(
                "water_heater", service, {"entity_id": self._entity, **values},
            ), timeout)
        except (HomeAssistantDispatchUncertain, asyncio.TimeoutError):
            raise KettleShortcutError("kettle_dispatch_uncertain", 502) from None
        except (HomeAssistantError, RuntimeError):
            raise KettleShortcutError("kettle_command_failed", 502) from None

    async def _verify(self, predicate: Callable[[dict], bool], deadline: float) -> None:
        while True:
            state = await self._read(self._remaining(deadline), verifying=True)
            remaining = self._remaining(deadline)
            if predicate(state):
                return
            await self._sleep(min(self._poll_interval, remaining))
