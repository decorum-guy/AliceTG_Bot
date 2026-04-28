from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup

LOGGER = logging.getLogger(__name__)


def is_message_not_modified_error(exc: TelegramBadRequest) -> bool:
    return "message is not modified" in str(exc).lower()


class TelegramMessages:
    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def safe_edit(
        self,
        chat_id: int,
        message_id: int | None,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> int | None:
        if message_id is None:
            return await self.safe_send(chat_id, text, reply_markup)

        try:
            await self._bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=reply_markup,
            )
            return message_id
        except TelegramBadRequest as exc:
            if is_message_not_modified_error(exc):
                LOGGER.info("Telegram message is not modified, keeping message_id=%s", message_id)
                return message_id
            LOGGER.info("Cannot edit Telegram message, sending a new one: %s", exc)
            return await self.safe_send(chat_id, text, reply_markup)

    async def safe_send(
        self,
        chat_id: int,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> int | None:
        try:
            message = await self._bot.send_message(chat_id, text, reply_markup=reply_markup)
            return message.message_id
        except TelegramAPIError:
            LOGGER.exception("Cannot send Telegram message")
            return None

    async def safe_delete(self, chat_id: int, message_id: int) -> None:
        try:
            await self._bot.delete_message(chat_id=chat_id, message_id=message_id)
        except TelegramAPIError as exc:
            LOGGER.info("Cannot delete Telegram message: %s", exc)
