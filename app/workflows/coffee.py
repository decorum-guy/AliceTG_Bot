from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from aiogram import Bot

from app.config import Settings
from app.keyboards.coffee import confirmation, delete_only
from app.services.home_assistant import HomeAssistantClient, HomeAssistantError
from app.storage.base import Reminder, Storage

LOGGER = logging.getLogger(__name__)

POSITIVE_WORDS = ("да", "хочу", "ага", "можно", "буду", "конечно")
NEGATIVE_WORDS = ("нет спасибо", "не хочу", "не надо", "нет")


@dataclass(frozen=True)
class SonyaAnswer:
    answer: str
    intent: str
    source: str | None = None
    request_id: str | None = None


class CoffeeWorkflow:
    def __init__(
        self,
        bot: Bot,
        settings: Settings,
        ha: HomeAssistantClient,
        storage: Storage,
    ) -> None:
        self._bot = bot
        self._settings = settings
        self._ha = ha
        self._storage = storage
        self._tasks: set[asyncio.Task[None]] = set()

    async def handle_sonya_answer(self, answer: SonyaAnswer) -> None:
        text = answer.answer.strip()
        normalized = text.lower()
        intent = answer.intent.strip()

        if intent == "YANDEX.CONFIRM" or any(word in normalized for word in POSITIVE_WORDS):
            await self._send_confirmation("Соня хочет кофе. Включить кофемашину?")
            await self._say_bedroom("Хорошо, уточняю у Артёма.")
            return

        if intent == "YANDEX.REJECT" or any(word in normalized for word in NEGATIVE_WORDS):
            await self._bot.send_message(
                self._settings.telegram_admin_chat_id,
                "Соня сказала, что кофе не хочет.",
                reply_markup=delete_only(),
            )
            await self._say_bedroom("Хорошо, не включаю кофемашину.")
            return

        shown = text or "пустой ответ"
        await self._send_confirmation(f"Соня ответила: {shown}. Включить кофемашину?")

    async def schedule_reminder(self, chat_id: int, minutes: int) -> None:
        reminder = Reminder(chat_id=chat_id, minutes=minutes, reason="sonya_coffee")
        reminder_id = await self._storage.add_reminder(reminder)
        task = asyncio.create_task(self._remind_later(reminder_id, reminder))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _remind_later(self, reminder_id: int, reminder: Reminder) -> None:
        try:
            await asyncio.sleep(reminder.minutes * 60)
            await self._bot.send_message(
                reminder.chat_id,
                "Напоминание: Соня просила кофе. Включить кофемашину?",
                reply_markup=confirmation(),
            )
        finally:
            await self._storage.remove_reminder(reminder_id)

    async def _send_confirmation(self, text: str) -> None:
        await self._bot.send_message(
            self._settings.telegram_admin_chat_id,
            text,
            reply_markup=confirmation(),
        )

    async def _say_bedroom(self, text: str) -> None:
        try:
            await self._ha.play_media(self._settings.bedroom_player_entity, text)
        except HomeAssistantError:
            LOGGER.exception("Cannot speak through bedroom media player")
