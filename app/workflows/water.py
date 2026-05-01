from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from aiogram.types import InlineKeyboardMarkup

from app.config import Settings
from app.keyboards.coffee import delete_only
from app.keyboards.main import main_menu, sonya_order_menu
from app.keyboards.water import water_confirmation
from app.messages import water as water_messages
from app.messages.common import admin_menu_text, sonya_menu_text
from app.services.home_assistant import HomeAssistantClient, HomeAssistantError
from app.services.telegram_messages import TelegramMessages
from app.services.yandex_dialogs import yandex_dialog_content_type
from app.storage.base import Reminder, Storage
from app.workflows.coffee import minute_word
from app.workflows.comments import NO_COMMENT, normalize_order_comment

LOGGER = logging.getLogger(__name__)

TG_WATER_WANTS_DIALOG_ID = "tg_ask_sonya_wants_water"
TG_WATER_COMMENT_DIALOG_ID = "tg_ask_sonya_water_comment"
DIRECT_WATER_COMMENT_DIALOG_ID = "sonya_direct_water_comment"

TG_AWAITING_WATER_WANTS = "input_boolean.tg_awaiting_sonya_water_wants"
TG_AWAITING_WATER_COMMENT = "input_boolean.tg_awaiting_sonya_water_comment"
DIRECT_AWAITING_WATER_COMMENT = "input_boolean.sonya_direct_awaiting_water_comment"

POSITIVE_WORDS = ("да", "хочу", "ага", "можно", "буду", "конечно")
NEGATIVE_WORDS = ("нет спасибо", "не хочу", "не надо", "нет")
ALL_WATER_WAIT_FLAGS = (
    TG_AWAITING_WATER_WANTS,
    TG_AWAITING_WATER_COMMENT,
    DIRECT_AWAITING_WATER_COMMENT,
)


@dataclass(frozen=True)
class WaterAnswer:
    answer: str
    intent: str
    dialog: str | None = None
    source: str | None = None
    request_id: str | None = None


@dataclass(frozen=True)
class WaterContext:
    comment: str = NO_COMMENT
    source: str = "voice"


class WaterWorkflow:
    def __init__(
        self,
        settings: Settings,
        ha: HomeAssistantClient,
        storage: Storage,
        telegram_messages: TelegramMessages,
    ) -> None:
        self._settings = settings
        self._ha = ha
        self._storage = storage
        self._telegram_messages = telegram_messages
        self._tasks: set[asyncio.Task[None]] = set()
        self._active_chat_id: int | None = None
        self._active_message_id: int | None = None
        self._latest_context: WaterContext | None = None
        self._context_by_message_id: dict[int, WaterContext] = {}
        self._sonya_order_messages: dict[int, tuple[int, int | None]] = {}
        self._recent_event_keys: dict[str, float] = {}
        self._dedupe_ttl_seconds = 5.0

    async def start_telegram_question(self, chat_id: int, message_id: int | None) -> int | None:
        await self._clear_all_wait_flags()
        await self._set_only_wait_flag(TG_AWAITING_WATER_WANTS)
        await self._ha.play_media(
            self._settings.bedroom_player_entity,
            "Соня, тебя спрашивают: хочешь воды?",
            yandex_dialog_content_type(self._settings, TG_WATER_WANTS_DIALOG_ID),
        )
        edited_id = await self._telegram_messages.safe_edit(
            chat_id,
            message_id,
            water_messages.water_questioning(),
            delete_only(),
        )
        self._set_active_message(chat_id, edited_id)
        return edited_id

    async def start_direct_request(self) -> None:
        self._set_active_message(self._settings.telegram_admin_chat_id, None)
        await self._ask_comment(direct=True)

    async def start_sonya_order_comment(self, user_id: int, chat_id: int, message_id: int | None) -> None:
        self._sonya_order_messages[user_id] = (chat_id, message_id)

    def is_waiting_sonya_order_comment(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self._sonya_order_messages

    async def submit_sonya_order(self, user_id: int, comment: str = NO_COMMENT) -> WaterContext:
        context = WaterContext(comment=normalize_order_comment(comment), source="sonya_telegram_order")
        self._latest_context = context
        message_id = await self._telegram_messages.safe_send(
            self._settings.telegram_admin_chat_id,
            water_messages.water_confirmation(context.comment),
            reply_markup=water_confirmation(),
        )
        if message_id is not None:
            self._context_by_message_id[message_id] = context
        return context

    async def complete_sonya_order_message(self, user_id: int) -> None:
        chat_id, message_id = self._sonya_order_messages.pop(user_id, (self._settings.telegram_admin_chat_id, None))
        await self._telegram_messages.safe_edit(chat_id, message_id, water_messages.sonya_water_sent(), delete_only())

    async def cancel_sonya_order_comment(self, user_id: int) -> None:
        self._sonya_order_messages.pop(user_id, None)

    async def handle_wants_answer(self, answer: WaterAnswer) -> None:
        if self._is_duplicate_event("wants", answer):
            return

        normalized = answer.answer.strip().lower()
        intent = answer.intent.strip()
        if intent == "YANDEX.REJECT" or any(word in normalized for word in NEGATIVE_WORDS):
            await self._clear_all_wait_flags()
            await self._say_bedroom("Хорошо.")
            await self._edit_active_or_send("Соня отказалась от воды.", delete_only())
            await self._show_admin_menu()
            return

        if intent == "YANDEX.CONFIRM" or any(word in normalized for word in POSITIVE_WORDS):
            await self._ask_comment(direct=False)
            return

        await self._clear_all_wait_flags()
        await self._edit_active_or_send(f"Соня ответила: {answer.answer.strip() or 'пусто'}. Я не поняла, нужна вода или нет.", delete_only())

    async def handle_comment_answer(self, answer: WaterAnswer, *, direct: bool) -> None:
        if self._is_duplicate_event("comment", answer):
            return

        context = WaterContext(comment=normalize_order_comment(answer.answer), source="direct" if direct else "telegram")
        self._latest_context = context
        if direct:
            self._set_active_message(self._settings.telegram_admin_chat_id, None)
        await self._clear_all_wait_flags()
        await self._edit_active_or_send(
            water_messages.water_confirmation(context.comment),
            water_confirmation(),
            context=context,
        )
        await self._say_bedroom("Хорошо, передала заказ.")

    async def confirm_now(self, chat_id: int, message_id: int | None) -> int | None:
        context = self._context_for_message(message_id)
        await self._say_bedroom("Артём скоро принесёт воду.")
        edited_id = await self._telegram_messages.safe_edit(
            chat_id,
            message_id,
            water_messages.water_done_now(context.comment),
            delete_only(),
        )
        await self._show_menu_for_chat(chat_id)
        return edited_id

    async def schedule_reminder(self, chat_id: int, minutes: int, message_id: int | None = None) -> WaterContext:
        context = self._context_for_message(message_id)
        reminder = Reminder(chat_id=chat_id, minutes=minutes, reason="sonya_water", comment=context.comment)
        reminder_id = await self._storage.add_reminder(reminder)
        task = asyncio.create_task(self._remind_later(reminder_id, reminder))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        await self._say_bedroom(f"Артём принесёт воду через {minutes} {minute_word(minutes)}.")
        await self._show_menu_for_chat(chat_id)
        return context

    async def _ask_comment(self, *, direct: bool) -> None:
        await self._set_only_wait_flag(DIRECT_AWAITING_WATER_COMMENT if direct else TG_AWAITING_WATER_COMMENT)
        await self._ha.play_media(
            self._settings.bedroom_player_entity,
            "Есть пожелания?",
            yandex_dialog_content_type(self._settings, DIRECT_WATER_COMMENT_DIALOG_ID if direct else TG_WATER_COMMENT_DIALOG_ID),
        )

    async def _remind_later(self, reminder_id: int, reminder: Reminder) -> None:
        try:
            await asyncio.sleep(reminder.minutes * 60)
            await self._telegram_messages.safe_send(
                reminder.chat_id,
                water_messages.water_reminder(reminder.comment or NO_COMMENT),
                reply_markup=delete_only(),
            )
        finally:
            await self._storage.remove_reminder(reminder_id)

    async def _edit_active_or_send(
        self,
        text: str,
        reply_markup: InlineKeyboardMarkup,
        context: WaterContext | None = None,
    ) -> None:
        chat_id = self._active_chat_id or self._settings.telegram_admin_chat_id
        message_id = self._active_message_id if self._active_chat_id else None
        edited_id = await self._telegram_messages.safe_edit(chat_id, message_id, text, reply_markup)
        self._set_active_message(chat_id, edited_id)
        if edited_id is not None and context:
            self._context_by_message_id[edited_id] = context

    def _set_active_message(self, chat_id: int, message_id: int | None) -> None:
        self._active_chat_id = chat_id
        self._active_message_id = message_id

    def _context_for_message(self, message_id: int | None) -> WaterContext:
        if message_id is not None and message_id in self._context_by_message_id:
            return self._context_by_message_id[message_id]
        return self._latest_context or WaterContext()

    async def reset_after_failure(self) -> None:
        LOGGER.warning("Water workflow failed, resetting water flags", exc_info=True)
        self._active_chat_id = None
        self._active_message_id = None
        await self._clear_all_wait_flags()

    async def _set_only_wait_flag(self, entity_id: str) -> None:
        await self._clear_all_wait_flags()
        await self._ha.input_boolean_turn_on(entity_id)

    async def _clear_all_wait_flags(self) -> None:
        for entity_id in ALL_WATER_WAIT_FLAGS:
            try:
                await self._ha.input_boolean_turn_off(entity_id)
            except HomeAssistantError:
                LOGGER.exception("Cannot clear water wait flag: %s", entity_id)

    async def _say_bedroom(self, text: str) -> None:
        try:
            await self._ha.play_media(self._settings.bedroom_player_entity, text)
        except HomeAssistantError:
            LOGGER.exception("Cannot speak through bedroom media player")

    async def _show_admin_menu(self) -> None:
        await self._telegram_messages.safe_send(self._settings.telegram_admin_chat_id, admin_menu_text(), reply_markup=main_menu())

    async def _show_menu_for_chat(self, chat_id: int) -> None:
        if chat_id == self._settings.telegram_admin_chat_id:
            await self._telegram_messages.safe_send(chat_id, admin_menu_text(), reply_markup=main_menu())
            return
        await self._telegram_messages.safe_send(chat_id, sonya_menu_text(), reply_markup=sonya_order_menu())

    def _is_duplicate_event(self, step: str, answer: WaterAnswer) -> bool:
        now = time.monotonic()
        self._recent_event_keys = {
            key: seen_at
            for key, seen_at in self._recent_event_keys.items()
            if now - seen_at <= self._dedupe_ttl_seconds
        }
        normalized_answer = " ".join(answer.answer.strip().lower().split())
        normalized_intent = answer.intent.strip()
        key = f"{step}:{answer.dialog}:{normalized_answer}:{normalized_intent}"
        if key in self._recent_event_keys:
            return True
        self._recent_event_keys[key] = now
        return False
