from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from aiogram.types import InlineKeyboardMarkup

from app.config import Settings
from app.keyboards.coffee import delete_only
from app.keyboards.main import main_menu, sonya_order_menu
from app.keyboards.tea import tea_confirmation
from app.messages import tea as tea_messages
from app.messages.common import admin_menu_text, sonya_menu_text
from app.services.home_assistant import HomeAssistantClient, HomeAssistantError
from app.services.telegram_messages import TelegramMessages
from app.storage.base import Reminder
from app.storage.base import Storage

LOGGER = logging.getLogger(__name__)

KEEP_WARM_TEMPERATURES = (40, 50, 60, 70, 80, 90)

TG_TEA_WANTS_DIALOG = "dialog:домашний помощник:tg_ask_sonya_wants_tea"
TG_TEA_KEEP_WARM_DIALOG = "dialog:домашний помощник:tg_ask_sonya_tea_keep_warm"
TG_TEA_KEEP_WARM_TEMPERATURE_DIALOG = "dialog:домашний помощник:tg_ask_sonya_tea_keep_warm_temperature"
DIRECT_TEA_KEEP_WARM_DIALOG = "dialog:домашний помощник:sonya_direct_tea_keep_warm"
DIRECT_TEA_KEEP_WARM_TEMPERATURE_DIALOG = "dialog:домашний помощник:sonya_direct_tea_keep_warm_temperature"
HALL_TEA_WANTS_DIALOG = "dialog:домашний помощник:hall_ask_sonya_wants_tea"
HALL_TEA_KEEP_WARM_DIALOG = "dialog:домашний помощник:hall_ask_sonya_tea_keep_warm"
HALL_TEA_KEEP_WARM_TEMPERATURE_DIALOG = "dialog:домашний помощник:hall_ask_sonya_tea_keep_warm_temperature"

TG_AWAITING_TEA_WANTS = "input_boolean.tg_awaiting_sonya_tea_wants"
TG_AWAITING_TEA_KEEP_WARM = "input_boolean.tg_awaiting_sonya_tea_keep_warm"
TG_AWAITING_TEA_KEEP_WARM_TEMPERATURE = "input_boolean.tg_awaiting_sonya_tea_keep_warm_temperature"
DIRECT_AWAITING_TEA_KEEP_WARM = "input_boolean.sonya_direct_awaiting_tea_keep_warm"
DIRECT_AWAITING_TEA_KEEP_WARM_TEMPERATURE = "input_boolean.sonya_direct_awaiting_tea_keep_warm_temperature"
HALL_AWAITING_TEA_WANTS = "input_boolean.hall_awaiting_sonya_tea_wants"
HALL_AWAITING_TEA_KEEP_WARM = "input_boolean.hall_awaiting_sonya_tea_keep_warm"
HALL_AWAITING_TEA_KEEP_WARM_TEMPERATURE = "input_boolean.hall_awaiting_sonya_tea_keep_warm_temperature"

ALL_TEA_WAIT_FLAGS = (
    TG_AWAITING_TEA_WANTS,
    TG_AWAITING_TEA_KEEP_WARM,
    TG_AWAITING_TEA_KEEP_WARM_TEMPERATURE,
    DIRECT_AWAITING_TEA_KEEP_WARM,
    DIRECT_AWAITING_TEA_KEEP_WARM_TEMPERATURE,
    HALL_AWAITING_TEA_WANTS,
    HALL_AWAITING_TEA_KEEP_WARM,
    HALL_AWAITING_TEA_KEEP_WARM_TEMPERATURE,
)

POSITIVE_WORDS = ("да", "хочу", "включи", "с поддержанием", "поддержание", "держи тепло", "поддерживать")
NEGATIVE_WORDS = ("нет", "не надо", "без", "без поддержания", "не нужно")
TEMPERATURE_WORDS = {
    "40": 40,
    "сорок": 40,
    "50": 50,
    "пятьдесят": 50,
    "60": 60,
    "шестьдесят": 60,
    "70": 70,
    "семьдесят": 70,
    "80": 80,
    "восемьдесят": 80,
    "90": 90,
    "девяносто": 90,
}


@dataclass(frozen=True)
class TeaAnswer:
    answer: str
    intent: str
    dialog: str | None = None
    source: str | None = None
    request_id: str | None = None


@dataclass
class TeaDraft:
    keep_warm: bool | None = None
    keep_warm_temperature: int | None = None


@dataclass(frozen=True)
class TeaContext:
    keep_warm_temperature: int | None = None
    is_reminder: bool = False
    source: str = "voice"
    speak_to_bedroom_on_confirm: bool = True
    speak_to_bedroom_on_decline: bool = True


class TeaWorkflow:
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
        self._latest_context: TeaContext | None = None
        self._context_by_message_id: dict[int, TeaContext] = {}
        self._tg_draft = TeaDraft()
        self._direct_draft = TeaDraft()
        self._hall_draft = TeaDraft()
        self._sonya_orders: dict[int, TeaDraft] = {}
        self._recent_event_keys: dict[str, float] = {}
        self._dedupe_ttl_seconds = 5.0

    async def start_telegram_question(self, chat_id: int, message_id: int | None) -> int | None:
        await self._clear_all_wait_flags()
        await self._set_only_wait_flag(TG_AWAITING_TEA_WANTS)
        await self._ha.play_media(self._settings.bedroom_player_entity, "Соня, тебя спрашивают: будешь чай?", TG_TEA_WANTS_DIALOG)
        edited_id = await self._telegram_messages.safe_edit(
            chat_id,
            message_id,
            tea_messages.tea_questioning(),
            delete_only(),
        )
        self._set_active_message(chat_id, edited_id)
        return edited_id

    async def start_hall_question(self) -> None:
        await self._clear_all_wait_flags()
        await self._set_only_wait_flag(HALL_AWAITING_TEA_WANTS)
        await self._ha.play_media(self._settings.bedroom_player_entity, "Соня, тебя спрашивают: будешь чай?", HALL_TEA_WANTS_DIALOG)

    async def handle_wants_answer(self, answer: TeaAnswer, *, flow_type: str = "telegram") -> None:
        if self._is_duplicate_event("wants", answer):
            return

        normalized = answer.answer.strip().lower()
        intent = answer.intent.strip()
        self._log_step(flow_type, "wants", normalized, "received answer")
        if intent == "YANDEX.REJECT" or _matches_answer(normalized, NEGATIVE_WORDS):
            await self._clear_all_wait_flags()
            if flow_type == "hall":
                await self._ha.play_media(self._settings.living_room_player_entity, "Соня отказалась от чая.")
                await self._telegram_messages.safe_send(
                    self._settings.telegram_admin_chat_id,
                    tea_messages.tea_hall_refused(),
                    reply_markup=delete_only(),
                )
                return
            await self._edit_active_or_send(tea_messages.tea_refused(), delete_only())
            await self._show_admin_menu()
            return

        if intent == "YANDEX.CONFIRM" or _matches_answer(normalized, POSITIVE_WORDS):
            if flow_type == "hall":
                self._hall_draft = TeaDraft()
                await self.boil()
                await self._ask_keep_warm(flow_type="hall")
            else:
                self._tg_draft = TeaDraft()
                await self._ask_keep_warm(flow_type="telegram")
                await self._edit_active_or_send(tea_progress_text(), delete_only())
            return

        if flow_type == "hall":
            await self._ha.play_media(self._settings.bedroom_player_entity, "Скажи да или нет.", HALL_TEA_WANTS_DIALOG)
            return

        await self._clear_all_wait_flags()
        await self._edit_active_or_send(f"Соня ответила: {normalized or 'пусто'}. Я не поняла, включать чайник или нет.", delete_only())

    async def start_direct_request(self) -> None:
        self._direct_draft = TeaDraft()
        self._set_active_message(self._settings.telegram_admin_chat_id, None)
        await self._ask_keep_warm(flow_type="direct")

    async def handle_keep_warm_answer(self, answer: TeaAnswer, *, flow_type: str) -> None:
        if self._is_duplicate_event("keep_warm", answer):
            return

        draft = self._draft_for_flow(flow_type)
        normalized = answer.answer.strip().lower()
        intent = answer.intent.strip()
        self._log_step(flow_type, "keep_warm", normalized, "received answer")

        if intent == "YANDEX.REJECT" or _matches_answer(normalized, NEGATIVE_WORDS):
            draft.keep_warm = False
            draft.keep_warm_temperature = None
            await self._finish_order(flow_type=flow_type)
            return

        if intent == "YANDEX.CONFIRM" or _matches_answer(normalized, POSITIVE_WORDS):
            draft.keep_warm = True
            await self._ask_keep_warm_temperature(flow_type=flow_type)
            if flow_type == "telegram":
                await self._edit_active_or_send(tea_progress_text("уточняю"), delete_only())
            return

        await self._ha.play_media(self._settings.bedroom_player_entity, "Скажи да или нет.")

    async def handle_keep_warm_temperature_answer(self, answer: TeaAnswer, *, flow_type: str) -> None:
        if self._is_duplicate_event("keep_warm_temperature", answer):
            return

        temperature = parse_keep_warm_temperature(answer.answer)
        self._log_step(flow_type, "keep_warm_temperature", str(temperature or answer.answer), "received answer")
        if temperature is None:
            await self._ha.play_media(self._settings.bedroom_player_entity, "Выбери температуру от 40 до 90 градусов с шагом 10.")
            return

        draft = self._draft_for_flow(flow_type)
        draft.keep_warm = True
        draft.keep_warm_temperature = temperature
        await self._finish_order(flow_type=flow_type)

    async def notify_auto_enabled(self, answer: TeaAnswer) -> None:
        if self._is_duplicate_event("auto_enabled", answer):
            return

        context = TeaContext(keep_warm_temperature=parse_keep_warm_temperature(answer.answer), source="hall", speak_to_bedroom_on_confirm=False, speak_to_bedroom_on_decline=False)
        self._latest_context = context
        await self._clear_all_wait_flags()
        await self._telegram_messages.safe_send(
            self._settings.telegram_admin_chat_id,
            tea_auto_enabled_text(context),
            reply_markup=delete_only(),
        )

    async def notify_hall_refused(self) -> None:
        await self._clear_all_wait_flags()
        await self._telegram_messages.safe_send(
            self._settings.telegram_admin_chat_id,
            tea_messages.tea_hall_refused(),
            reply_markup=delete_only(),
        )

    async def submit_sonya_order(self, user_id: int) -> str:
        draft = self._sonya_orders.get(user_id, TeaDraft())
        context = context_from_draft(
            draft,
            source="sonya_telegram_order",
            speak_to_bedroom_on_confirm=False,
            speak_to_bedroom_on_decline=False,
        )
        self._latest_context = context
        message_id = await self._telegram_messages.safe_send(
            self._settings.telegram_admin_chat_id,
            tea_confirmation_text(context),
            reply_markup=tea_confirmation(),
        )
        if message_id is not None:
            self._context_by_message_id[message_id] = context
        self._sonya_orders.pop(user_id, None)
        return "чай"

    async def set_sonya_order_keep_warm(self, user_id: int, keep_warm: bool) -> TeaDraft:
        draft = self._sonya_orders.setdefault(user_id, TeaDraft())
        draft.keep_warm = keep_warm
        draft.keep_warm_temperature = None
        return draft

    async def set_sonya_order_temperature(self, user_id: int, temperature: int) -> TeaDraft:
        draft = self._sonya_orders.setdefault(user_id, TeaDraft())
        draft.keep_warm = True
        draft.keep_warm_temperature = temperature
        return draft

    async def confirm_turn_on(self, chat_id: int, message_id: int | None) -> int | None:
        context = self._context_for_message(message_id)
        self._log_step("telegram", "confirm", tea_keep_warm_label(context), f"turn on source={context.source}")
        await self._clear_all_wait_flags()
        await self.boil()
        if context.keep_warm_temperature is not None:
            self._schedule_post_boil_keep_warm(context.keep_warm_temperature)
        edited_id = await self._telegram_messages.safe_edit(
            chat_id,
            message_id,
            tea_started_text(context),
            delete_only(),
        )
        if context.speak_to_bedroom_on_confirm:
            self._schedule_bedroom_speech("Чай скоро будет готов.", delay_seconds=2.0)
        await self._show_menu_for_chat(chat_id)
        return edited_id

    async def confirm_decline(self, chat_id: int, message_id: int | None) -> int | None:
        context = self._context_for_message(message_id)
        self._log_step("telegram", "confirm", tea_keep_warm_label(context), f"decline source={context.source}")
        await self._clear_all_wait_flags()
        edited_id = await self._telegram_messages.safe_edit(
            chat_id,
            message_id,
            tea_messages.tea_declined(),
            delete_only(),
        )
        if context.speak_to_bedroom_on_decline:
            self._schedule_bedroom_speech("Артём пока не включает чайник.", delay_seconds=2.0)
        await self._show_menu_for_chat(chat_id)
        return edited_id

    async def schedule_reminder(self, chat_id: int, minutes: int, message_id: int | None = None) -> None:
        context = self._context_for_message(message_id)
        await self._clear_all_wait_flags()
        reminder = Reminder(
            chat_id=chat_id,
            minutes=minutes,
            reason="sonya_tea",
            tea_keep_warm_temperature=context.keep_warm_temperature,
        )
        reminder_id = await self._storage.add_reminder(reminder)
        task = asyncio.create_task(self._remind_later(reminder_id, reminder))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        await self._show_menu_for_chat(chat_id)

    async def boil(self) -> None:
        await self._ha.water_heater_set_temperature(self._settings.kettle_entity, 100)

    async def stop_kettle(self) -> None:
        keep_warm = await self._ha.get_state(self._settings.kettle_keep_warm_switch_entity)
        kettle = await self._ha.get_state(self._settings.kettle_entity)
        if (keep_warm or {}).get("state") == "on":
            await self._ha.switch_turn_off(self._settings.kettle_keep_warm_switch_entity)
        operation_mode = (kettle or {}).get("attributes", {}).get("operation_mode")
        if (kettle or {}).get("state") == "on" or operation_mode != "off":
            await self._ha.water_heater_set_operation_mode(self._settings.kettle_entity, "off")

    async def enable_keep_warm(self, temperature: int) -> None:
        if temperature not in KEEP_WARM_TEMPERATURES:
            raise ValueError(f"Unsupported keep-warm temperature: {temperature}")
        await self._ha.water_heater_set_temperature(self._settings.kettle_entity, temperature)
        await self._ha.switch_turn_on(self._settings.kettle_keep_warm_switch_entity)

    async def disable_keep_warm(self) -> None:
        await self._ha.switch_turn_off(self._settings.kettle_keep_warm_switch_entity)

    async def reset_after_failure(self) -> None:
        LOGGER.warning("Tea workflow failed, resetting tea flags", exc_info=True)
        self._active_chat_id = None
        self._active_message_id = None
        self._tg_draft = TeaDraft()
        self._direct_draft = TeaDraft()
        self._hall_draft = TeaDraft()
        await self._clear_all_wait_flags()

    async def _ask_keep_warm(self, *, flow_type: str) -> None:
        if flow_type == "direct":
            flag = DIRECT_AWAITING_TEA_KEEP_WARM
            dialog = DIRECT_TEA_KEEP_WARM_DIALOG
        elif flow_type == "hall":
            flag = HALL_AWAITING_TEA_KEEP_WARM
            dialog = HALL_TEA_KEEP_WARM_DIALOG
        else:
            flag = TG_AWAITING_TEA_KEEP_WARM
            dialog = TG_TEA_KEEP_WARM_DIALOG
        await self._set_only_wait_flag(flag)
        await self._ha.play_media(self._settings.bedroom_player_entity, "Включить поддержание тепла?", dialog)

    async def _ask_keep_warm_temperature(self, *, flow_type: str) -> None:
        if flow_type == "direct":
            flag = DIRECT_AWAITING_TEA_KEEP_WARM_TEMPERATURE
            dialog = DIRECT_TEA_KEEP_WARM_TEMPERATURE_DIALOG
        elif flow_type == "hall":
            flag = HALL_AWAITING_TEA_KEEP_WARM_TEMPERATURE
            dialog = HALL_TEA_KEEP_WARM_TEMPERATURE_DIALOG
        else:
            flag = TG_AWAITING_TEA_KEEP_WARM_TEMPERATURE
            dialog = TG_TEA_KEEP_WARM_TEMPERATURE_DIALOG
        await self._set_only_wait_flag(flag)
        await self._ha.play_media(self._settings.bedroom_player_entity, "На какой температуре поддерживать тепло: 40, 50, 60, 70, 80 или 90 градусов?", dialog)

    async def _finish_order(self, *, flow_type: str) -> None:
        draft = self._draft_for_flow(flow_type)
        context = context_from_draft(
            draft,
            source=flow_type,
            speak_to_bedroom_on_confirm=flow_type != "hall",
            speak_to_bedroom_on_decline=flow_type != "hall",
        )
        self._latest_context = context
        if flow_type == "direct":
            self._set_active_message(self._settings.telegram_admin_chat_id, None)
        await self._clear_all_wait_flags()
        if flow_type == "hall":
            if context.keep_warm_temperature is not None:
                self._schedule_post_boil_keep_warm(context.keep_warm_temperature)
            await self._ha.play_media(self._settings.living_room_player_entity, tea_hall_result_speech(context))
            await self._telegram_messages.safe_send(
                self._settings.telegram_admin_chat_id,
                tea_auto_enabled_text(context),
                reply_markup=delete_only(),
            )
            return
        await self._edit_active_or_send(tea_confirmation_text(context), tea_confirmation(), context=context)

    async def _remind_later(self, reminder_id: int, reminder: Reminder) -> None:
        try:
            await asyncio.sleep(reminder.minutes * 60)
            context = TeaContext(
                keep_warm_temperature=reminder.tea_keep_warm_temperature,
                is_reminder=True,
                source="reminder",
                speak_to_bedroom_on_confirm=False,
                speak_to_bedroom_on_decline=False,
            )
            message_id = await self._telegram_messages.safe_send(
                reminder.chat_id,
                tea_reminder_text(context),
                reply_markup=tea_confirmation(),
            )
            if message_id is not None:
                self._context_by_message_id[message_id] = context
        finally:
            await self._storage.remove_reminder(reminder_id)

    async def _edit_active_or_send(self, text: str, reply_markup: InlineKeyboardMarkup, context: TeaContext | None = None) -> None:
        chat_id = self._active_chat_id or self._settings.telegram_admin_chat_id
        message_id = self._active_message_id if self._active_chat_id else None
        edited_id = await self._telegram_messages.safe_edit(chat_id, message_id, text, reply_markup)
        action = "edited message" if message_id and edited_id == message_id else "created message"
        LOGGER.info("Tea workflow Telegram message: action=%s chat_id=%s message_id=%s previous_message_id=%s", action, chat_id, edited_id, message_id)
        self._set_active_message(chat_id, edited_id)
        if edited_id is not None and context:
            self._context_by_message_id[edited_id] = context

    def _set_active_message(self, chat_id: int, message_id: int | None) -> None:
        self._active_chat_id = chat_id
        self._active_message_id = message_id

    def _context_for_message(self, message_id: int | None) -> TeaContext:
        if message_id is not None and message_id in self._context_by_message_id:
            return self._context_by_message_id[message_id]
        return self._latest_context or TeaContext()

    def _draft_for_flow(self, flow_type: str) -> TeaDraft:
        if flow_type == "direct":
            return self._direct_draft
        if flow_type == "hall":
            return self._hall_draft
        return self._tg_draft

    async def _set_only_wait_flag(self, entity_id: str) -> None:
        await self._clear_all_wait_flags()
        await self._ha.input_boolean_turn_on(entity_id)

    async def _clear_all_wait_flags(self) -> None:
        for entity_id in ALL_TEA_WAIT_FLAGS:
            try:
                await self._ha.input_boolean_turn_off(entity_id)
            except HomeAssistantError:
                LOGGER.exception("Cannot clear tea wait flag: %s", entity_id)

    def _schedule_post_boil_keep_warm(self, temperature: int) -> None:
        async def watch() -> None:
            try:
                deadline = time.monotonic() + 15 * 60
                while time.monotonic() < deadline:
                    await asyncio.sleep(10)
                    state = await self._ha.get_state(self._settings.kettle_entity)
                    attrs = (state or {}).get("attributes", {})
                    current_temperature = attrs.get("current_temperature")
                    operation_mode = attrs.get("operation_mode")
                    if _as_float(current_temperature) >= 98 or (state or {}).get("state") == "off" or operation_mode == "off":
                        await self.enable_keep_warm(temperature)
                        LOGGER.info("Tea post-boil keep-warm enabled: temperature=%s", temperature)
                        return
                LOGGER.warning("Tea post-boil keep-warm watcher timed out: temperature=%s", temperature)
            except Exception:
                LOGGER.exception("Cannot enable tea post-boil keep-warm")

        task = asyncio.create_task(watch())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _schedule_bedroom_speech(self, text: str, *, delay_seconds: float) -> None:
        async def delayed_speech() -> None:
            await asyncio.sleep(delay_seconds)
            try:
                await self._ha.play_media(self._settings.bedroom_player_entity, text)
            except HomeAssistantError:
                LOGGER.exception("Cannot speak through bedroom media player")

        task = asyncio.create_task(delayed_speech())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _show_admin_menu(self) -> None:
        await self._telegram_messages.safe_send(self._settings.telegram_admin_chat_id, admin_menu_text(), reply_markup=main_menu())

    async def _show_menu_for_chat(self, chat_id: int) -> None:
        if chat_id == self._settings.telegram_admin_chat_id:
            await self._telegram_messages.safe_send(chat_id, admin_menu_text(), reply_markup=main_menu())
            return
        await self._telegram_messages.safe_send(chat_id, sonya_menu_text(), reply_markup=sonya_order_menu())

    def _is_duplicate_event(self, step: str, answer: TeaAnswer) -> bool:
        now = time.monotonic()
        self._recent_event_keys = {key: seen_at for key, seen_at in self._recent_event_keys.items() if now - seen_at <= self._dedupe_ttl_seconds}
        normalized_answer = " ".join(answer.answer.strip().lower().split())
        normalized_intent = answer.intent.strip()
        key = f"{step}:{answer.dialog}:{normalized_answer}:{normalized_intent}"
        if key in self._recent_event_keys:
            LOGGER.info("Duplicate tea event ignored: step=%s dialog=%s intent=%s answer=%r", step, answer.dialog, answer.intent, answer.answer)
            return True
        self._recent_event_keys[key] = now
        return False

    def _log_step(self, flow_type: str, step: str, normalized_answer: str, action: str) -> None:
        LOGGER.info(
            "Tea workflow step: flow=%s step=%s normalized_answer=%r active_message_id=%s order_state=%s action=%s",
            flow_type,
            step,
            normalized_answer,
            self._active_message_id,
            {
                "telegram": self._tg_draft,
                "direct": self._direct_draft,
            },
            action,
        )


def context_from_draft(
    draft: TeaDraft,
    *,
    source: str = "voice",
    speak_to_bedroom_on_confirm: bool = True,
    speak_to_bedroom_on_decline: bool = True,
) -> TeaContext:
    return TeaContext(
        keep_warm_temperature=draft.keep_warm_temperature if draft.keep_warm else None,
        source=source,
        speak_to_bedroom_on_confirm=speak_to_bedroom_on_confirm,
        speak_to_bedroom_on_decline=speak_to_bedroom_on_decline,
    )


def parse_keep_warm_temperature(answer: str) -> int | None:
    normalized = answer.strip().lower().replace("°", "").replace("градусов", "").replace("градуса", "").replace("градус", "").strip()
    for key, value in TEMPERATURE_WORDS.items():
        if key in normalized:
            return value
    return None


def tea_keep_warm_label(context: TeaContext) -> str:
    return f"{context.keep_warm_temperature} °C" if context.keep_warm_temperature is not None else "нет"


def tea_progress_text(keep_warm: str = "уточняю") -> str:
    return tea_messages.tea_progress(keep_warm)


def tea_confirmation_text(context: TeaContext) -> str:
    return tea_messages.tea_confirmation(tea_keep_warm_label(context))


def tea_auto_enabled_text(context: TeaContext) -> str:
    return tea_messages.tea_auto_enabled(tea_keep_warm_label(context))


def tea_hall_result_speech(context: TeaContext) -> str:
    if context.keep_warm_temperature is None:
        return "Соня заказала чай. Чайник уже включен."
    return f"Соня заказала чай. Чайник уже включен. Поддержание тепла: {context.keep_warm_temperature} градусов."


def tea_reminder_text(context: TeaContext) -> str:
    return tea_messages.tea_reminder(tea_keep_warm_label(context))


def tea_started_text(context: TeaContext) -> str:
    return tea_messages.tea_started(context.keep_warm_temperature)


def _matches_answer(normalized: str, words: tuple[str, ...]) -> bool:
    tokens = set(normalized.replace(",", " ").replace(".", " ").split())
    for word in words:
        if " " in word:
            if word in normalized:
                return True
        elif word in tokens or normalized == word:
            return True
    return False


def _as_float(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
