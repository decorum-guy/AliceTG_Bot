from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup

LOGGER = logging.getLogger(__name__)


class TelegramMessages:
    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def safe_edit(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> None:
        try:
            await self._bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=reply_markup,
            )
        except TelegramBadRequest as exc:
            LOGGER.info("Cannot edit Telegram message, sending a new one: %s", exc)
            await self.safe_send(chat_id, text, reply_markup)

    async def safe_send(
        self,
        chat_id: int,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> None:
        try:
            await self._bot.send_message(chat_id, text, reply_markup=reply_markup)
        except TelegramAPIError:
            LOGGER.exception("Cannot send Telegram message")

    async def safe_delete(self, chat_id: int, message_id: int) -> None:
        try:
            await self._bot.delete_message(chat_id=chat_id, message_id=message_id)
        except TelegramAPIError as exc:
            LOGGER.info("Cannot delete Telegram message: %s", exc)
