import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery

from app.config import Settings
from app.keyboards.coffee import delete_only
from app.keyboards.tea import (
    KEEP_WARM_TEMPERATURES,
    keep_warm_menu,
    kettle_status,
    sonya_tea_confirm_menu,
    sonya_tea_keep_warm_menu,
    sonya_tea_temperature_menu,
    tea_later_options,
)
from app.messages import tea as tea_messages
from app.messages.common import reminder_scheduled_text
from app.messages.devices import device_action_prefix, kettle_status_text
from app.services.home_assistant import HomeAssistantClient, HomeAssistantError
from app.services.telegram_messages import TelegramMessages
from app.workflows.coffee import minute_word
from app.workflows.tea import TeaWorkflow
from app.workflows.tea import context_from_draft
from app.workflows.tea import tea_keep_warm_label

router = Router()
LOGGER = logging.getLogger(__name__)


@router.callback_query(F.data == "sonya:ask_tea")
async def ask_sonya_tea(callback: CallbackQuery, settings: Settings, tea_workflow: TeaWorkflow) -> None:
    if not settings.is_admin_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    if callback.message:
        try:
            await tea_workflow.start_telegram_question(callback.message.chat.id, callback.message.message_id)
        except HomeAssistantError:
            LOGGER.exception("Cannot ask Sonya about tea through Home Assistant")
            await callback.answer("Не получилось спросить Соню", show_alert=True)
            return
    await callback.answer("Я спрошу Соню")


@router.callback_query(F.data == "kettle:status")
async def show_kettle_status(callback: CallbackQuery, settings: Settings, ha: HomeAssistantClient, telegram_messages: TelegramMessages) -> None:
    if not settings.is_admin_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    text, state = await _kettle_status_text(settings, ha)
    if callback.message:
        await telegram_messages.safe_edit(callback.message.chat.id, callback.message.message_id, text, kettle_status(**state))
    await callback.answer()


@router.callback_query(F.data == "kettle:boil")
async def kettle_boil(callback: CallbackQuery, settings: Settings, tea_workflow: TeaWorkflow, ha: HomeAssistantClient, telegram_messages: TelegramMessages) -> None:
    if not settings.is_admin_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    await tea_workflow.boil()
    if callback.message:
        text, state = await _kettle_status_text(settings, ha, prefix=device_action_prefix("Включила чайник. Грею до кипения."))
        await telegram_messages.safe_edit(callback.message.chat.id, callback.message.message_id, text, kettle_status(**state))
    await callback.answer("Включила")


@router.callback_query(F.data == "kettle:stop")
async def kettle_stop(callback: CallbackQuery, settings: Settings, tea_workflow: TeaWorkflow, ha: HomeAssistantClient, telegram_messages: TelegramMessages) -> None:
    if not settings.is_admin_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    await tea_workflow.stop_kettle()
    if callback.message:
        text, state = await _kettle_status_text(settings, ha, prefix=device_action_prefix("Остановила чайник."))
        await telegram_messages.safe_edit(callback.message.chat.id, callback.message.message_id, text, kettle_status(**state))
    await callback.answer("Остановила")


@router.callback_query(F.data == "kettle:keep_warm")
async def kettle_keep_warm(callback: CallbackQuery, settings: Settings) -> None:
    if not settings.is_admin_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    if callback.message:
        await callback.message.edit_text("🫖 <b>Поддержание тепла</b>\n\nВыбери температуру.", reply_markup=keep_warm_menu())
    await callback.answer()


@router.callback_query(F.data.startswith("kettle:keep_warm:"))
async def kettle_keep_warm_temperature(callback: CallbackQuery, settings: Settings, tea_workflow: TeaWorkflow, ha: HomeAssistantClient, telegram_messages: TelegramMessages) -> None:
    if not settings.is_admin_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    temperature = int((callback.data or "").rsplit(":", 1)[1])
    if temperature not in KEEP_WARM_TEMPERATURES:
        await callback.answer("Неверная температура", show_alert=True)
        return
    await tea_workflow.enable_keep_warm(temperature)
    if callback.message:
        text, state = await _kettle_status_text(settings, ha, prefix=device_action_prefix(f"Включила поддержание тепла на {temperature} °C."))
        await telegram_messages.safe_edit(callback.message.chat.id, callback.message.message_id, text, kettle_status(**state))
    await callback.answer("Включила")


@router.callback_query(F.data == "kettle:keep_warm_off")
async def kettle_keep_warm_off(callback: CallbackQuery, settings: Settings, tea_workflow: TeaWorkflow, ha: HomeAssistantClient, telegram_messages: TelegramMessages) -> None:
    if not settings.is_admin_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    await tea_workflow.disable_keep_warm()
    if callback.message:
        text, state = await _kettle_status_text(settings, ha, prefix=device_action_prefix("Выключила поддержание тепла."))
        await telegram_messages.safe_edit(callback.message.chat.id, callback.message.message_id, text, kettle_status(**state))
    await callback.answer("Выключила")


@router.callback_query(F.data.in_({"kettle:toggle_light", "kettle:toggle_mute"}))
async def kettle_toggle_switch(callback: CallbackQuery, settings: Settings, ha: HomeAssistantClient, telegram_messages: TelegramMessages) -> None:
    if not settings.is_admin_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    is_light = callback.data == "kettle:toggle_light"
    entity_id = settings.kettle_light_switch_entity if is_light else settings.kettle_mute_switch_entity
    state = await ha.get_state(entity_id)
    turn_on = (state or {}).get("state") != "on"
    if turn_on:
        await ha.switch_turn_on(entity_id)
    else:
        await ha.switch_turn_off(entity_id)
    if is_light:
        prefix = device_action_prefix("Включила подсветку." if turn_on else "Выключила подсветку.")
    else:
        prefix = device_action_prefix("Включила режим без звука." if turn_on else "Выключила режим без звука.")
    if callback.message:
        text, keyboard_state = await _kettle_status_text(settings, ha, prefix=prefix)
        await telegram_messages.safe_edit(callback.message.chat.id, callback.message.message_id, text, kettle_status(**keyboard_state))
    await callback.answer("Готово")


@router.callback_query(F.data.in_({"tea_confirm:yes", "tea_confirm:no", "tea_confirm:later"}))
async def tea_confirmation(callback: CallbackQuery, settings: Settings, tea_workflow: TeaWorkflow) -> None:
    if not settings.is_admin_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    if callback.data == "tea_confirm:later":
        if callback.message:
            await callback.message.edit_text(
                "Когда напомнить включить чайник?",
                reply_markup=tea_later_options(settings.telegram_enable_test_1_min_reminder),
            )
        await callback.answer("Я напомню позже")
        return

    try:
        if callback.message and callback.data == "tea_confirm:yes":
            await tea_workflow.confirm_turn_on(callback.message.chat.id, callback.message.message_id)
        elif callback.message:
            await tea_workflow.confirm_decline(callback.message.chat.id, callback.message.message_id)
    except HomeAssistantError:
        LOGGER.exception("Cannot process tea confirmation")
        await callback.answer("Не получилось связаться с Home Assistant", show_alert=True)
        return
    await callback.answer("Готово")


@router.callback_query(F.data.startswith("tea_later:"))
async def tea_later(callback: CallbackQuery, settings: Settings, tea_workflow: TeaWorkflow) -> None:
    if not settings.is_admin_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    minutes = int((callback.data or "").split(":", 1)[1])
    await tea_workflow.schedule_reminder(callback.message.chat.id, minutes, callback.message.message_id)  # type: ignore[union-attr]
    if callback.message:
        await callback.message.edit_text(reminder_scheduled_text(minutes, minute_word(minutes)), reply_markup=delete_only())
    await callback.answer("Я напомню позже")


@router.callback_query(F.data == "sonya_order:tea")
async def sonya_order_tea(callback: CallbackQuery, settings: Settings) -> None:
    if not settings.is_sonya_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    if callback.message:
        await callback.message.edit_text(tea_messages.sonya_tea_keep_warm_question(), reply_markup=sonya_tea_keep_warm_menu())
    await callback.answer()


@router.callback_query(F.data.startswith("sonya_tea_keep_warm:"))
async def sonya_tea_keep_warm(callback: CallbackQuery, settings: Settings, tea_workflow: TeaWorkflow) -> None:
    if not settings.is_sonya_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    keep_warm = (callback.data or "").split(":", 1)[1] == "yes"
    draft = await tea_workflow.set_sonya_order_keep_warm(callback.from_user.id, keep_warm)
    if callback.message:
        if keep_warm:
            await callback.message.edit_text(tea_messages.sonya_tea_temperature_question(), reply_markup=sonya_tea_temperature_menu())
        else:
            await callback.message.edit_text(_sonya_tea_confirmation_text(draft), reply_markup=sonya_tea_confirm_menu())
    await callback.answer()


@router.callback_query(F.data.startswith("sonya_tea_temp:"))
async def sonya_tea_temperature(callback: CallbackQuery, settings: Settings, tea_workflow: TeaWorkflow) -> None:
    if not settings.is_sonya_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    temperature = int((callback.data or "").split(":", 1)[1])
    if temperature not in KEEP_WARM_TEMPERATURES:
        await callback.answer("Неверная температура", show_alert=True)
        return
    draft = await tea_workflow.set_sonya_order_temperature(callback.from_user.id, temperature)
    if callback.message:
        await callback.message.edit_text(_sonya_tea_confirmation_text(draft), reply_markup=sonya_tea_confirm_menu())
    await callback.answer()


@router.callback_query(F.data == "sonya_tea:confirm")
async def sonya_tea_confirm(callback: CallbackQuery, settings: Settings, tea_workflow: TeaWorkflow) -> None:
    if not settings.is_sonya_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    await tea_workflow.submit_sonya_order(callback.from_user.id)
    if callback.message:
        await callback.message.edit_text(tea_messages.sonya_tea_sent(), reply_markup=None)
    await callback.answer("Готово, я передала сообщение")


@router.callback_query(F.data == "sonya_tea:cancel")
async def sonya_tea_cancel(callback: CallbackQuery, settings: Settings) -> None:
    if not settings.is_sonya_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    if callback.message:
        await callback.message.edit_text(tea_messages.sonya_tea_cancelled(), reply_markup=None)
    await callback.answer()


async def _kettle_status_text(settings: Settings, ha: HomeAssistantClient, prefix: str = "") -> tuple[str, dict[str, bool]]:
    kettle = await ha.get_state(settings.kettle_entity)
    keep_warm = await ha.get_state(settings.kettle_keep_warm_switch_entity)
    light = await ha.get_state(settings.kettle_light_switch_entity)
    mute = await ha.get_state(settings.kettle_mute_switch_entity)

    attrs = (kettle or {}).get("attributes", {})
    heat_on = (kettle or {}).get("state") == "on" or attrs.get("operation_mode") not in {None, "off"}
    keep_warm_on = (keep_warm or {}).get("state") == "on"
    light_on = (light or {}).get("state") == "on"
    mute_on = (mute or {}).get("state") == "on"
    text = kettle_status_text(
        current_temperature=_format_temperature(attrs.get("current_temperature")),
        target_temperature=_format_temperature(attrs.get("temperature")),
        heat_on=heat_on,
        keep_warm_on=keep_warm_on,
        light_on=light_on,
        mute_on=mute_on,
        prefix=prefix,
    )
    return text, {"heat_on": heat_on, "keep_warm_on": keep_warm_on, "light_on": light_on, "mute_on": mute_on}


def _format_temperature(value: object) -> str:
    if value in {None, "unknown", "unavailable"}:
        return "н/д"
    return f"{value} °C"


def _sonya_tea_confirmation_text(draft) -> str:
    context = context_from_draft(draft, source="sonya_telegram_order", speak_to_bedroom_on_confirm=False, speak_to_bedroom_on_decline=False)
    return tea_messages.sonya_tea_confirm(tea_keep_warm_label(context))
