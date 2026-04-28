from __future__ import annotations

import asyncio
import html
import logging
from dataclasses import dataclass

from aiogram.types import InlineKeyboardMarkup

from app.config import Settings
from app.keyboards.coffee import confirmation, delete_only
from app.keyboards.main import main_menu, sonya_order_menu
from app.services.home_assistant import HomeAssistantClient, HomeAssistantError
from app.services.telegram_messages import TelegramMessages
from app.storage.base import Reminder, Storage

LOGGER = logging.getLogger(__name__)

POSITIVE_WORDS = ("да", "хочу", "ага", "можно", "буду", "конечно")
NEGATIVE_WORDS = ("нет спасибо", "не хочу", "не надо", "нет")

TG_WANTS_DIALOG = "dialog:домашний помощник:tg_ask_sonya_wants_coffee"
TG_TEMPERATURE_DIALOG = "dialog:домашний помощник:tg_ask_sonya_coffee_temperature"
TG_SYRUP_DIALOG = "dialog:домашний помощник:tg_ask_sonya_coffee_syrup"
DIRECT_TEMPERATURE_DIALOG = "dialog:домашний помощник:sonya_direct_coffee_temperature"
DIRECT_SYRUP_DIALOG = "dialog:домашний помощник:sonya_direct_coffee_syrup"

TG_AWAITING_TEMPERATURE = "input_boolean.tg_awaiting_sonya_coffee_temperature"
TG_AWAITING_SYRUP = "input_boolean.tg_awaiting_sonya_coffee_syrup"
DIRECT_AWAITING_TEMPERATURE = "input_boolean.sonya_direct_awaiting_coffee_temperature"
DIRECT_AWAITING_SYRUP = "input_boolean.sonya_direct_awaiting_coffee_syrup"


@dataclass(frozen=True)
class SonyaAnswer:
    answer: str
    intent: str
    dialog: str | None = None
    source: str | None = None
    request_id: str | None = None


@dataclass
class CoffeeDraft:
    temperature: str | None = None
    syrup: str | None = None

    def order_text(self) -> str:
        temperature = self.temperature or "кофе"
        syrup = self.syrup
        if syrup:
            return f"{temperature} {syrup}".strip()
        return temperature


class CoffeeWorkflow:
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
        self._latest_coffee_type: str | None = None
        self._coffee_type_by_message_id: dict[int, str] = {}
        self._tg_draft = CoffeeDraft()
        self._direct_draft = CoffeeDraft()
        self._sonya_orders: dict[int, CoffeeDraft] = {}

    async def start_telegram_question(self, chat_id: int, message_id: int | None) -> int | None:
        await self._ha.play_media(
            self._settings.bedroom_player_entity,
            "Соня, хочешь кофе?",
            TG_WANTS_DIALOG,
        )
        edited_id = await self._telegram_messages.safe_edit(
            chat_id,
            message_id,
            "Я уточняю у Сони, хочет ли она кофе...",
            delete_only(),
        )
        self._set_active_message(chat_id, edited_id)
        return edited_id

    async def handle_wants_answer(self, answer: SonyaAnswer) -> None:
        text = answer.answer.strip()
        normalized = text.lower()
        intent = answer.intent.strip()
        LOGGER.info("Sonya wants-coffee answer received: dialog=%s intent=%s answer=%r", answer.dialog, intent, text)

        if intent == "YANDEX.REJECT" or any(word in normalized for word in NEGATIVE_WORDS):
            await self._edit_active_or_send("Соня отказалась от кофе. Не включаю кофемашину.", delete_only())
            await self._show_admin_menu()
            return

        if intent == "YANDEX.CONFIRM" or any(word in normalized for word in POSITIVE_WORDS):
            self._tg_draft = CoffeeDraft()
            await self._ask_temperature(direct=False)
            await self._edit_active_or_send(
                "Соня хочет кофе.\nТип: <i>уточняю</i>\nСироп: <i>уточняю</i>",
                delete_only(),
            )
            return

        shown = text or "пустой ответ"
        coffee_type = normalize_coffee_answer(shown)
        self._latest_coffee_type = coffee_type
        await self._edit_active_or_send(
            f"Соня ответила: {_h(shown)}. Включить кофемашину?",
            confirmation(),
            coffee_type=coffee_type,
        )

    async def handle_temperature_answer(self, answer: SonyaAnswer, *, direct: bool) -> None:
        draft = self._direct_draft if direct else self._tg_draft
        draft.temperature = normalize_temperature(answer.answer)
        LOGGER.info("Sonya temperature received: dialog=%s direct=%s answer=%r", answer.dialog, direct, answer.answer)

        if direct:
            self._set_active_message(self._settings.telegram_admin_chat_id, None)

        await self._edit_active_or_send(
            f"Соня хочет кофе.\nТип: {draft.temperature}\nСироп: <i>уточняю</i>",
            delete_only(),
        )
        await self._ask_syrup(direct=direct)

    async def handle_syrup_answer(self, answer: SonyaAnswer, *, direct: bool) -> None:
        draft = self._direct_draft if direct else self._tg_draft
        draft.syrup = normalize_syrup(answer.answer)
        coffee_type = normalize_coffee_answer(draft.order_text())
        self._latest_coffee_type = coffee_type
        LOGGER.info("Sonya syrup received: dialog=%s direct=%s answer=%r", answer.dialog, direct, answer.answer)

        if direct:
            self._set_active_message(self._settings.telegram_admin_chat_id, None)

        await self._edit_active_or_send(
            f"Соня попросила: {_h(coffee_type)}. Включить кофемашину?",
            confirmation(),
            coffee_type=coffee_type,
        )

    async def notify_auto_enabled(self, answer: SonyaAnswer) -> None:
        coffee_type = normalize_coffee_answer(answer.answer)
        self._latest_coffee_type = coffee_type
        await self._telegram_messages.safe_send(
            self._settings.telegram_admin_chat_id,
            f"Соня заказала: {_h(coffee_type)}. Кофемашина уже включена.",
            reply_markup=delete_only(),
        )

    async def confirm_turn_on(self, chat_id: int, message_id: int | None) -> int | None:
        coffee_type = self._coffee_type_for_message(message_id)
        await self._ha.switch_turn_on(self._settings.coffee_switch_entity)
        edited_id = await self._telegram_messages.safe_edit(
            chat_id,
            message_id,
            f"Я включила кофемашину. Соня попросила: {_h(coffee_type)}.",
            delete_only(),
        )
        await self._say_bedroom("Твой кофе скоро будет готов.")
        await self._show_menu_for_chat(chat_id)
        return edited_id

    async def confirm_decline(self, chat_id: int, message_id: int | None) -> int | None:
        coffee_type = self._coffee_type_for_message(message_id)
        edited_id = await self._telegram_messages.safe_edit(
            chat_id,
            message_id,
            f"Я не включаю кофемашину. Соня попросила: {_h(coffee_type)}.",
            delete_only(),
        )
        await self._say_bedroom("Артём пока не включает кофемашину.")
        await self._show_menu_for_chat(chat_id)
        return edited_id

    async def schedule_reminder(self, chat_id: int, minutes: int, message_id: int | None = None) -> None:
        coffee_type = self._coffee_type_for_message(message_id)
        reminder = Reminder(chat_id=chat_id, minutes=minutes, reason="sonya_coffee", coffee_type=coffee_type)
        reminder_id = await self._storage.add_reminder(reminder)
        task = asyncio.create_task(self._remind_later(reminder_id, reminder))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        await self._show_menu_for_chat(chat_id)

    async def set_sonya_order_temperature(self, user_id: int, temperature: str) -> CoffeeDraft:
        draft = self._sonya_orders.setdefault(user_id, CoffeeDraft())
        draft.temperature = "холодный кофе" if temperature == "cold" else "горячий кофе"
        draft.syrup = None
        return draft

    async def set_sonya_order_syrup(self, user_id: int, syrup: str) -> CoffeeDraft:
        draft = self._sonya_orders.setdefault(user_id, CoffeeDraft())
        draft.syrup = "с сиропом" if syrup == "yes" else "без сиропа"
        return draft

    async def submit_sonya_order(self, user_id: int) -> str:
        draft = self._sonya_orders.get(user_id, CoffeeDraft())
        coffee_type = normalize_coffee_answer(draft.order_text())
        message_id = await self._telegram_messages.safe_send(
            self._settings.telegram_admin_chat_id,
            f"Соня заказала: {_h(coffee_type)}. Включить кофемашину?",
            reply_markup=confirmation(),
        )
        if message_id is not None:
            self._coffee_type_by_message_id[message_id] = coffee_type
        self._sonya_orders.pop(user_id, None)
        return coffee_type

    async def _ask_temperature(self, *, direct: bool) -> None:
        await self._ha.input_boolean_turn_on(DIRECT_AWAITING_TEMPERATURE if direct else TG_AWAITING_TEMPERATURE)
        await self._ha.play_media(
            self._settings.bedroom_player_entity,
            "Какой кофе ты хочешь: горячий или холодный?",
            DIRECT_TEMPERATURE_DIALOG if direct else TG_TEMPERATURE_DIALOG,
        )

    async def _ask_syrup(self, *, direct: bool) -> None:
        await self._ha.input_boolean_turn_on(DIRECT_AWAITING_SYRUP if direct else TG_AWAITING_SYRUP)
        await self._ha.play_media(
            self._settings.bedroom_player_entity,
            "С сиропом или без?",
            DIRECT_SYRUP_DIALOG if direct else TG_SYRUP_DIALOG,
        )

    async def _remind_later(self, reminder_id: int, reminder: Reminder) -> None:
        try:
            await asyncio.sleep(reminder.minutes * 60)
            coffee_type = reminder.coffee_type or "кофе"
            message_id = await self._telegram_messages.safe_send(
                reminder.chat_id,
                f"Напоминание: Соня просила кофе: {_h(coffee_type)}. Включить кофемашину?",
                reply_markup=confirmation(),
            )
            if message_id is not None:
                self._coffee_type_by_message_id[message_id] = coffee_type
        finally:
            await self._storage.remove_reminder(reminder_id)

    async def _edit_active_or_send(
        self,
        text: str,
        reply_markup: InlineKeyboardMarkup,
        coffee_type: str | None = None,
    ) -> None:
        chat_id = self._active_chat_id or self._settings.telegram_admin_chat_id
        message_id = self._active_message_id if self._active_chat_id else None
        edited_id = await self._telegram_messages.safe_edit(chat_id, message_id, text, reply_markup)
        self._set_active_message(chat_id, edited_id)
        if edited_id is not None and coffee_type:
            self._coffee_type_by_message_id[edited_id] = coffee_type

    def _set_active_message(self, chat_id: int, message_id: int | None) -> None:
        self._active_chat_id = chat_id
        self._active_message_id = message_id

    def _coffee_type_for_message(self, message_id: int | None) -> str:
        if message_id is not None and message_id in self._coffee_type_by_message_id:
            return self._coffee_type_by_message_id[message_id]
        return self._latest_coffee_type or "кофе"

    async def _say_bedroom(self, text: str) -> None:
        try:
            await self._ha.play_media(self._settings.bedroom_player_entity, text)
        except HomeAssistantError:
            LOGGER.exception("Cannot speak through bedroom media player")

    async def _show_admin_menu(self) -> None:
        await self._telegram_messages.safe_send(
            self._settings.telegram_admin_chat_id,
            "Я рядом. Что делаем дальше?",
            reply_markup=main_menu(),
        )

    async def _show_menu_for_chat(self, chat_id: int) -> None:
        if chat_id == self._settings.telegram_admin_chat_id:
            await self._telegram_messages.safe_send(chat_id, "Я рядом. Что делаем дальше?", reply_markup=main_menu())
            return
        await self._telegram_messages.safe_send(chat_id, "Что заказать?", reply_markup=sonya_order_menu())


def normalize_coffee_answer(answer: str) -> str:
    normalized = answer.strip().lower()
    if not normalized:
        return "кофе"
    if "кофе" in normalized:
        return normalized
    if "холодн" in normalized:
        return normalized.replace("холодный", "холодный кофе", 1)
    if "горяч" in normalized:
        return normalized.replace("горячий", "горячий кофе", 1)
    return normalized


def _h(value: str) -> str:
    return html.escape(value, quote=False)


def normalize_temperature(answer: str) -> str:
    normalized = answer.strip().lower()
    if "холод" in normalized:
        return "холодный кофе"
    if "горяч" in normalized:
        return "горячий кофе"
    return normalize_coffee_answer(normalized)


def normalize_syrup(answer: str) -> str:
    normalized = answer.strip().lower()
    if "без" in normalized:
        return "без сиропа"
    if "сироп" in normalized:
        return "с сиропом"
    return normalized or "без сиропа"
