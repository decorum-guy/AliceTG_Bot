from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from dataclasses import dataclass

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from app.config import Settings
from app.keyboards.coffee import (
    coffee_alert_settings,
    coffee_alert_time_confirm,
    coffee_pushward_settings,
    coffee_settings,
    coffee_status,
    coffee_turn_off_only,
    delete_only,
    later_options,
    sonya_menu,
)
from app.keyboards.main import sonya_order_confirm_menu, sonya_order_menu, sonya_syrup_menu, sonya_temperature_menu
from app.messages import coffee as coffee_messages
from app.services.app_state import AppStatePersistenceError, AppStateStore
from app.services.coffee_alerts import CoffeeAlertScheduler
from app.services.coffee_timing_policy import CoffeeTimingPolicyService, TimingPolicyError
from app.services.coffee_machine import turn_off_coffee_machine, turn_on_coffee_machine
from app.services.home_assistant import HomeAssistantClient, HomeAssistantError
from app.services.telegram_messages import TelegramMessages
from app.workflows.coffee import CoffeeWorkflow
from app.workflows.coffee import minute_word

router = Router()
LOGGER = logging.getLogger(__name__)
COFFEE_ALERTS = {"warmed_up", "long_running"}
COFFEE_ALERT_TIME_RE = re.compile(r"^\s*(?P<value>\d{1,3})\s*(?P<unit>мин|минута|минуты|минут|ч|час|часа|часов)\s*$", re.IGNORECASE)


@dataclass
class CoffeeAlertTimeDraft:
    alert: str
    delay_seconds: int | None = None


_coffee_alert_time_drafts: dict[int, CoffeeAlertTimeDraft] = {}


@router.callback_query(F.data == "coffee:status")
async def show_coffee_status(
    callback: CallbackQuery,
    settings: Settings,
    ha: HomeAssistantClient,
    telegram_messages: TelegramMessages,
) -> None:
    if not settings.is_admin_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    text, is_on = await _coffee_status_text(settings, ha)
    if callback.message:
        await telegram_messages.safe_edit(callback.message.chat.id, callback.message.message_id, text, coffee_status(is_on))
    await callback.answer("Обновила")


@router.callback_query(F.data == "coffee:settings")
async def show_coffee_settings(
    callback: CallbackQuery,
    settings: Settings,
    app_state: AppStateStore,
    coffee_timing_policy: CoffeeTimingPolicyService,
) -> None:
    if not settings.is_admin_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    if callback.message:
        await callback.message.edit_text(
            _coffee_settings_text(app_state, coffee_timing_policy),
            reply_markup=coffee_settings(),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("coffee_alert_settings:"))
async def show_coffee_alert_settings(
    callback: CallbackQuery,
    settings: Settings,
    app_state: AppStateStore,
    coffee_timing_policy: CoffeeTimingPolicyService,
) -> None:
    if not settings.is_admin_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    alert = (callback.data or "").split(":", 1)[1]
    if alert not in COFFEE_ALERTS:
        await callback.answer("Неизвестная настройка", show_alert=True)
        return
    if callback.message:
        await callback.message.edit_text(
            _coffee_alert_settings_text(app_state, coffee_timing_policy, alert, settings),
            reply_markup=_coffee_alert_settings_markup(app_state, alert),
        )
    await callback.answer()


@router.callback_query(F.data == "coffee:pushward_settings")
async def show_coffee_pushward_settings(
    callback: CallbackQuery,
    settings: Settings,
    app_state: AppStateStore,
) -> None:
    if not settings.is_admin_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    if callback.message:
        await callback.message.edit_text(
            _coffee_pushward_settings_text(app_state, settings),
            reply_markup=coffee_pushward_settings(show_seconds=app_state.coffee_pushward_show_seconds),
        )
    await callback.answer()


@router.callback_query(F.data == "coffee:pushward_time_format:toggle")
async def toggle_coffee_pushward_time_format(
    callback: CallbackQuery,
    settings: Settings,
    app_state: AppStateStore,
    coffee_alert_scheduler: CoffeeAlertScheduler,
) -> None:
    if not settings.is_admin_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    show_seconds = not app_state.coffee_pushward_show_seconds
    await app_state.set_coffee_pushward_show_seconds(show_seconds)
    LOGGER.info("Coffee PushWard time display setting updated: show_seconds=%s", show_seconds)
    coffee_alert_scheduler.reschedule_active_alerts()
    if callback.message:
        await callback.message.edit_text(
            _coffee_pushward_settings_text(app_state, settings),
            reply_markup=coffee_pushward_settings(show_seconds=show_seconds),
        )
    await callback.answer("Сохранено")


@router.callback_query(F.data.startswith("coffee_alert_toggle:"))
async def toggle_coffee_alert(
    callback: CallbackQuery,
    settings: Settings,
    app_state: AppStateStore,
    coffee_alert_scheduler: CoffeeAlertScheduler,
    coffee_timing_policy: CoffeeTimingPolicyService,
) -> None:
    if not settings.is_admin_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    alert = (callback.data or "").split(":", 1)[1]
    if alert not in COFFEE_ALERTS:
        await callback.answer("Неизвестная настройка", show_alert=True)
        return
    enabled = not _coffee_alert_enabled(app_state, alert)
    try:
        changed = await _set_coffee_alert_enabled(app_state, alert, enabled)
    except AppStatePersistenceError:
        LOGGER.warning("Cannot persist coffee notification setting")
        await callback.answer("Не удалось сохранить настройку", show_alert=True)
        return
    if changed:
        coffee_alert_scheduler.reschedule_active_alerts()
    if callback.message:
        await callback.message.edit_text(
            _coffee_alert_settings_text(app_state, coffee_timing_policy, alert, settings),
            reply_markup=_coffee_alert_settings_markup(app_state, alert),
        )
    await callback.answer("Включено" if enabled else "Выключено")


@router.callback_query(F.data.in_({"coffee:toggle_warmed_up_alert", "coffee:toggle_long_running_alert"}))
async def toggle_legacy_coffee_alert(
    callback: CallbackQuery,
    settings: Settings,
    app_state: AppStateStore,
    coffee_alert_scheduler: CoffeeAlertScheduler,
    coffee_timing_policy: CoffeeTimingPolicyService,
) -> None:
    legacy_alert = "warmed_up" if callback.data == "coffee:toggle_warmed_up_alert" else "long_running"
    enabled = not _coffee_alert_enabled(app_state, legacy_alert)
    try:
        changed = await _set_coffee_alert_enabled(
            app_state,
            legacy_alert,
            enabled,
        )
    except AppStatePersistenceError:
        LOGGER.warning("Cannot persist coffee notification setting")
        await callback.answer("Не удалось сохранить настройку", show_alert=True)
        return
    if changed:
        coffee_alert_scheduler.reschedule_active_alerts()
    if callback.message:
        await callback.message.edit_text(
            _coffee_alert_settings_text(app_state, coffee_timing_policy, legacy_alert, settings),
            reply_markup=_coffee_alert_settings_markup(app_state, legacy_alert),
        )
    await callback.answer("Включено" if enabled else "Выключено")


@router.callback_query(F.data.startswith("coffee_alert_channel_toggle:"))
async def toggle_coffee_alert_channel(
    callback: CallbackQuery,
    settings: Settings,
    app_state: AppStateStore,
    coffee_alert_scheduler: CoffeeAlertScheduler,
    coffee_timing_policy: CoffeeTimingPolicyService,
) -> None:
    if not settings.is_admin_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    parts = (callback.data or "").split(":")
    if len(parts) != 3:
        await callback.answer("Неизвестная настройка", show_alert=True)
        return
    _, alert, channel = parts
    if alert not in COFFEE_ALERTS or channel not in {"telegram", "iphone"}:
        await callback.answer("Неизвестная настройка", show_alert=True)
        return

    enabled = not _coffee_alert_channel_enabled(app_state, alert, channel)
    try:
        changed = await _set_coffee_alert_channel_enabled(
            app_state,
            alert,
            channel,
            enabled,
        )
    except AppStatePersistenceError:
        LOGGER.warning("Cannot persist coffee notification setting")
        await callback.answer("Не удалось сохранить настройку", show_alert=True)
        return
    if changed:
        coffee_alert_scheduler.reschedule_active_alerts()
    if callback.message:
        await callback.message.edit_text(
            _coffee_alert_settings_text(app_state, coffee_timing_policy, alert, settings),
            reply_markup=_coffee_alert_settings_markup(app_state, alert),
        )
    await callback.answer("Включено" if enabled else "Выключено")


@router.callback_query(F.data.startswith("coffee_alert_time:"))
async def request_coffee_alert_time(
    callback: CallbackQuery,
    settings: Settings,
) -> None:
    if not settings.is_admin_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    alert = (callback.data or "").split(":", 1)[1]
    if alert not in COFFEE_ALERTS:
        await callback.answer("Неизвестная настройка", show_alert=True)
        return
    _coffee_alert_time_drafts[callback.from_user.id] = CoffeeAlertTimeDraft(alert=alert)
    if callback.message:
        await callback.message.edit_text(_coffee_alert_time_prompt(alert))
    await callback.answer()


@router.callback_query(F.data.startswith("coffee_alert_time_change:"))
async def change_coffee_alert_time(
    callback: CallbackQuery,
    settings: Settings,
) -> None:
    if not settings.is_admin_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    alert = (callback.data or "").split(":", 1)[1]
    if alert not in COFFEE_ALERTS:
        await callback.answer("Неизвестная настройка", show_alert=True)
        return
    _coffee_alert_time_drafts[callback.from_user.id] = CoffeeAlertTimeDraft(alert=alert)
    if callback.message:
        await callback.message.edit_text(_coffee_alert_time_prompt(alert))
    await callback.answer()


@router.callback_query(F.data.startswith("coffee_alert_time_confirm:"))
async def confirm_coffee_alert_time(
    callback: CallbackQuery,
    settings: Settings,
    app_state: AppStateStore,
    coffee_alert_scheduler: CoffeeAlertScheduler,
    coffee_timing_policy: CoffeeTimingPolicyService,
) -> None:
    if not settings.is_admin_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    alert = (callback.data or "").split(":", 1)[1]
    draft = _coffee_alert_time_drafts.get(callback.from_user.id)
    if alert not in COFFEE_ALERTS or draft is None or draft.alert != alert or draft.delay_seconds is None:
        await callback.answer("Сначала введи время", show_alert=True)
        return
    try:
        await _set_coffee_alert_delay_seconds(
            coffee_timing_policy,
            alert,
            draft.delay_seconds,
        )
    except (HomeAssistantError, TimingPolicyError):
        LOGGER.exception("Cannot update canonical coffee timing helper in Home Assistant")
        await callback.answer(
            "Home Assistant недоступен — значение не изменено",
            show_alert=True,
        )
        return
    coffee_alert_scheduler.reschedule_active_alerts()
    _coffee_alert_time_drafts.pop(callback.from_user.id, None)
    if callback.message:
        await callback.message.edit_text(
            _coffee_alert_settings_text(app_state, coffee_timing_policy, alert, settings),
            reply_markup=_coffee_alert_settings_markup(app_state, alert),
        )
    await callback.answer("Сохранено")


@router.callback_query(F.data.in_({"coffee:turn_on", "coffee:turn_off"}))
async def toggle_coffee(
    callback: CallbackQuery,
    settings: Settings,
    ha: HomeAssistantClient,
    telegram_messages: TelegramMessages,
    coffee_alert_scheduler: CoffeeAlertScheduler,
) -> None:
    if not settings.is_admin_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    turn_on = callback.data == "coffee:turn_on"
    callback_text = "\u0413\u043e\u0442\u043e\u0432\u043e"
    try:
        if turn_on:
            result = await turn_on_coffee_machine(ha, settings, source="telegram_callback:coffee:turn_on")
            if result.already_on:
                callback_text = "\u041a\u043e\u0444\u0435\u043c\u0430\u0448\u0438\u043d\u0430 \u0443\u0436\u0435 \u0440\u0430\u0431\u043e\u0442\u0430\u0435\u0442"
            else:
                await coffee_alert_scheduler.handle_state("on")
        else:
            await turn_off_coffee_machine(ha, settings, source="telegram_callback:coffee:turn_off")
            await coffee_alert_scheduler.handle_state("off")
    except HomeAssistantError:
        LOGGER.exception("Cannot toggle coffee machine")
        await callback.answer("Не получилось связаться с Home Assistant", show_alert=True)
        return

    if callback.message:
        text, is_on = await _coffee_status_text(settings, ha)
        message_id = await telegram_messages.safe_edit(
            callback.message.chat.id,
            callback.message.message_id,
            text,
            coffee_status(is_on),
        )
        if turn_on:
            asyncio.create_task(
                _refresh_later(
                    callback.message.chat.id,
                    message_id or callback.message.message_id,
                    settings,
                    ha,
                    telegram_messages,
                )
            )
    await callback.answer(callback_text)


@router.message(Command("coffee_on"))
async def coffee_on_command(
    message: Message,
    settings: Settings,
    ha: HomeAssistantClient,
    coffee_alert_scheduler: CoffeeAlertScheduler,
    telegram_messages: TelegramMessages,
) -> None:
    if not settings.is_admin_user(message.from_user.id if message.from_user else None):
        await message.answer("Нет доступа")
        return

    try:
        result = await turn_on_coffee_machine(ha, settings, source="telegram_command:/coffee_on")
        if result.already_on:
            runtime_text = result.runtime_text or "00:00"
            sent = await message.answer(
                "\u2615 \u041a\u043e\u0444\u0435\u043c\u0430\u0448\u0438\u043d\u0430 \u0443\u0436\u0435 "
                f"\u0432\u043a\u043b\u044e\u0447\u0435\u043d\u0430. \u0412\u0440\u0435\u043c\u044f \u0440\u0430\u0431\u043e\u0442\u044b: {runtime_text}"
            )
            asyncio.create_task(_delete_later(telegram_messages, sent.chat.id, sent.message_id, 4))
            return
        await coffee_alert_scheduler.handle_state("on")
    except HomeAssistantError:
        LOGGER.exception("Cannot turn on coffee machine from Telegram command")
        await message.answer("Не получилось включить кофемашину.")
        return
    await message.answer("☕ Кофемашина включена.")


@router.message(Command("coffee_off"))
async def coffee_off_command(message: Message, settings: Settings, ha: HomeAssistantClient, coffee_alert_scheduler: CoffeeAlertScheduler) -> None:
    if not settings.is_admin_user(message.from_user.id if message.from_user else None):
        await message.answer("Нет доступа")
        return

    try:
        await turn_off_coffee_machine(ha, settings, source="telegram_command:/coffee_off")
        await coffee_alert_scheduler.handle_state("off")
    except HomeAssistantError:
        LOGGER.exception("Cannot turn off coffee machine from Telegram command")
        await message.answer("Не получилось выключить кофемашину.")
        return
    await message.answer("☕ Кофемашина выключена.")


@router.message(F.text)
async def coffee_alert_time_text(message: Message, settings: Settings) -> None:
    user_id = message.from_user.id if message.from_user else None
    if user_id is None or not settings.is_admin_user(user_id):
        raise SkipHandler()
    draft = _coffee_alert_time_drafts.get(user_id)
    if draft is None:
        raise SkipHandler()

    delay_seconds = parse_coffee_alert_delay_seconds(message.text or "")
    if delay_seconds is None:
        await message.answer("Не понял время. Напиши, например: 15 мин, 15 минут, 1 ч или 1 час.")
        return
    draft.delay_seconds = delay_seconds
    await message.answer(
        f"Новое время: {_format_delay(delay_seconds)}\n\nСохранить?",
        reply_markup=coffee_alert_time_confirm(alert=draft.alert),
    )


@router.callback_query(F.data == "coffee_alert:turn_off")
async def turn_off_from_coffee_alert(
    callback: CallbackQuery,
    settings: Settings,
    ha: HomeAssistantClient,
    telegram_messages: TelegramMessages,
    coffee_alert_scheduler: CoffeeAlertScheduler,
) -> None:
    if not settings.is_admin_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    try:
        await turn_off_coffee_machine(ha, settings, source="telegram_callback:coffee_alert:turn_off")
        await coffee_alert_scheduler.handle_state("off")
    except HomeAssistantError:
        LOGGER.exception("Cannot turn off coffee machine from alert")
        await callback.answer("Не получилось выключить кофемашину", show_alert=True)
        return

    if callback.message:
        await telegram_messages.safe_edit(
            callback.message.chat.id,
            callback.message.message_id,
            coffee_messages.coffee_alert_turned_off(),
            delete_only(),
        )
    await callback.answer("Выключила")


@router.callback_query(F.data == "sonya:menu")
async def ask_sonya_menu(callback: CallbackQuery, settings: Settings) -> None:
    if not settings.is_admin_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    if callback.message:
        await callback.message.edit_text(coffee_messages.coffee_questioning(), reply_markup=sonya_menu())
    await callback.answer()


@router.callback_query(F.data == "sonya:ask_coffee")
async def ask_sonya_coffee(
    callback: CallbackQuery,
    settings: Settings,
    coffee_workflow: CoffeeWorkflow,
) -> None:
    if not settings.is_admin_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    if callback.message:
        try:
            await coffee_workflow.start_telegram_question(callback.message.chat.id, callback.message.message_id)
        except HomeAssistantError:
            LOGGER.exception("Cannot ask Sonya through Home Assistant")
            await callback.answer("Не получилось спросить Соню", show_alert=True)
            return
    await callback.answer("Я спрошу Соню")


@router.callback_query(F.data.in_({"coffee_confirm:yes", "coffee_confirm:no", "coffee_confirm:later"}))
async def coffee_confirmation(
    callback: CallbackQuery,
    settings: Settings,
    coffee_workflow: CoffeeWorkflow,
) -> None:
    if not settings.is_admin_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    if callback.data == "coffee_confirm:later":
        if callback.message:
            await callback.message.edit_text(
                "Когда напомнить включить кофемашину?",
                reply_markup=later_options(settings.telegram_enable_test_1_min_reminder),
            )
        await callback.answer("Я напомню позже")
        return

    if callback.data == "coffee_confirm:yes":
        try:
            if callback.message:
                await coffee_workflow.confirm_turn_on(callback.message.chat.id, callback.message.message_id)
        except HomeAssistantError:
            LOGGER.exception("Cannot confirm coffee machine start")
            await callback.answer("Не получилось включить кофемашину", show_alert=True)
            return
        await callback.answer("Готово")
        return

    try:
        if callback.message:
            await coffee_workflow.confirm_decline(callback.message.chat.id, callback.message.message_id)
    except HomeAssistantError:
        LOGGER.exception("Cannot speak negative confirmation")
    await callback.answer("Готово")


@router.callback_query(F.data.startswith("coffee_later:"))
async def coffee_later(
    callback: CallbackQuery,
    settings: Settings,
    coffee_workflow: CoffeeWorkflow,
) -> None:
    if not settings.is_admin_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    minutes = int((callback.data or "").split(":", 1)[1])
    context = await coffee_workflow.schedule_reminder(callback.message.chat.id, minutes, callback.message.message_id)  # type: ignore[union-attr]
    if callback.message:
        await callback.message.edit_text(
            coffee_messages.coffee_scheduled(
                context.temperature or context.coffee_type,
                context.syrup or "не указано",
                context.comment,
                minutes,
                minute_word(minutes),
            ),
            reply_markup=delete_only(),
        )
    await callback.answer("Я напомню позже")


@router.callback_query(F.data == "sonya_order:coffee")
async def sonya_order_coffee(callback: CallbackQuery, settings: Settings) -> None:
    if not settings.is_sonya_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    if callback.message:
        await callback.message.edit_text(coffee_messages.sonya_coffee_temperature_question(), reply_markup=sonya_temperature_menu())
    await callback.answer()


@router.callback_query(F.data.startswith("sonya_order_temp:"))
async def sonya_order_temperature(
    callback: CallbackQuery,
    settings: Settings,
    coffee_workflow: CoffeeWorkflow,
) -> None:
    if not settings.is_sonya_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    temperature = (callback.data or "").split(":", 1)[1]
    await coffee_workflow.set_sonya_order_temperature(callback.from_user.id, temperature)
    if callback.message:
        await callback.message.edit_text(coffee_messages.sonya_coffee_syrup_question(), reply_markup=sonya_syrup_menu())
    await callback.answer()


@router.callback_query(F.data.startswith("sonya_order_syrup:"))
async def sonya_order_syrup(
    callback: CallbackQuery,
    settings: Settings,
    coffee_workflow: CoffeeWorkflow,
) -> None:
    if not settings.is_sonya_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    syrup = (callback.data or "").split(":", 1)[1]
    draft = await coffee_workflow.set_sonya_order_syrup(callback.from_user.id, syrup)
    if callback.message:
        await callback.message.edit_text(
            coffee_messages.sonya_coffee_confirm(draft.order_text()),
            reply_markup=sonya_order_confirm_menu(),
        )
    await callback.answer()


@router.callback_query(F.data == "sonya_order:confirm")
async def sonya_order_confirm(
    callback: CallbackQuery,
    settings: Settings,
    coffee_workflow: CoffeeWorkflow,
) -> None:
    if not settings.is_sonya_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    await coffee_workflow.submit_sonya_order(callback.from_user.id)
    if callback.message:
        await callback.message.edit_text(coffee_messages.sonya_coffee_sent(), reply_markup=sonya_order_menu())
    await callback.answer("Готово, я передала сообщение")


@router.callback_query(F.data == "sonya_order:cancel")
async def sonya_order_cancel(callback: CallbackQuery, settings: Settings) -> None:
    if not settings.is_sonya_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    if callback.message:
        await callback.message.edit_text(coffee_messages.sonya_order_cancelled(), reply_markup=sonya_order_menu())
    await callback.answer()


async def _coffee_status_text(settings: Settings, ha: HomeAssistantClient) -> tuple[str, bool]:
    switch_state = await ha.get_state(settings.coffee_switch_entity)
    state = (switch_state or {}).get("state", "unavailable")
    is_on = state == "on"
    sensors: list[tuple[str, str]] = []

    labels = {
        "power": "Мощность",
        "current": "Ток",
        "voltage": "Напряжение",
    }
    for key, label in labels.items():
        entity_id = settings.coffee_sensors.get(key, "")
        value = "нет данных"
        if entity_id:
            sensor_state = await ha.get_state(entity_id)
            if sensor_state and sensor_state.get("state") not in {None, "unknown", "unavailable", ""}:
                unit = sensor_state.get("attributes", {}).get("unit_of_measurement", "")
                value = f"{sensor_state['state']} {unit}".strip()
        sensors.append((label, value))

    return coffee_messages.coffee_status_text(
        is_on=is_on,
        uptime=_coffee_uptime_minutes_text(switch_state, is_on),
        sensors=sensors,
    ), is_on


async def _refresh_later(
    chat_id: int,
    message_id: int,
    settings: Settings,
    ha: HomeAssistantClient,
    telegram_messages: TelegramMessages,
) -> None:
    for delay_seconds in (3, 7):
        await asyncio.sleep(delay_seconds)
        try:
            text, is_on = await _coffee_status_text(settings, ha)
            await telegram_messages.safe_edit(chat_id, message_id, text, coffee_status(is_on))
        except Exception:
            LOGGER.exception("Cannot refresh coffee status after delay=%s", delay_seconds)


async def _delete_later(telegram_messages: TelegramMessages, chat_id: int, message_id: int, delay_seconds: int) -> None:
    await asyncio.sleep(delay_seconds)
    await telegram_messages.safe_delete(chat_id, message_id)


def _coffee_uptime_minutes_text(switch_state: dict | None, is_on: bool) -> str:
    if not is_on or not switch_state:
        return "—"

    last_changed = switch_state.get("last_changed")
    changed_at = _parse_ha_datetime(last_changed)
    if changed_at is None:
        return "—"

    minutes = max(0, int((datetime.now(timezone.utc) - changed_at).total_seconds() // 60))
    return f"{minutes} мин"


def _parse_ha_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        LOGGER.warning("Cannot parse Home Assistant datetime: %s", value)
        return None


def _coffee_settings_text(
    app_state: AppStateStore,
    timing_policy: CoffeeTimingPolicyService,
) -> str:
    warmed_up_state = "включено" if app_state.coffee_warmed_up_alert_enabled else "выключено"
    long_running_state = "включено" if app_state.coffee_long_running_alert_enabled else "выключено"
    warmup = timing_policy.warmup_duration_seconds
    long_running = timing_policy.long_running_threshold_seconds
    timing_state = (
        f"{_format_delay(warmup)} / {_format_delay(long_running)}"
        if warmup is not None and long_running is not None
        else "временно недоступны"
    )
    return (
        "⚙️ <b>Настройки кофемашины</b>\n\n"
        f"Разогрев: <b>{warmed_up_state}</b>\n"
        f"Долгая работа: <b>{long_running_state}</b>\n"
        f"Пороговые значения HA: <b>{timing_state}</b>\n\n"
        "Время хранится в Home Assistant; Telegram изменяет канонические helpers."
    )


def _coffee_pushward_settings_text(app_state: AppStateStore, settings: Settings) -> str:
    live_activity_state = "включено" if settings.pushward_coffee_activity_enabled else "выключено"
    time_state = "минуты и секунды" if app_state.coffee_pushward_show_seconds else "только минуты"
    return (
        "⚙️ <b>Настройки PushWard Live Activity</b>\n\n"
        f"Live Activity: <b>{live_activity_state}</b>\n"
        f"Время: <b>{time_state}</b>\n\n"
        "Эта настройка влияет только на текст времени в Live Activity кофемашины."
    )


def _coffee_alert_settings_text(
    app_state: AppStateStore,
    timing_policy: CoffeeTimingPolicyService,
    alert: str,
    settings: Settings,
) -> str:
    title = "Уведомление о разогреве" if alert == "warmed_up" else "Уведомление о долгой работе"
    state = "включено" if _coffee_alert_enabled(app_state, alert) else "выключено"
    delay_seconds = _coffee_alert_delay_seconds(timing_policy, alert)
    delay = _format_delay(delay_seconds) if delay_seconds is not None else "недоступно в Home Assistant"
    telegram_state = "включено" if _coffee_alert_channel_enabled(app_state, alert, "telegram") else "выключено"
    iphone_state = "включено" if _coffee_alert_channel_enabled(app_state, alert, "iphone") else "выключено"
    if _coffee_alert_channel_enabled(app_state, alert, "iphone") and not settings.ha_mobile_notify_services:
        iphone_state = "включено, HA service не задан"
    channels_warning = "\nКаналы: <b>отключены</b>" if not _coffee_alert_channel_enabled(app_state, alert, "telegram") and not _coffee_alert_channel_enabled(app_state, alert, "iphone") else ""
    return (
        f"⚙️ <b>{title}</b>\n\n"
        f"Состояние: <b>{state}</b>\n"
        f"Время: <b>{delay}</b>\n"
        f"Каналы:\nTelegram: <b>{telegram_state}</b>\niPhone: <b>{iphone_state}</b>"
        f"{channels_warning}"
    )


def _coffee_alert_time_prompt(alert: str) -> str:
    title = "разогрева" if alert == "warmed_up" else "долгой работы"
    return (
        f"Настройка времени уведомления {title}.\n\n"
        "Через сколько отправлять уведомление?\n"
        "Например: 15 мин, 15 минут, 1 ч, 1 час."
    )


def _coffee_alert_enabled(app_state: AppStateStore, alert: str) -> bool:
    return app_state.coffee_warmed_up_alert_enabled if alert == "warmed_up" else app_state.coffee_long_running_alert_enabled


def _coffee_alert_settings_markup(app_state: AppStateStore, alert: str):
    return coffee_alert_settings(
        alert=alert,
        enabled=_coffee_alert_enabled(app_state, alert),
        telegram_enabled=_coffee_alert_channel_enabled(app_state, alert, "telegram"),
        iphone_enabled=_coffee_alert_channel_enabled(app_state, alert, "iphone"),
    )


def _coffee_alert_channel_enabled(app_state: AppStateStore, alert: str, channel: str) -> bool:
    if alert == "warmed_up" and channel == "telegram":
        return app_state.coffee_warmed_up_notify_telegram
    if alert == "warmed_up" and channel == "iphone":
        return app_state.coffee_warmed_up_notify_iphone
    if channel == "telegram":
        return app_state.coffee_long_running_notify_telegram
    return app_state.coffee_long_running_notify_iphone


async def _set_coffee_alert_channel_enabled(
    app_state: AppStateStore,
    alert: str,
    channel: str,
    enabled: bool,
) -> bool:
    if alert == "warmed_up" and channel == "telegram":
        return await app_state.set_coffee_warmed_up_notify_telegram(enabled)
    if alert == "warmed_up" and channel == "iphone":
        return await app_state.set_coffee_warmed_up_notify_iphone(enabled)
    if channel == "telegram":
        return await app_state.set_coffee_long_running_notify_telegram(enabled)
    return await app_state.set_coffee_long_running_notify_iphone(enabled)


async def _set_coffee_alert_enabled(
    app_state: AppStateStore,
    alert: str,
    enabled: bool,
) -> bool:
    if alert == "warmed_up":
        return await app_state.set_coffee_warmed_up_alert_enabled(enabled)
    return await app_state.set_coffee_long_running_alert_enabled(enabled)


def _coffee_alert_delay_seconds(
    timing_policy: CoffeeTimingPolicyService,
    alert: str,
) -> int | None:
    return (
        timing_policy.warmup_duration_seconds
        if alert == "warmed_up"
        else timing_policy.long_running_threshold_seconds
    )


async def _set_coffee_alert_delay_seconds(
    timing_policy: CoffeeTimingPolicyService,
    alert: str,
    delay_seconds: int,
) -> None:
    if alert == "warmed_up":
        await timing_policy.set_warmup_duration_seconds(delay_seconds)
        return
    await timing_policy.set_long_running_threshold_seconds(delay_seconds)


def parse_coffee_alert_delay_seconds(text: str) -> int | None:
    match = COFFEE_ALERT_TIME_RE.match(text)
    if match is None:
        return None
    value = int(match.group("value"))
    unit = match.group("unit").lower()
    if value <= 0:
        return None
    return value * 3600 if unit.startswith("ч") else value * 60


def _format_delay(delay_seconds: int) -> str:
    minutes = max(1, delay_seconds // 60)
    if minutes < 60:
        return f"{minutes} {minute_word(minutes)}"
    if minutes % 60 == 0:
        hours = minutes // 60
        return f"{hours} {_hour_word(hours)}"
    hours = minutes // 60
    rest_minutes = minutes % 60
    return f"{hours} {_hour_word(hours)} {rest_minutes} {minute_word(rest_minutes)}"


def _hour_word(hours: int) -> str:
    if 11 <= hours % 100 <= 14:
        return "часов"
    if hours % 10 == 1:
        return "час"
    if hours % 10 in {2, 3, 4}:
        return "часа"
    return "часов"
