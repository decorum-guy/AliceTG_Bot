"""The single allow-listed Yandex Station action boundary for all clients."""
from __future__ import annotations

from typing import Literal

from app.config import Settings
from app.services.app_state import AppStateStore
from app.services.home_assistant import (
    HomeAssistantClient,
    HomeAssistantDispatchUncertain,
    HomeAssistantError,
)

StationAction = Literal["play", "pause", "volume_down", "volume_up", "previous", "next", "like"]
STATION_COMMANDS: dict[StationAction, str] = {
    "play": "Включи музыку",
    "pause": "Поставь на паузу",
    "volume_down": "Сделай тише",
    "volume_up": "Сделай громче",
    "previous": "Предыдущий трек",
    "next": "Следующий трек",
    "like": "Поставь лайк",
}


class StationActionService:
    def __init__(self, ha: HomeAssistantClient, settings: Settings) -> None:
        self._ha = ha
        self._entity = settings.station_player_entity

    async def dispatch(self, action: StationAction) -> Literal["dispatched", "uncertain"]:
        if action not in STATION_COMMANDS:
            raise ValueError("unknown_station_action")
        return await self._dispatch_command(STATION_COMMANDS[action])

    async def dispatch_preset(self, preset_id: str, app_state: AppStateStore) -> Literal["dispatched", "uncertain"]:
        await app_state.station_presets()
        preset = app_state.station_preset(preset_id)
        if preset is None:
            raise ValueError("unknown_station_preset")
        return await self._dispatch_command(preset["command"])

    async def _dispatch_command(self, command: str) -> Literal["dispatched", "uncertain"]:
        if not self._entity.startswith("media_player."):
            raise HomeAssistantError("Station target is not configured")
        try:
            await self._ha.call_service_once(
                "media_player",
                "play_media",
                {
                    "entity_id": self._entity,
                    "media_content_id": command,
                    "media_content_type": "command",
                },
            )
        except HomeAssistantDispatchUncertain:
            return "uncertain"
        return "dispatched"
