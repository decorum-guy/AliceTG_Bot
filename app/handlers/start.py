from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, Message

from app.config import Settings
from app.keyboards.main import main_menu, smart_devices_menu, sonya_order_menu
from app.messages.common import access_denied_text, admin_menu_text, smart_devices_menu_text, sonya_menu_text

router = Router()

SONYA_MENU_TEXT = sonya_menu_text()


@router.message(CommandStart())
async def start(message: Message, settings: Settings) -> None:
    if not settings.is_allowed_user(message.from_user.id if message.from_user else None):
        await message.answer(access_denied_text())
        return

    if settings.is_sonya_user(message.from_user.id if message.from_user else None):
        await message.answer(SONYA_MENU_TEXT, reply_markup=sonya_order_menu())
        return

    await message.answer(
        admin_menu_text(),
        reply_markup=main_menu(planning_enabled=getattr(settings, "planning_telegram_ui_enabled", False)),
    )


@router.callback_query(F.data == "menu:main")
async def menu_main(callback: CallbackQuery, settings: Settings) -> None:
    if not settings.is_allowed_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    if settings.is_sonya_user(callback.from_user.id):
        if callback.message:
            await callback.message.edit_text(SONYA_MENU_TEXT, reply_markup=sonya_order_menu())
        await callback.answer()
        return

    if callback.message:
        await callback.message.edit_text(
            admin_menu_text(),
            reply_markup=main_menu(planning_enabled=getattr(settings, "planning_telegram_ui_enabled", False)),
        )
    await callback.answer()


@router.callback_query(F.data == "devices:menu")
async def devices_menu(callback: CallbackQuery, settings: Settings) -> None:
    if not settings.is_admin_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    if callback.message:
        await callback.message.edit_text(smart_devices_menu_text(), reply_markup=smart_devices_menu())
    await callback.answer()


@router.callback_query(F.data == "menu:sonya")
async def menu_sonya(callback: CallbackQuery, settings: Settings) -> None:
    if not settings.is_sonya_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    if callback.message:
        await callback.message.edit_text(SONYA_MENU_TEXT, reply_markup=sonya_order_menu())
    await callback.answer()
