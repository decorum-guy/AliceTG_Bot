from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict, deque
from dataclasses import dataclass

from app.config import Settings
from app.services.coffee_machine import set_coffee_machine
from app.services.home_assistant import HomeAssistantClient, HomeAssistantError

LOGGER = logging.getLogger(__name__)


class CoffeeActionConflict(RuntimeError):
    pass


class CoffeeActionRateLimited(RuntimeError):
    pass


@dataclass(frozen=True)
class ConfirmedCoffeeAction:
    action: str
    request_id: str
    state: str
    already_in_state: bool
    observed_at: str | None


class ControlCenterCoffeeActions:
    def __init__(
        self,
        ha: HomeAssistantClient,
        settings: Settings,
        *,
        confirmation_timeout_seconds: float = 10,
    ) -> None:
        self._ha = ha
        self._settings = settings
        self._timeout = confirmation_timeout_seconds
        self._lock = asyncio.Lock()
        self._results: OrderedDict[str, ConfirmedCoffeeAction] = OrderedDict()
        self._recent: deque[float] = deque()

    async def execute(self, action: str, request_id: str) -> ConfirmedCoffeeAction:
        if action not in {"turn_on", "turn_off"}:
            raise ValueError("Unsupported coffee action")
        cached = self._results.get(request_id)
        if cached is not None:
            if cached.action != action:
                raise CoffeeActionConflict("requestId was already used for another action")
            return cached

        async with self._lock:
            cached = self._results.get(request_id)
            if cached is not None:
                if cached.action != action:
                    raise CoffeeActionConflict("requestId was already used for another action")
                return cached
            self._check_rate_limit()
            before = await self._ha.get_state(self._settings.coffee_switch_entity)
            before_state = _usable_state(before)
            target = "on" if action == "turn_on" else "off"
            if before_state == target:
                result = _result(action, request_id, before, already=True)
                self._remember(result)
                return result

            await set_coffee_machine(
                self._ha,
                self._settings,
                action,  # type: ignore[arg-type]
                source="control-center",
            )
            confirmed = await self._wait_for_state(target)
            result = _result(action, request_id, confirmed, already=False)
            self._remember(result)
            LOGGER.info(
                "Control Center coffee action confirmed: action=%s request_id_hash=%s state=%s",
                action,
                hash(request_id),
                target,
            )
            return result

    def _check_rate_limit(self) -> None:
        now = time.monotonic()
        while self._recent and now - self._recent[0] > 60:
            self._recent.popleft()
        if len(self._recent) >= 5:
            raise CoffeeActionRateLimited("Coffee action rate limit exceeded")
        self._recent.append(now)

    async def _wait_for_state(self, expected: str) -> dict:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._timeout
        while True:
            state = await self._ha.get_state(self._settings.coffee_switch_entity)
            if _usable_state(state) == expected:
                return state or {}
            if loop.time() >= deadline:
                raise HomeAssistantError(
                    "Home Assistant did not confirm coffee state before timeout"
                )
            await asyncio.sleep(0.2)

    def _remember(self, result: ConfirmedCoffeeAction) -> None:
        self._results[result.request_id] = result
        self._results.move_to_end(result.request_id)
        while len(self._results) > 200:
            self._results.popitem(last=False)


def _usable_state(state: dict | None) -> str:
    value = str((state or {}).get("state", "")).lower()
    if value not in {"on", "off"}:
        raise HomeAssistantError("Coffee state is unavailable")
    return value


def _result(
    action: str,
    request_id: str,
    state: dict | None,
    *,
    already: bool,
) -> ConfirmedCoffeeAction:
    return ConfirmedCoffeeAction(
        action=action,
        request_id=request_id,
        state=_usable_state(state),
        already_in_state=already,
        observed_at=str((state or {}).get("last_updated") or "") or None,
    )
