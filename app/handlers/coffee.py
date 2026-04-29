from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.types import CallbackQuery

from app.config import Settings
from app.keyboards.coffee import coffee_settings, coffee_status, coffee_turn_off_only, delete_only, later_options, sonya_menu
from app.keyboards.main import sonya_order_confirm_menu, sonya_order_menu, sonya_syrup_menu, sonya_temperature_menu
from app.messages import coffee as coffee_messages
from app.messages.common import reminder_scheduled_text
from app.services.app_state import AppStateStore
from app.services.home_assistant import HomeAssistantClient, HomeAssistantError
from app.services.telegram_messages import TelegramMessages
from app.workflows.coffee import CoffeeWorkflow
from app.workflows.coffee import minute_word

router = Router()
LOGGER = logging.getLogger(__name__)


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
) -> None:
    if not settings.is_admin_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    if callback.message:
        await callback.message.edit_text(
            _coffee_settings_text(app_state),
            reply_markup=coffee_settings(
                warmed_up_enabled=app_state.coffee_warmed_up_alert_enabled,
                long_running_enabled=app_state.coffee_long_running_alert_enabled,
            ),
        )
    await callback.answer()


@router.callback_query(F.data == "coffee:toggle_warmed_up_alert")
async def toggle_coffee_warmed_up_alert(
    callback: CallbackQuery,
    settings: Settings,
    app_state: AppStateStore,
) -> None:
    if not settings.is_admin_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    enabled = not app_state.coffee_warmed_up_alert_enabled
    await app_state.set_coffee_warmed_up_alert_enabled(enabled)
    if callback.message:
        await callback.message.edit_text(
            _coffee_settings_text(app_state),
            reply_markup=coffee_settings(
                warmed_up_enabled=app_state.coffee_warmed_up_alert_enabled,
                long_running_enabled=app_state.coffee_long_running_alert_enabled,
            ),
        )
    await callback.answer("Готовность включена" if enabled else "Готовность выключена")


@router.callback_query(F.data == "coffee:toggle_long_running_alert")
async def toggle_coffee_long_running_alert(
    callback: CallbackQuery,
    settings: Settings,
    app_state: AppStateStore,
) -> None:
    if not settings.is_admin_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    enabled = not app_state.coffee_long_running_alert_enabled
    await app_state.set_coffee_long_running_alert_enabled(enabled)
    if callback.message:
        await callback.message.edit_text(
            _coffee_settings_text(app_state),
            reply_markup=coffee_settings(
                warmed_up_enabled=app_state.coffee_warmed_up_alert_enabled,
                long_running_enabled=app_state.coffee_long_running_alert_enabled,
            ),
        )
    await callback.answer("Перегрев включен" if enabled else "Перегрев выключен")


@router.callback_query(F.data.in_({"coffee:turn_on", "coffee:turn_off"}))
async def toggle_coffee(
    callback: CallbackQuery,
    settings: Settings,
    ha: HomeAssistantClient,
    telegram_messages: TelegramMessages,
) -> None:
    if not settings.is_admin_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    turn_on = callback.data == "coffee:turn_on"
    try:
        if turn_on:
            await ha.switch_turn_on(settings.coffee_switch_entity)
        else:
            await ha.switch_turn_off(settings.coffee_switch_entity)
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
    await callback.answer("Готово")


@router.callback_query(F.data == "coffee_alert:turn_off")
async def turn_off_from_coffee_alert(
    callback: CallbackQuery,
    settings: Settings,
    ha: HomeAssistantClient,
    telegram_messages: TelegramMessages,
) -> None:
    if not settings.is_admin_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    try:
        await ha.switch_turn_off(settings.coffee_switch_entity)
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
    await coffee_workflow.schedule_reminder(callback.message.chat.id, minutes, callback.message.message_id)  # type: ignore[union-attr]
    if callback.message:
        await callback.message.edit_text(reminder_scheduled_text(minutes, minute_word(minutes)), reply_markup=delete_only())
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


def _coffee_settings_text(app_state: AppStateStore) -> str:
    warmed_up_state = "включено" if app_state.coffee_warmed_up_alert_enabled else "выключено"
    long_running_state = "включено" if app_state.coffee_long_running_alert_enabled else "выключено"
    return (
        "⚙️ <b>Настройки кофемашины</b>\n\n"
        f"Уведомление о готовности 13 мин: <b>{warmed_up_state}</b>\n"
        f"Уведомление о перегреве 1 час: <b>{long_running_state}</b>\n\n"
        "Эти настройки управляют только Telegram-уведомлениями, не самой кофемашиной."
    )
