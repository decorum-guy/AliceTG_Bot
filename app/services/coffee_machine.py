from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from app.config import Settings
from app.services.home_assistant import HomeAssistantClient

LOGGER = logging.getLogger(__name__)

CoffeeAction = Literal["turn_on", "turn_off"]


@dataclass(frozen=True)
class CoffeeActionResult:
    action: CoffeeAction
    already_on: bool = False
    runtime_text: str | None = None


async def set_coffee_machine(
    ha: HomeAssistantClient,
    settings: Settings,
    action: CoffeeAction,
    *,
    source: str,
) -> CoffeeActionResult:
    LOGGER.info("Coffee action started: action=%s source=%s entity_id=%s", action, source, settings.coffee_switch_entity)
    try:
        if action == "turn_on":
            switch_state = await ha.get_state(settings.coffee_switch_entity)
            if switch_state and switch_state.get("state") == "on":
                runtime_text = _coffee_runtime_clock(switch_state)
                LOGGER.info(
                    "Coffee turn_on ignored because already on: source=%s entity_id=%s runtime=%s",
                    source,
                    settings.coffee_switch_entity,
                    runtime_text,
                )
                return CoffeeActionResult(action=action, already_on=True, runtime_text=runtime_text)
            await ha.switch_turn_on(settings.coffee_switch_entity)
        else:
            await ha.switch_turn_off(settings.coffee_switch_entity)
    except Exception:
        LOGGER.exception("Coffee action failed: action=%s source=%s entity_id=%s", action, source, settings.coffee_switch_entity)
        raise
    LOGGER.info("Coffee action completed: action=%s source=%s entity_id=%s", action, source, settings.coffee_switch_entity)
    return CoffeeActionResult(action=action)


async def turn_on_coffee_machine(ha: HomeAssistantClient, settings: Settings, *, source: str) -> CoffeeActionResult:
    return await set_coffee_machine(ha, settings, "turn_on", source=source)


async def turn_off_coffee_machine(ha: HomeAssistantClient, settings: Settings, *, source: str) -> CoffeeActionResult:
    return await set_coffee_machine(ha, settings, "turn_off", source=source)


def _coffee_runtime_clock(switch_state: dict) -> str:
    started_at = _parse_ha_datetime(str(switch_state.get("last_changed", "")))
    if started_at is None:
        return "00:00"
    total_seconds = max(0, int((datetime.now(timezone.utc) - started_at).total_seconds()))
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def _parse_ha_datetime(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        LOGGER.warning("Cannot parse Home Assistant datetime: %s", value)
        return None
