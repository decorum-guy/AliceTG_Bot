from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from app.planning.providers.cache import ProviderCalendarCache
from app.planning.providers.contracts import CalendarWindow


class ICloudCalendarRefreshLoop:
    """Server-owned rolling read refresh; it exposes no HTTP or provider proxy."""

    def __init__(self, cache: ProviderCalendarCache, *, interval_seconds: int) -> None:
        if interval_seconds < 60:
            raise ValueError("iCloud refresh interval is too short")
        self.cache = cache
        self.interval_seconds = interval_seconds
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="planning-icloud-read-refresh")

    async def _run(self) -> None:
        while True:
            await self.refresh_once()
            await asyncio.sleep(self.interval_seconds)

    async def refresh_once(self) -> None:
        now = datetime.now(timezone.utc)
        window = CalendarWindow(
            start=now - timedelta(days=30),
            end=now + timedelta(days=365),
        )
        await self.cache.refresh(window)

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        close = getattr(self.cache.provider, "close", None) if self.cache.provider is not None else None
        if callable(close):
            await close()
