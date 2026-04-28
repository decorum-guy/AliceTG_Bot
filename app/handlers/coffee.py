from __future__ import annotations

import asyncio
import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery

from app.config import Settings
from app.keyboards.coffee import coffee_status, delete_only, later_options, sonya_menu
from app.keyboards.main import sonya_order_confirm_menu, sonya_order_menu, sonya_syrup_menu, sonya_temperature_menu
from app.services.home_assistant import HomeAssistantClient, HomeAssistantError
from app.services.telegram_messages import TelegramMessages
from app.workflows.coffee import CoffeeWorkflow

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
        await callback.answer("Доступ запрещён", show_alert=True)
        return

    text, is_on = await _coffee_status_text(settings, ha)
    if callback.message:
        await telegram_messages.safe_edit(callback.message.chat.id, callback.message.message_id, text, coffee_status(is_on))
    await callback.answer()


@router.callback_query(F.data.in_({"coffee:turn_on", "coffee:turn_off"}))
async def toggle_coffee(
    callback: CallbackQuery,
    settings: Settings,
    ha: HomeAssistantClient,
    telegram_messages: TelegramMessages,
) -> None:
    if not settings.is_admin_user(callback.from_user.id):
        await callback.answer("Доступ запрещён", show_alert=True)
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
        asyncio.create_task(_refresh_later(callback.message.chat.id, message_id or callback.message.message_id, settings, ha, telegram_messages))
    await callback.answer("Готово")


@router.callback_query(F.data == "sonya:menu")
async def ask_sonya_menu(callback: CallbackQuery, settings: Settings) -> None:
    if not settings.is_admin_user(callback.from_user.id):
        await callback.answer("Доступ запрещён", show_alert=True)
        return

    if callback.message:
        await callback.message.edit_text("Я уточняю у Сони.", reply_markup=sonya_menu())
    await callback.answer()


@router.callback_query(F.data == "sonya:ask_coffee")
async def ask_sonya_coffee(
    callback: CallbackQuery,
    settings: Settings,
    coffee_workflow: CoffeeWorkflow,
) -> None:
    if not settings.is_admin_user(callback.from_user.id):
        await callback.answer("Доступ запрещён", show_alert=True)
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
        await callback.answer("Доступ запрещён", show_alert=True)
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
        await callback.answer("Доступ запрещён", show_alert=True)
        return

    minutes = int((callback.data or "").split(":", 1)[1])
    await coffee_workflow.schedule_reminder(callback.message.chat.id, minutes, callback.message.message_id)  # type: ignore[union-attr]
    if callback.message:
        await callback.message.edit_text(f"Я напомню через {minutes} минут.", reply_markup=delete_only())
    await callback.answer("Я напомню позже")


@router.callback_query(F.data == "sonya_order:tea")
async def sonya_order_tea(callback: CallbackQuery, settings: Settings) -> None:
    if not settings.is_sonya_user(callback.from_user.id):
        await callback.answer("Доступ запрещён", show_alert=True)
        return

    if callback.message:
        await callback.message.edit_text("Чай я добавлю чуть позже.", reply_markup=sonya_order_menu())
    await callback.answer()


@router.callback_query(F.data == "sonya_order:coffee")
async def sonya_order_coffee(callback: CallbackQuery, settings: Settings) -> None:
    if not settings.is_sonya_user(callback.from_user.id):
        await callback.answer("Доступ запрещён", show_alert=True)
        return

    if callback.message:
        await callback.message.edit_text("Какой кофе?", reply_markup=sonya_temperature_menu())
    await callback.answer()


@router.callback_query(F.data.startswith("sonya_order_temp:"))
async def sonya_order_temperature(
    callback: CallbackQuery,
    settings: Settings,
    coffee_workflow: CoffeeWorkflow,
) -> None:
    if not settings.is_sonya_user(callback.from_user.id):
        await callback.answer("Доступ запрещён", show_alert=True)
        return

    temperature = (callback.data or "").split(":", 1)[1]
    await coffee_workflow.set_sonya_order_temperature(callback.from_user.id, temperature)
    if callback.message:
        await callback.message.edit_text("С сиропом?", reply_markup=sonya_syrup_menu())
    await callback.answer()


@router.callback_query(F.data.startswith("sonya_order_syrup:"))
async def sonya_order_syrup(
    callback: CallbackQuery,
    settings: Settings,
    coffee_workflow: CoffeeWorkflow,
) -> None:
    if not settings.is_sonya_user(callback.from_user.id):
        await callback.answer("Доступ запрещён", show_alert=True)
        return

    syrup = (callback.data or "").split(":", 1)[1]
    draft = await coffee_workflow.set_sonya_order_syrup(callback.from_user.id, syrup)
    if callback.message:
        await callback.message.edit_text(
            f"Проверяю заказ:\n{draft.order_text()}.\nВсё верно?",
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
        await callback.answer("Доступ запрещён", show_alert=True)
        return

    await coffee_workflow.submit_sonya_order(callback.from_user.id)
    if callback.message:
        await callback.message.edit_text("Передаю заказ Артёму.\nКофе скоро будет готов.", reply_markup=sonya_order_menu())
    await callback.answer("Готово, я передала сообщение")


@router.callback_query(F.data == "sonya_order:cancel")
async def sonya_order_cancel(callback: CallbackQuery, settings: Settings) -> None:
    if not settings.is_sonya_user(callback.from_user.id):
        await callback.answer("Доступ запрещён", show_alert=True)
        return

    if callback.message:
        await callback.message.edit_text("Хорошо, отменяю заказ.", reply_markup=sonya_order_menu())
    await callback.answer()


async def _coffee_status_text(settings: Settings, ha: HomeAssistantClient) -> tuple[str, bool]:
    switch_state = await ha.get_state(settings.coffee_switch_entity)
    state = (switch_state or {}).get("state", "unavailable")
    is_on = state == "on"
    lines = [f"Кофемашина: {'включена' if is_on else 'выключена' if state == 'off' else 'н/д'}"]

    labels = {
        "voltage": "Напряжение",
        "power": "Мощность",
        "current": "Ток",
    }
    for key, label in labels.items():
        entity_id = settings.coffee_sensors.get(key, "")
        value = "н/д"
        if entity_id:
            sensor_state = await ha.get_state(entity_id)
            if sensor_state and sensor_state.get("state") not in {None, "unknown", "unavailable"}:
                unit = sensor_state.get("attributes", {}).get("unit_of_measurement", "")
                value = f"{sensor_state['state']} {unit}".strip()
        lines.append(f"{label}: {value}")

    return "\n".join(lines), is_on


async def _refresh_later(
    chat_id: int,
    message_id: int,
    settings: Settings,
    ha: HomeAssistantClient,
    telegram_messages: TelegramMessages,
) -> None:
    await asyncio.sleep(5)
    try:
        text, is_on = await _coffee_status_text(settings, ha)
        await telegram_messages.safe_edit(chat_id, message_id, text, coffee_status(is_on))
    except Exception:
        LOGGER.exception("Cannot refresh coffee status after delay")
