from __future__ import annotations

import logging
from typing import Literal

from app.config import Settings
from app.services.home_assistant import HomeAssistantClient

LOGGER = logging.getLogger(__name__)

CoffeeAction = Literal["turn_on", "turn_off"]


async def set_coffee_machine(
    ha: HomeAssistantClient,
    settings: Settings,
    action: CoffeeAction,
    *,
    source: str,
) -> None:
    LOGGER.info("Coffee action started: action=%s source=%s entity_id=%s", action, source, settings.coffee_switch_entity)
    try:
        if action == "turn_on":
            await ha.switch_turn_on(settings.coffee_switch_entity)
        else:
            await ha.switch_turn_off(settings.coffee_switch_entity)
    except Exception:
        LOGGER.exception("Coffee action failed: action=%s source=%s entity_id=%s", action, source, settings.coffee_switch_entity)
        raise
    LOGGER.info("Coffee action completed: action=%s source=%s entity_id=%s", action, source, settings.coffee_switch_entity)


async def turn_on_coffee_machine(ha: HomeAssistantClient, settings: Settings, *, source: str) -> None:
    await set_coffee_machine(ha, settings, "turn_on", source=source)


async def turn_off_coffee_machine(ha: HomeAssistantClient, settings: Settings, *, source: str) -> None:
    await set_coffee_machine(ha, settings, "turn_off", source=source)
