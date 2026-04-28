from aiogram import F, Router
from aiogram.types import CallbackQuery

from app.config import Settings
from app.services.telegram_messages import TelegramMessages

router = Router()


@router.callback_query(F.data == "message:delete")
async def delete_message(
    callback: CallbackQuery,
    settings: Settings,
    telegram_messages: TelegramMessages,
) -> None:
    if not settings.is_allowed_user(callback.from_user.id):
        await callback.answer("Доступ запрещён", show_alert=True)
        return

    if callback.message:
        await telegram_messages.safe_delete(callback.message.chat.id, callback.message.message_id)
    await callback.answer()
