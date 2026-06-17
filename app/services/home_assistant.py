from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

LOGGER = logging.getLogger(__name__)


class HomeAssistantError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, body: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class HomeAssistantClient:
    def __init__(self, base_url: str, token: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._session = self._create_session()

    def _create_session(self) -> aiohttp.ClientSession:
        return aiohttp.ClientSession(
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
            timeout=aiohttp.ClientTimeout(total=20),
        )

    async def close(self) -> None:
        if not self._session.closed:
            await self._session.close()

    async def get_state(self, entity_id: str) -> dict[str, Any] | None:
        return await self._request(
            "GET",
            f"/api/states/{entity_id}",
            error_message=f"Cannot read state for {entity_id}",
            json_response=True,
            not_found_none=True,
        )

    async def call_service(self, domain: str, service: str, payload: dict[str, Any], *, timeout_seconds: float | None = None) -> None:
        await self._request(
            "POST",
            f"/api/services/{domain}/{service}",
            error_message=f"Cannot call {domain}.{service}",
            payload=payload,
            timeout_seconds=timeout_seconds,
        )

    async def get_services(self) -> dict[str, set[str]]:
        response = await self._request(
            "GET",
            "/api/services",
            error_message="Cannot read Home Assistant services",
            json_response=True,
        )
        services: dict[str, set[str]] = {}
        if not isinstance(response, list):
            return services
        for domain_info in response:
            if not isinstance(domain_info, dict):
                continue
            domain = str(domain_info.get("domain", "")).strip()
            service_map = domain_info.get("services")
            if domain and isinstance(service_map, dict):
                services[domain] = {str(name) for name in service_map}
        return services

    async def notify(self, service_full_name: str, *, title: str, message: str, data: dict[str, Any] | None = None) -> None:
        service_full_name = service_full_name.strip()
        if "." not in service_full_name:
            raise HomeAssistantError("Home Assistant notify service must be in domain.service format")
        domain, service = service_full_name.split(".", 1)
        payload: dict[str, Any] = {"title": title, "message": message}
        if data is not None:
            payload["data"] = data
        await self.call_service(domain, service, payload)

    async def switch_turn_on(self, entity_id: str) -> None:
        await self.call_service("switch", "turn_on", {"entity_id": entity_id})

    async def switch_turn_off(self, entity_id: str) -> None:
        await self.call_service("switch", "turn_off", {"entity_id": entity_id})

    async def water_heater_set_temperature(self, entity_id: str, temperature: int) -> None:
        await self.call_service("water_heater", "set_temperature", {"entity_id": entity_id, "temperature": temperature})

    async def water_heater_set_operation_mode(self, entity_id: str, operation_mode: str) -> None:
        await self.call_service(
            "water_heater",
            "set_operation_mode",
            {"entity_id": entity_id, "operation_mode": operation_mode},
        )

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

    async def set_volume(self, entity_id: str, volume_level: float) -> None:
        await self.call_service(
            "media_player",
            "volume_set",
            {
                "entity_id": entity_id,
                "volume_level": volume_level,
            },
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        error_message: str,
        payload: dict[str, Any] | None = None,
        json_response: bool = False,
        not_found_none: bool = False,
        timeout_seconds: float | None = None,
    ) -> Any:
        url = f"{self._base_url}{path}"
        request_timeout = aiohttp.ClientTimeout(total=timeout_seconds) if timeout_seconds is not None else None
        for attempt in range(2):
            try:
                if self._session.closed:
                    raise RuntimeError("Home Assistant aiohttp session is closed")
                async with self._session.request(method, url, json=payload, timeout=request_timeout) as response:
                    if not_found_none and response.status == 404:
                        return None
                    await self._raise_for_response(response, error_message=error_message)
                    if json_response:
                        return await response.json()
                    return None
            except HomeAssistantError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as exc:
                if isinstance(exc, RuntimeError) and not self._is_retryable_runtime_error(exc):
                    raise HomeAssistantError(error_message) from exc
                if attempt == 0:
                    LOGGER.warning(
                        "Home Assistant request failed, recreating aiohttp session and retrying once: method=%s path=%s error=%r",
                        method,
                        path,
                        exc,
                    )
                    await self._recreate_session()
                    continue
                raise HomeAssistantError(error_message) from exc
        raise HomeAssistantError(error_message)

    async def _raise_for_response(self, response: aiohttp.ClientResponse, *, error_message: str) -> None:
        if response.status < 400:
            return
        body = await response.text()
        LOGGER.warning("Home Assistant API error: status=%s body=%s", response.status, body[:500])
        raise HomeAssistantError(
            f"{error_message}: Home Assistant API returned HTTP {response.status}",
            status=response.status,
            body=body,
        )

    async def _recreate_session(self) -> None:
        old_session = self._session
        try:
            if not old_session.closed:
                await old_session.close()
                await asyncio.sleep(0)
        except Exception:
            LOGGER.exception("Cannot close stale Home Assistant aiohttp session")
        self._session = self._create_session()

    def _is_retryable_runtime_error(self, exc: RuntimeError) -> bool:
        message = str(exc).lower()
        return "session is closed" in message or "session closed" in message
