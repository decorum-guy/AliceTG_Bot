from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, Message

from app.config import Settings
from app.keyboards.main import main_menu

router = Router()


@router.message(CommandStart())
async def start(message: Message, settings: Settings) -> None:
    if not settings.is_allowed_user(message.from_user.id if message.from_user else None):
        await message.answer("Доступ запрещён")
        return

    await message.answer("Я Алиса. Чем займёмся дома?", reply_markup=main_menu())


@router.callback_query(F.data == "menu:main")
async def menu_main(callback: CallbackQuery, settings: Settings) -> None:
    if not settings.is_allowed_user(callback.from_user.id):
        await callback.answer("Доступ запрещён", show_alert=True)
        return

    if callback.message:
        await callback.message.edit_text("Я Алиса. Чем займёмся дома?", reply_markup=main_menu())
    await callback.answer()
