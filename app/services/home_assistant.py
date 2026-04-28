from __future__ import annotations

import logging
from typing import Any

import aiohttp

LOGGER = logging.getLogger(__name__)


class HomeAssistantError(RuntimeError):
    pass


class HomeAssistantClient:
    def __init__(self, base_url: str, token: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._session = aiohttp.ClientSession(
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=aiohttp.ClientTimeout(total=20),
        )

    async def close(self) -> None:
        await self._session.close()

    async def get_state(self, entity_id: str) -> dict[str, Any] | None:
        try:
            async with self._session.get(f"{self._base_url}/api/states/{entity_id}") as response:
                if response.status == 404:
                    return None
                await self._raise_for_response(response)
                return await response.json()
        except aiohttp.ClientError as exc:
            raise HomeAssistantError(f"Cannot read state for {entity_id}") from exc

    async def call_service(self, domain: str, service: str, payload: dict[str, Any]) -> None:
        try:
            url = f"{self._base_url}/api/services/{domain}/{service}"
            async with self._session.post(url, json=payload) as response:
                await self._raise_for_response(response)
        except aiohttp.ClientError as exc:
            raise HomeAssistantError(f"Cannot call {domain}.{service}") from exc

    async def switch_turn_on(self, entity_id: str) -> None:
        await self.call_service("switch", "turn_on", {"entity_id": entity_id})

    async def switch_turn_off(self, entity_id: str) -> None:
        await self.call_service("switch", "turn_off", {"entity_id": entity_id})

    async def input_boolean_turn_on(self, entity_id: str) -> None:
        await self.call_service("input_boolean", "turn_on", {"entity_id": entity_id})

    async def input_boolean_turn_off(self, entity_id: str) -> None:
        await self.call_service("input_boolean", "turn_off", {"entity_id": entity_id})

    async def play_media(self, entity_id: str, text: str, content_type: str = "text") -> None:
        await self.call_service(
            "media_player",
            "play_media",
            {
                "entity_id": entity_id,
                "media_content_id": text,
                "media_content_type": content_type,
            },
        )

    async def _raise_for_response(self, response: aiohttp.ClientResponse) -> None:
        if response.status < 400:
            return
        body = await response.text()
        LOGGER.warning("Home Assistant API error: status=%s body=%s", response.status, body[:500])
        raise HomeAssistantError(f"Home Assistant API returned HTTP {response.status}")
