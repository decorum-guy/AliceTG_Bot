import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery

from app.config import Settings
from app.keyboards.coffee import delete_only
from app.keyboards.main import sonya_order_menu
from app.keyboards.water import water_later_options
from app.messages import water as water_messages
from app.messages.common import sonya_menu_text
from app.services.home_assistant import HomeAssistantError
from app.workflows.coffee import minute_word
from app.workflows.water import WaterWorkflow

router = Router()
LOGGER = logging.getLogger(__name__)


@router.callback_query(F.data == "sonya:ask_water")
async def ask_sonya_water(callback: CallbackQuery, settings: Settings, water_workflow: WaterWorkflow) -> None:
    if not settings.is_admin_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    if callback.message:
        try:
            await water_workflow.start_telegram_question(callback.message.chat.id, callback.message.message_id)
        except HomeAssistantError:
            LOGGER.exception("Cannot ask Sonya about water through Home Assistant")
            await callback.answer("Не получилось спросить Соню", show_alert=True)
            return
    await callback.answer("Я спрошу Соню")


@router.callback_query(F.data == "sonya_order:water")
async def sonya_order_water(callback: CallbackQuery, settings: Settings, water_workflow: WaterWorkflow) -> None:
    if not settings.is_sonya_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    await water_workflow.submit_sonya_order(callback.from_user.id)
    if callback.message:
        await callback.message.edit_text(water_messages.sonya_water_sent(), reply_markup=delete_only())
    await callback.answer("Передала заказ Артему", show_alert=True)
    if callback.message:
        await callback.message.answer(sonya_menu_text(), reply_markup=sonya_order_menu())


@router.callback_query(F.data.in_({"water:now", "water:later"}))
async def water_confirmation(callback: CallbackQuery, settings: Settings, water_workflow: WaterWorkflow) -> None:
    if not settings.is_admin_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    if callback.data == "water:later":
        if callback.message:
            await callback.message.edit_text("Когда напомнить принести воду?", reply_markup=water_later_options())
        await callback.answer("Выбери задержку")
        return

    try:
        if callback.message:
            await water_workflow.confirm_now(callback.message.chat.id, callback.message.message_id)
    except HomeAssistantError:
        LOGGER.exception("Cannot announce water confirmation")
        await callback.answer("Не получилось озвучить Соне", show_alert=True)
        return
    await callback.answer("Готово")


@router.callback_query(F.data.startswith("water_later:"))
async def water_later(callback: CallbackQuery, settings: Settings, water_workflow: WaterWorkflow) -> None:
    if not settings.is_admin_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    minutes = int((callback.data or "").split(":", 1)[1])
    if minutes not in range(1, 6):
        await callback.answer("Неверная задержка", show_alert=True)
        return
    if callback.message:
        context = await water_workflow.schedule_reminder(callback.message.chat.id, minutes, callback.message.message_id)
        await callback.message.edit_text(
            water_messages.water_scheduled(minutes, minute_word(minutes), context.comment),
            reply_markup=delete_only(),
        )
    await callback.answer("Я напомню позже")
