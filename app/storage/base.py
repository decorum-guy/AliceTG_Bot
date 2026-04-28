from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class Reminder:
    chat_id: int
    minutes: int
    reason: str
    coffee_type: str | None = None
    coffee_temperature: str | None = None
    coffee_syrup: str | None = None
    tea_keep_warm_temperature: int | None = None


class Storage(ABC):
    @abstractmethod
    async def add_reminder(self, reminder: Reminder) -> int:
        raise NotImplementedError

    @abstractmethod
    async def remove_reminder(self, reminder_id: int) -> None:
        raise NotImplementedError
