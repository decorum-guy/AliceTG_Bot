from __future__ import annotations

import asyncio

from app.storage.base import Reminder, Storage


class MemoryStorage(Storage):
    def __init__(self) -> None:
        self._next_id = 1
        self._reminders: dict[int, Reminder] = {}
        self._lock = asyncio.Lock()

    async def add_reminder(self, reminder: Reminder) -> int:
        async with self._lock:
            reminder_id = self._next_id
            self._next_id += 1
            self._reminders[reminder_id] = reminder
            return reminder_id

    async def remove_reminder(self, reminder_id: int) -> None:
        async with self._lock:
            self._reminders.pop(reminder_id, None)
