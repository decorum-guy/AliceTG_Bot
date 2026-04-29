from __future__ import annotations

import asyncio
import html
import logging
import time
from dataclasses import dataclass

from aiogram.types import InlineKeyboardMarkup

from app.config import Settings
from app.keyboards.coffee import confirmation, delete_only
from app.keyboards.main import main_menu, sonya_order_menu
from app.messages import coffee as coffee_messages
from app.messages.common import admin_menu_text, sonya_menu_text
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
HALL_AWAITING_TEMPERATURE = "input_boolean.hall_awaiting_sonya_coffee_temperature"
HALL_AWAITING_SYRUP = "input_boolean.hall_awaiting_sonya_coffee_syrup"
ALL_COFFEE_WAIT_FLAGS = (
    TG_AWAITING_TEMPERATURE,
    TG_AWAITING_SYRUP,
    DIRECT_AWAITING_TEMPERATURE,
    DIRECT_AWAITING_SYRUP,
    HALL_AWAITING_TEMPERATURE,
    HALL_AWAITING_SYRUP,
)


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


@dataclass(frozen=True)
class CoffeeMessageContext:
    coffee_type: str
    temperature: str | None = None
    syrup: str | None = None
    is_reminder: bool = False
    source: str = "voice"
    speak_to_bedroom_on_confirm: bool = True
    speak_to_bedroom_on_decline: bool = True
    bedroom_ack_enabled: bool = False


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
        self._latest_context: CoffeeMessageContext | None = None
        self._context_by_message_id: dict[int, CoffeeMessageContext] = {}
        self._tg_draft = CoffeeDraft()
        self._direct_draft = CoffeeDraft()
        self._sonya_orders: dict[int, CoffeeDraft] = {}
        self._recent_event_keys: dict[str, float] = {}
        self._dedupe_ttl_seconds = 5.0

    async def start_telegram_question(self, chat_id: int, message_id: int | None) -> int | None:
        await self._clear_all_wait_flags()
        await self._ha.play_media(
            self._settings.bedroom_player_entity,
            "Соня, тебя спрашивают: будешь кофе?",
            TG_WANTS_DIALOG,
        )
        edited_id = await self._telegram_messages.safe_edit(
            chat_id,
            message_id,
            coffee_messages.coffee_questioning(),
            delete_only(),
        )
        self._set_active_message(chat_id, edited_id)
        return edited_id

    async def handle_wants_answer(self, answer: SonyaAnswer) -> None:
        if self._is_duplicate_event("wants", answer):
            return

        text = answer.answer.strip()
        normalized = text.lower()
        intent = answer.intent.strip()
        LOGGER.info("Sonya wants-coffee answer received: dialog=%s intent=%s answer=%r", answer.dialog, intent, text)
        self._log_step("telegram", "wants", normalized, "received answer")

        if intent == "YANDEX.REJECT" or any(word in normalized for word in NEGATIVE_WORDS):
            await self._clear_all_wait_flags()
            await self._edit_active_or_send(coffee_messages.coffee_refused(), delete_only())
            await self._show_admin_menu()
            return

        if intent == "YANDEX.CONFIRM" or any(word in normalized for word in POSITIVE_WORDS):
            self._tg_draft = CoffeeDraft()
            await self._ask_temperature(direct=False)
            await self._edit_active_or_send(
                order_progress_text(None, None),
                delete_only(),
            )
            return

        shown = text or "пустой ответ"
        coffee_type = normalize_coffee_answer(shown)
        context = CoffeeMessageContext(coffee_type=coffee_type)
        self._latest_context = context
        await self._clear_all_wait_flags()
        await self._edit_active_or_send(
            coffee_messages.coffee_unknown_answer(shown),
            confirmation(),
            context=context,
        )

    async def handle_temperature_answer(self, answer: SonyaAnswer, *, direct: bool) -> None:
        if self._is_duplicate_event("temperature", answer):
            return

        draft = self._direct_draft if direct else self._tg_draft
        draft.temperature = normalize_temperature(answer.answer)
        LOGGER.info("Sonya temperature received: dialog=%s direct=%s answer=%r", answer.dialog, direct, answer.answer)
        flow_type = "direct" if direct else "telegram"
        self._log_step(flow_type, "temperature", draft.temperature, "received answer")

        if direct:
            self._set_active_message(self._settings.telegram_admin_chat_id, None)
            self._log_step(flow_type, "temperature", draft.temperature, "skipped intermediate message")
        else:
            await self._edit_active_or_send(
                order_progress_text(draft.temperature, None),
                delete_only(),
            )
        await self._ask_syrup(direct=direct)

    async def handle_syrup_answer(self, answer: SonyaAnswer, *, direct: bool) -> None:
        if self._is_duplicate_event("syrup", answer):
            return

        draft = self._direct_draft if direct else self._tg_draft
        draft.syrup = normalize_syrup(answer.answer)
        flow_type = "direct" if direct else "telegram"
        context = context_from_draft(draft, source=flow_type, bedroom_ack_enabled=True)
        self._latest_context = context
        LOGGER.info("Sonya syrup received: dialog=%s direct=%s answer=%r", answer.dialog, direct, answer.answer)
        self._log_step(flow_type, "syrup", draft.syrup or answer.answer, "received answer")

        if direct:
            self._set_active_message(self._settings.telegram_admin_chat_id, None)

        await self._clear_all_wait_flags()
        await self._edit_active_or_send(
            order_confirmation_text(context),
            confirmation(),
            context=context,
        )
        await self._say_bedroom_ack(context)

    async def notify_auto_enabled(self, answer: SonyaAnswer) -> None:
        if self._is_duplicate_event("auto_enabled", answer):
            return

        context = context_from_text(answer.answer)
        self._latest_context = context
        self._log_step(
            "hall",
            "final",
            context.coffee_type,
            "auto_enabled=True telegram info only / no confirmation buttons",
        )
        await self._clear_all_wait_flags()
        await self._telegram_messages.safe_send(
            self._settings.telegram_admin_chat_id,
            auto_enabled_text(context),
            reply_markup=delete_only(),
        )

    async def notify_hall_refused(self) -> None:
        self._log_step(
            "hall",
            "wants",
            "нет",
            "auto_enabled=False telegram info only / no confirmation buttons",
        )
        await self._clear_all_wait_flags()
        await self._telegram_messages.safe_send(
            self._settings.telegram_admin_chat_id,
            coffee_messages.coffee_hall_refused(),
            reply_markup=delete_only(),
        )

    async def confirm_turn_on(self, chat_id: int, message_id: int | None) -> int | None:
        context = self._context_for_message(message_id)
        self._log_step("telegram", "confirm", context.coffee_type, f"turn on source={context.source}")
        await self._clear_all_wait_flags()
        await self._ha.switch_turn_on(self._settings.coffee_switch_entity)
        edited_id = await self._telegram_messages.safe_edit(
            chat_id,
            message_id,
            coffee_messages.coffee_started(context.coffee_type),
            delete_only(),
        )
        if context.speak_to_bedroom_on_confirm:
            self._schedule_bedroom_speech("Твой кофе скоро будет готов.", delay_seconds=2.0)
        await self._show_menu_for_chat(chat_id)
        return edited_id

    async def confirm_decline(self, chat_id: int, message_id: int | None) -> int | None:
        context = self._context_for_message(message_id)
        self._log_step("telegram", "confirm", context.coffee_type, f"decline source={context.source}")
        await self._clear_all_wait_flags()
        edited_id = await self._telegram_messages.safe_edit(
            chat_id,
            message_id,
            coffee_messages.coffee_declined(context.coffee_type),
            delete_only(),
        )
        if context.speak_to_bedroom_on_decline:
            self._schedule_bedroom_speech("Артём пока не включает кофемашину.", delay_seconds=2.0)
        await self._show_menu_for_chat(chat_id)
        return edited_id

    async def schedule_reminder(self, chat_id: int, minutes: int, message_id: int | None = None) -> None:
        context = self._context_for_message(message_id)
        await self._clear_all_wait_flags()
        reminder = Reminder(
            chat_id=chat_id,
            minutes=minutes,
            reason="sonya_coffee",
            coffee_type=context.coffee_type,
            coffee_temperature=context.temperature,
            coffee_syrup=context.syrup,
        )
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
        context = context_from_draft(
            draft,
            source="sonya_telegram_order",
            speak_to_bedroom_on_confirm=False,
            speak_to_bedroom_on_decline=False,
        )
        self._latest_context = context
        message_id = await self._telegram_messages.safe_send(
            self._settings.telegram_admin_chat_id,
            order_confirmation_text(context),
            reply_markup=confirmation(),
        )
        if message_id is not None:
            self._context_by_message_id[message_id] = context
        self._sonya_orders.pop(user_id, None)
        return context.coffee_type

    async def _ask_temperature(self, *, direct: bool) -> None:
        await self._set_only_wait_flag(DIRECT_AWAITING_TEMPERATURE if direct else TG_AWAITING_TEMPERATURE)
        await self._ha.play_media(
            self._settings.bedroom_player_entity,
            "Какой кофе ты хочешь: горячий или холодный?",
            DIRECT_TEMPERATURE_DIALOG if direct else TG_TEMPERATURE_DIALOG,
        )

    async def _ask_syrup(self, *, direct: bool) -> None:
        await self._set_only_wait_flag(DIRECT_AWAITING_SYRUP if direct else TG_AWAITING_SYRUP)
        await self._ha.play_media(
            self._settings.bedroom_player_entity,
            "С сиропом или без?",
            DIRECT_SYRUP_DIALOG if direct else TG_SYRUP_DIALOG,
        )

    async def _remind_later(self, reminder_id: int, reminder: Reminder) -> None:
        try:
            await asyncio.sleep(reminder.minutes * 60)
            context = CoffeeMessageContext(
                coffee_type=reminder.coffee_type or "кофе",
                temperature=reminder.coffee_temperature,
                syrup=reminder.coffee_syrup,
                is_reminder=True,
                source="reminder",
                speak_to_bedroom_on_confirm=False,
                speak_to_bedroom_on_decline=False,
            )
            message_id = await self._telegram_messages.safe_send(
                reminder.chat_id,
                reminder_text(context),
                reply_markup=confirmation(),
            )
            if message_id is not None:
                self._context_by_message_id[message_id] = context
        finally:
            await self._storage.remove_reminder(reminder_id)

    async def _edit_active_or_send(
        self,
        text: str,
        reply_markup: InlineKeyboardMarkup,
        context: CoffeeMessageContext | None = None,
    ) -> None:
        chat_id = self._active_chat_id or self._settings.telegram_admin_chat_id
        message_id = self._active_message_id if self._active_chat_id else None
        edited_id = await self._telegram_messages.safe_edit(chat_id, message_id, text, reply_markup)
        action = "edited message" if message_id and edited_id == message_id else "created message"
        LOGGER.info(
            "Coffee workflow Telegram message: action=%s chat_id=%s message_id=%s previous_message_id=%s",
            action,
            chat_id,
            edited_id,
            message_id,
        )
        self._set_active_message(chat_id, edited_id)
        if edited_id is not None and context:
            self._context_by_message_id[edited_id] = context

    def _set_active_message(self, chat_id: int, message_id: int | None) -> None:
        self._active_chat_id = chat_id
        self._active_message_id = message_id

    def _context_for_message(self, message_id: int | None) -> CoffeeMessageContext:
        if message_id is not None and message_id in self._context_by_message_id:
            return self._context_by_message_id[message_id]
        return self._latest_context or CoffeeMessageContext(coffee_type="кофе")

    async def reset_after_failure(self) -> None:
        LOGGER.warning("Coffee workflow failed, resetting coffee flags", exc_info=True)
        self._active_chat_id = None
        self._active_message_id = None
        self._tg_draft = CoffeeDraft()
        self._direct_draft = CoffeeDraft()
        await self._clear_all_wait_flags()

    async def _set_only_wait_flag(self, entity_id: str) -> None:
        await self._clear_all_wait_flags()
        try:
            await self._ha.input_boolean_turn_on(entity_id)
        except HomeAssistantError:
            LOGGER.exception("Cannot enable coffee wait flag: %s", entity_id)
            raise

    async def _clear_all_wait_flags(self) -> None:
        for entity_id in ALL_COFFEE_WAIT_FLAGS:
            try:
                await self._ha.input_boolean_turn_off(entity_id)
            except HomeAssistantError:
                LOGGER.exception("Cannot clear coffee wait flag: %s", entity_id)

    def _is_duplicate_event(self, step: str, answer: SonyaAnswer) -> bool:
        now = time.monotonic()
        self._recent_event_keys = {
            key: seen_at
            for key, seen_at in self._recent_event_keys.items()
            if now - seen_at <= self._dedupe_ttl_seconds
        }
        normalized_answer = " ".join(answer.answer.strip().lower().split())
        normalized_intent = answer.intent.strip()
        key = f"{step}:{normalized_answer}:{normalized_intent}"
        if key in self._recent_event_keys:
            LOGGER.info(
                "Duplicate coffee event ignored: step=%s dialog=%s intent=%s answer=%r",
                step,
                answer.dialog,
                answer.intent,
                answer.answer,
            )
            return True
        self._recent_event_keys[key] = now
        return False

    def _log_step(self, flow_type: str, step: str, normalized_answer: str, action: str) -> None:
        LOGGER.info(
            "Coffee workflow step: flow=%s step=%s normalized_answer=%r active_message_id=%s order_state=%s action=%s",
            flow_type,
            step,
            normalized_answer,
            self._active_message_id,
            {
                "telegram": self._tg_draft.order_text(),
                "direct": self._direct_draft.order_text(),
            },
            action,
        )

    async def _say_bedroom(self, text: str) -> None:
        try:
            await self._ha.play_media(self._settings.bedroom_player_entity, text)
        except HomeAssistantError:
            LOGGER.exception("Cannot speak through bedroom media player")

    def _schedule_bedroom_speech(self, text: str, *, delay_seconds: float) -> None:
        async def delayed_speech() -> None:
            await asyncio.sleep(delay_seconds)
            await self._say_bedroom(text)

        task = asyncio.create_task(delayed_speech())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _say_bedroom_ack(self, context: CoffeeMessageContext) -> None:
        if not context.bedroom_ack_enabled:
            return

        syrup = context.syrup or "без сиропа"
        await self._say_bedroom(f"Хорошо, {context.temperature or context.coffee_type} {syrup}, поняла.")

    async def _show_admin_menu(self) -> None:
        await self._telegram_messages.safe_send(
            self._settings.telegram_admin_chat_id,
            admin_menu_text(),
            reply_markup=main_menu(),
        )

    async def _show_menu_for_chat(self, chat_id: int) -> None:
        if chat_id == self._settings.telegram_admin_chat_id:
            await self._telegram_messages.safe_send(chat_id, admin_menu_text(), reply_markup=main_menu())
            return
        await self._telegram_messages.safe_send(chat_id, sonya_menu_text(), reply_markup=sonya_order_menu())


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


def context_from_draft(
    draft: CoffeeDraft,
    *,
    source: str = "voice",
    speak_to_bedroom_on_confirm: bool = True,
    speak_to_bedroom_on_decline: bool = True,
    bedroom_ack_enabled: bool = False,
) -> CoffeeMessageContext:
    return CoffeeMessageContext(
        coffee_type=normalize_coffee_answer(draft.order_text()),
        temperature=draft.temperature,
        syrup=draft.syrup,
        source=source,
        speak_to_bedroom_on_confirm=speak_to_bedroom_on_confirm,
        speak_to_bedroom_on_decline=speak_to_bedroom_on_decline,
        bedroom_ack_enabled=bedroom_ack_enabled,
    )


def context_from_text(answer: str) -> CoffeeMessageContext:
    coffee_type = normalize_coffee_answer(answer)
    syrup = normalize_syrup(answer) if ("сироп" in answer.lower() or "без" in answer.lower()) else None
    return CoffeeMessageContext(
        coffee_type=coffee_type,
        temperature=normalize_temperature(answer),
        syrup=syrup,
    )


def order_progress_text(temperature: str | None, syrup: str | None) -> str:
    return coffee_messages.coffee_order_progress(temperature, syrup)


def order_confirmation_text(context: CoffeeMessageContext) -> str:
    return coffee_messages.coffee_order_confirmation(context.temperature or context.coffee_type, context.syrup or "не указано")


def auto_enabled_text(context: CoffeeMessageContext) -> str:
    return coffee_messages.coffee_auto_enabled(context.temperature or context.coffee_type, context.syrup or "не указано")


def reminder_text(context: CoffeeMessageContext) -> str:
    return coffee_messages.coffee_reminder(context.temperature or context.coffee_type, context.syrup or "не указано")


def minute_word(minutes: int) -> str:
    if 11 <= minutes % 100 <= 14:
        return "минут"
    if minutes % 10 == 1:
        return "минуту"
    if minutes % 10 in {2, 3, 4}:
        return "минуты"
    return "минут"


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
    if "не хочу" in normalized or "нет" in normalized or "без" in normalized:
        return "без сиропа"
    if normalized in {"да", "хочу"} or "сироп" in normalized:
        return "с сиропом"
    return normalized or "без сиропа"
