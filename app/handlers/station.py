"""Admin-only Telegram controls for the fixed Station target."""
from aiogram import F, Router
from aiogram.types import CallbackQuery

from app.config import Settings
from app.keyboards.main import station_menu
from app.services.home_assistant import HomeAssistantClient, HomeAssistantError
from app.services.station_actions import STATION_COMMANDS, StationActionService

router = Router()


@router.callback_query(F.data == "station:menu")
async def show_station_menu(callback: CallbackQuery, settings: Settings) -> None:
    if not settings.is_admin_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    if callback.message:
        await callback.message.edit_text("Станция Mini 2", reply_markup=station_menu())
    await callback.answer()


@router.callback_query(F.data.startswith("station:"))
async def station_action(callback: CallbackQuery, settings: Settings, ha: HomeAssistantClient) -> None:
    if not settings.is_admin_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    action = (callback.data or "").removeprefix("station:")
    if action not in STATION_COMMANDS:
        await callback.answer("Неизвестная команда", show_alert=True)
        return
    try:
        outcome = await StationActionService(ha, settings).dispatch(action)
    except HomeAssistantError:
        await callback.answer("Не удалось отправить команду", show_alert=True)
        return
    await callback.answer(
        "Команда отправлена" if outcome == "dispatched" else "Результат отправки неизвестен",
        show_alert=outcome != "dispatched",
    )
