from __future__ import annotations

import asyncio
from typing import Any

import aiohttp
from aiohttp_socks import ProxyConnector

from app.config import Settings

PUSHWARD_API_BASE_URL = "https://api.pushward.app"


class PushWardHttpError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, body: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class PushWardHttpClient:
    """PushWard API client using the bot's existing outbound proxy when configured."""

    def __init__(self, settings: Settings, *, timeout_seconds: float = 8) -> None:
        self._key = settings.pushward_integration_key
        self._proxy_url = settings.telegram_proxy_url
        self._timeout_seconds = timeout_seconds
        self._session: aiohttp.ClientSession | None = None

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        allow_not_found: bool = False,
    ) -> dict[str, Any] | str | None:
        proxy = self._proxy_url if self._proxy_url and not self._proxy_url.startswith(("socks4://", "socks5://")) else None
        headers = {"Content-Type": "application/merge-patch+json" if method == "PATCH" else "application/json"}
        try:
            async with self._get_session().request(
                method,
                f"{PUSHWARD_API_BASE_URL}{path}",
                json=payload,
                headers=headers,
                proxy=proxy,
            ) as response:
                if allow_not_found and response.status == 404:
                    return "not_found"
                if response.status >= 400:
                    body = (await response.text())[:500]
                    raise PushWardHttpError(f"PushWard API HTTP {response.status}", status=response.status, body=body)
                if response.content_type == "application/json":
                    return await response.json()
                return None
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            raise PushWardHttpError(f"PushWard transport {type(exc).__name__}") from exc

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = (
                ProxyConnector.from_url(self._proxy_url)
                if self._proxy_url and self._proxy_url.startswith(("socks4://", "socks5://"))
                else None
            )
            self._session = aiohttp.ClientSession(
                connector=connector,
                headers={"Authorization": f"Bearer {self._key}"},
                timeout=aiohttp.ClientTimeout(total=self._timeout_seconds),
            )
        return self._session
