from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message
from aiogram.types import InlineKeyboardMarkup, MaybeInaccessibleMessage

from app.config import Settings
from app.keyboards.reminders import reminders_back_menu, reminders_list_menu, reminders_menu, reminders_settings_menu
from app.messages import reminders as reminder_messages
from app.services.reminder_parser import parse_reminder_request
from app.services.telegram_messages import is_message_not_modified_error
from app.workflows.reminders import ReminderWorkflow

router = Router()
LOGGER = logging.getLogger(__name__)


@router.callback_query(F.data == "reminders:menu")
async def reminders_menu_handler(callback: CallbackQuery, settings: Settings) -> None:
    if not settings.is_admin_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    if callback.message:
        await _safe_edit_reminder_message(callback.message, reminder_messages.reminder_menu_text(), reminders_menu())
    await callback.answer()


@router.callback_query(F.data == "reminders:create")
async def reminders_create(callback: CallbackQuery, settings: Settings, reminder_workflow: ReminderWorkflow) -> None:
    if not settings.is_admin_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    reminder_workflow.start_draft(callback.from_user.id)
    if callback.message:
        await _safe_edit_reminder_message(callback.message, reminder_messages.reminder_create_prompt(), reminders_back_menu())
    await callback.answer()


@router.callback_query(F.data == "reminders:list")
async def reminders_list(callback: CallbackQuery, settings: Settings, reminder_workflow: ReminderWorkflow) -> None:
    if not settings.is_admin_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    reminders = await reminder_workflow.list_pending()
    if callback.message:
        if reminders:
            await _safe_edit_reminder_message(callback.message, reminder_messages.reminders_list(reminders), reminders_list_menu(reminders))
        else:
            await _safe_edit_reminder_message(callback.message, reminder_messages.reminders_empty(), reminders_back_menu())
    await callback.answer()


@router.callback_query(F.data == "reminders:settings")
async def reminders_settings(callback: CallbackQuery, settings: Settings, reminder_workflow: ReminderWorkflow) -> None:
    if not settings.is_admin_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    reminder_settings = await reminder_workflow.get_settings()
    if callback.message:
        await _safe_edit_reminder_message(
            callback.message,
            reminder_messages.reminder_settings(reminder_settings),
            reminders_settings_menu(reminder_settings),
        )
    await callback.answer()


@router.callback_query(F.data == "reminders:voice:toggle")
async def reminders_voice_toggle(callback: CallbackQuery, settings: Settings, reminder_workflow: ReminderWorkflow) -> None:
    if not settings.is_admin_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    reminder_settings = await reminder_workflow.toggle_voice()
    if callback.message:
        await _safe_edit_reminder_message(
            callback.message,
            reminder_messages.reminder_settings(reminder_settings),
            reminders_settings_menu(reminder_settings),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("reminders:voice_station:"))
async def reminders_voice_station(callback: CallbackQuery, settings: Settings, reminder_workflow: ReminderWorkflow) -> None:
    if not settings.is_admin_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    station_key = (callback.data or "").rsplit(":", 1)[-1]
    if station_key == "toggle":
        reminder_settings = await reminder_workflow.toggle_voice_station()
    else:
        reminder_settings = await reminder_workflow.set_voice_station(station_key)
        if reminder_settings is None:
            await callback.answer("Неизвестная колонка", show_alert=True)
            return
    if callback.message:
        await _safe_edit_reminder_message(
            callback.message,
            reminder_messages.reminder_settings(reminder_settings),
            reminders_settings_menu(reminder_settings),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("reminders:cancel:"))
async def reminders_cancel(callback: CallbackQuery, settings: Settings, reminder_workflow: ReminderWorkflow) -> None:
    if not settings.is_admin_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    reminder_id = (callback.data or "").split(":", 2)[2]
    await reminder_workflow.cancel(reminder_id)
    reminders = await reminder_workflow.list_pending()
    if callback.message:
        if reminders:
            await _safe_edit_reminder_message(callback.message, reminder_messages.reminders_list(reminders), reminders_list_menu(reminders))
        else:
            await _safe_edit_reminder_message(callback.message, reminder_messages.reminder_cancelled(), reminders_back_menu())
    await callback.answer("Напоминание удалено")


@router.message(F.text)
async def reminders_text(message: Message, settings: Settings, reminder_workflow: ReminderWorkflow) -> None:
    user_id = message.from_user.id if message.from_user else None
    if user_id is None or not settings.is_admin_user(user_id):
        raise SkipHandler()
    draft = reminder_workflow.get_draft(user_id)
    if draft is None:
        raise SkipHandler()

    text = message.text or ""
    if draft.step == "text":
        parsed = parse_reminder_request(text)
        if parsed is not None:
            result = await reminder_workflow.create_from_text(text, source="telegram", chat_id=message.chat.id)
            reminder_workflow.clear_draft(user_id)
            if result is None:
                await message.answer(reminder_messages.reminder_time_parse_error(), reply_markup=reminders_back_menu())
                return
            _, parsed = result
            await message.answer(reminder_messages.reminder_created(parsed.text, parsed.human_delay_text), reply_markup=reminders_menu())
            return
        cleaned_text = text.strip()
        if not cleaned_text:
            await message.answer(reminder_messages.reminder_text_parse_error(), reply_markup=reminders_back_menu())
            return
        reminder_workflow.set_draft_waiting_delay(user_id, cleaned_text)
        await message.answer(reminder_messages.reminder_delay_prompt(cleaned_text), reply_markup=reminders_back_menu())
        return

    result = await reminder_workflow.create_from_parts(draft.text or "", text, source="telegram", chat_id=message.chat.id)
    if result is None:
        await message.answer(reminder_messages.reminder_time_parse_error(), reply_markup=reminders_back_menu())
        return
    _, human_delay_text = result
    reminder_workflow.clear_draft(user_id)
    await message.answer(reminder_messages.reminder_created(draft.text or "", human_delay_text), reply_markup=reminders_menu())


async def _safe_edit_reminder_message(
    message: MaybeInaccessibleMessage,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as exc:
        if is_message_not_modified_error(exc):
            LOGGER.info("Reminder message is not modified, edit skipped: message_id=%s", message.message_id)
            return
        LOGGER.exception("Cannot edit reminder message: message_id=%s", message.message_id)
        raise
