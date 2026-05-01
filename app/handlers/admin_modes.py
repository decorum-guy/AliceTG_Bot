from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from app.config import Settings
from app.keyboards.admin_modes import room_menu, volume_menu
from app.keyboards.main import admin_reset_confirm_menu, admin_settings_menu, main_menu
from app.messages.common import admin_menu_text
from app.services.admin_modes import ADMIN_ROOM_OPTIONS, ADMIN_TALK_DIALOGS, SONYA_WAITING_FLAGS, AdminModeManager
from app.services.home_assistant import HomeAssistantClient, HomeAssistantError
from app.services.yandex_dialogs import yandex_dialog_content_type

LOGGER = logging.getLogger(__name__)
router = Router()
WHISPER_PREFIX = '<speaker is_whisper="true">'
WHISPER_PATTERN = re.compile(r"^\s*/ш[её]пот/\s*", re.IGNORECASE)
STOP_SUFFIX_PATTERN = re.compile(r"(?:^|\s)/stop\s*$", re.IGNORECASE)

MODE_TITLES = {
    "announce": "Озвучить",
    "talk": "Разговор",
}


SETTINGS_TEXT = "⚙️ Настройки"
RESET_CONFIRM_TEXT = "Сбросить все флаги ожидания и активные админ-режимы?\nУстройства выключены не будут."
RESET_DONE_TEXT = "Готово. Флаги ожидания и активные админ-режимы сброшены."


@dataclass(frozen=True)
class VoiceMessage:
    text: str
    whisper: bool = False

    @property
    def media_content_id(self) -> str:
        if not self.whisper:
            return self.text
        return f"{WHISPER_PREFIX}{html.escape(self.text, quote=True)}"


@router.callback_query(F.data == "admin_settings:menu")
async def admin_settings(callback: CallbackQuery, settings: Settings) -> None:
    if not settings.is_admin_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    if callback.message:
        await callback.message.edit_text(SETTINGS_TEXT, reply_markup=admin_settings_menu())
    await callback.answer()


@router.callback_query(F.data == "admin_settings:reset:confirm")
async def confirm_admin_reset(callback: CallbackQuery, settings: Settings) -> None:
    if not settings.is_admin_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    if callback.message:
        await callback.message.edit_text(RESET_CONFIRM_TEXT, reply_markup=admin_reset_confirm_menu())
    await callback.answer()


@router.callback_query(F.data == "admin_settings:reset:yes")
async def reset_admin_state(
    callback: CallbackQuery,
    settings: Settings,
    admin_modes: AdminModeManager,
    ha: HomeAssistantClient,
) -> None:
    if not settings.is_admin_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    await _reset_sonya_waiting_flags(ha)
    session = admin_modes.clear(callback.from_user.id)
    if session is not None:
        await _restore_volume(session, ha)
        LOGGER.info("Admin mode cleared by settings reset: user_id=%s mode=%s", callback.from_user.id, session.mode)

    if callback.message:
        await callback.message.edit_text(RESET_DONE_TEXT, reply_markup=main_menu())
    await callback.answer("Готово")


@router.callback_query(F.data.in_({"admin_mode:start:announce", "admin_mode:start:talk"}))
async def start_admin_mode(callback: CallbackQuery, settings: Settings, admin_modes: AdminModeManager, ha: HomeAssistantClient) -> None:
    if not settings.is_admin_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    previous = admin_modes.clear(callback.from_user.id)
    if previous is not None:
        await _restore_volume(previous, ha)

    mode = (callback.data or "").rsplit(":", 1)[1]
    admin_modes.start(callback.from_user.id, mode)  # type: ignore[arg-type]
    LOGGER.info("Admin mode setup started: user_id=%s mode=%s", callback.from_user.id, mode)
    if callback.message:
        await callback.message.edit_text(f"{MODE_TITLES[mode]}\n\nВыбери колонку.", reply_markup=room_menu(mode))
    await callback.answer()


@router.callback_query(F.data.startswith("admin_mode:room:"))
async def select_admin_mode_room(callback: CallbackQuery, settings: Settings, admin_modes: AdminModeManager) -> None:
    if not settings.is_admin_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    parts = (callback.data or "").split(":")
    if len(parts) != 4:
        await callback.answer("Неизвестная кнопка", show_alert=True)
        return
    _, _, mode, room = parts
    if mode not in MODE_TITLES or room not in ADMIN_ROOM_OPTIONS:
        await callback.answer("Неизвестная кнопка", show_alert=True)
        return
    session = admin_modes.get(callback.from_user.id) or admin_modes.start(callback.from_user.id, mode)  # type: ignore[arg-type]
    if session.mode != mode:
        session = admin_modes.start(callback.from_user.id, mode)  # type: ignore[arg-type]
    session = admin_modes.set_room(callback.from_user.id, room)  # type: ignore[arg-type]
    if session is None:
        await callback.answer("Режим не найден", show_alert=True)
        return

    LOGGER.info("Admin mode room selected: user_id=%s mode=%s room=%s entity_id=%s", callback.from_user.id, mode, room, session.entity_id)
    if callback.message:
        await callback.message.edit_text(
            f"{MODE_TITLES[mode]}\n\nКолонка: {session.room_label}\n\nВыбери громкость.",
            reply_markup=volume_menu(mode),
        )
    await callback.answer()


@router.callback_query(F.data == "admin_mode:back_to_room")
async def back_to_admin_mode_room(callback: CallbackQuery, settings: Settings, admin_modes: AdminModeManager) -> None:
    if not settings.is_admin_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    session = admin_modes.get(callback.from_user.id)
    if session is None:
        if callback.message:
            await callback.message.edit_text(admin_menu_text(), reply_markup=main_menu())
        await callback.answer()
        return
    if callback.message:
        await callback.message.edit_text(f"{MODE_TITLES[session.mode]}\n\nВыбери колонку.", reply_markup=room_menu(session.mode))
    await callback.answer()


@router.callback_query(F.data.startswith("admin_mode:volume:"))
async def select_admin_mode_volume(
    callback: CallbackQuery,
    settings: Settings,
    admin_modes: AdminModeManager,
    ha: HomeAssistantClient,
) -> None:
    if not settings.is_admin_user(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    parts = (callback.data or "").split(":")
    if len(parts) != 4:
        await callback.answer("Неизвестная кнопка", show_alert=True)
        return
    _, _, mode, volume_text = parts
    if mode not in MODE_TITLES:
        await callback.answer("Неизвестная кнопка", show_alert=True)
        return
    session = admin_modes.get(callback.from_user.id)
    if session is None or session.mode != mode or not session.entity_id:
        await callback.answer("Сначала выбери колонку", show_alert=True)
        return

    try:
        volume = float(volume_text)
    except ValueError:
        await callback.answer("Неверная громкость", show_alert=True)
        return

    previous_volume = await _read_volume(session.entity_id, ha)
    try:
        await ha.set_volume(session.entity_id, volume)
    except HomeAssistantError:
        LOGGER.exception("Cannot set admin mode volume: user_id=%s entity_id=%s volume=%s", callback.from_user.id, session.entity_id, volume)
        admin_modes.clear(callback.from_user.id)
        if callback.message:
            await callback.message.edit_text("Не удалось установить громкость на колонке. Режим не включен.", reply_markup=main_menu())
        await callback.answer("Ошибка Home Assistant", show_alert=True)
        return

    session = admin_modes.activate(callback.from_user.id, volume=volume, previous_volume=previous_volume)
    if session is None:
        await callback.answer("Режим не найден", show_alert=True)
        return
    LOGGER.info(
        "Admin mode activated: user_id=%s mode=%s room=%s entity_id=%s volume=%s previous_volume=%s",
        callback.from_user.id,
        session.mode,
        session.room,
        session.entity_id,
        volume,
        previous_volume,
    )
    if callback.message:
        await callback.message.edit_text(_mode_enabled_text(session.mode, session.room_label or "", volume))
    await callback.answer("Режим включен")


@router.message(Command("stop"))
async def stop_admin_mode(message: Message, settings: Settings, admin_modes: AdminModeManager, ha: HomeAssistantClient) -> None:
    if not settings.is_admin_user(message.from_user.id if message.from_user else None):
        return
    session = admin_modes.clear(message.from_user.id)  # type: ignore[union-attr]
    if session is not None:
        await _restore_volume(session, ha)
        LOGGER.info("Admin mode stopped: user_id=%s mode=%s", message.from_user.id, session.mode)
    await message.answer(admin_menu_text(), reply_markup=main_menu())


@router.message()
async def admin_mode_message(message: Message, settings: Settings, admin_modes: AdminModeManager, ha: HomeAssistantClient) -> None:
    user_id = message.from_user.id if message.from_user else None
    if not settings.is_admin_user(user_id):
        raise SkipHandler()

    session = admin_modes.get(user_id) if user_id is not None else None
    if session is None or not session.active or not session.entity_id:
        raise SkipHandler()

    if not message.text:
        await message.answer("Отправь текст или /stop.")
        return

    stop_after_send, text = split_stop_suffix(message.text)
    if stop_after_send and not text.strip():
        session = admin_modes.clear(user_id)
        if session is not None:
            await _restore_volume(session, ha)
            LOGGER.info("Admin mode stopped by suffix-only stop: user_id=%s mode=%s", user_id, session.mode)
        await message.answer(admin_menu_text(), reply_markup=main_menu())
        return

    voice_message = parse_voice_modifier(text)
    if voice_message is None:
        await message.answer("Напиши текст после /шепот/ или отправь /stop.")
        return

    if session.mode == "announce":
        try:
            await ha.play_media(session.entity_id, voice_message.media_content_id, "text")
        except HomeAssistantError:
            LOGGER.exception("Cannot announce admin text: user_id=%s entity_id=%s", user_id, session.entity_id)
            await message.answer("Не удалось озвучить текст через Home Assistant.")
            return
        LOGGER.info("Admin announce sent: user_id=%s entity_id=%s whisper=%s", user_id, session.entity_id, voice_message.whisper)
        if stop_after_send:
            await _finish_admin_mode_after_send(message, admin_modes, ha, user_id, "announce")
            return
        await message.answer("Озвучила.")
        return

    if not session.room:
        await message.answer("Режим разговора не настроен. Напиши /stop.")
        return
    dialog = ADMIN_TALK_DIALOGS[session.room]
    try:
        # Yandex Station dialog mode accepts the same speaker markup in media_content_id
        # while media_content_type keeps the dialog tag that makes the station listen.
        await ha.play_media(session.entity_id, voice_message.media_content_id, yandex_dialog_content_type(settings, dialog))
    except HomeAssistantError:
        LOGGER.exception("Cannot send admin talk text: user_id=%s entity_id=%s dialog=%s", user_id, session.entity_id, dialog)
        await message.answer("Не удалось отправить текст на колонку через Home Assistant.")
        return
    admin_modes.set_pending_dialog(user_id, dialog)
    LOGGER.info("Admin talk sent: user_id=%s entity_id=%s dialog=%s whisper=%s", user_id, session.entity_id, dialog, voice_message.whisper)
    if stop_after_send:
        await _finish_admin_mode_after_send(message, admin_modes, ha, user_id, "talk")
        return
    await message.answer("Отправила.")


def _mode_enabled_text(mode: str, room_label: str, volume: float) -> str:
    if mode == "announce":
        return (
            "🔊 Режим озвучивания включен\n\n"
            f"Колонка: {room_label}\n"
            f"Громкость: {_volume_percent(volume)}\n\n"
            "Отправь текст, и я озвучу его на колонке.\n"
            "Для разового шепота напиши /шепот/ текст.\n"
            "Чтобы выйти, напиши /stop."
        )
    return (
        "💬 Режим разговора включен\n\n"
        f"Колонка: {room_label}\n"
        f"Громкость: {_volume_percent(volume)}\n\n"
        "Пиши сообщения сюда - я буду озвучивать их на колонке и ждать ответ.\n"
        "Если Соня ответит, я пришлю ее ответ сюда.\n"
        "Для разового шепота напиши /шепот/ текст.\n"
        "Чтобы выйти, напиши /stop."
    )


async def _read_volume(entity_id: str, ha: HomeAssistantClient) -> float | None:
    try:
        state = await ha.get_state(entity_id)
    except HomeAssistantError:
        LOGGER.exception("Cannot read previous volume for admin mode: entity_id=%s", entity_id)
        return None
    volume = (state or {}).get("attributes", {}).get("volume_level")
    try:
        return float(volume)
    except (TypeError, ValueError):
        LOGGER.warning("Previous volume is unavailable for admin mode: entity_id=%s volume=%r", entity_id, volume)
        return None


async def _restore_volume(session, ha: HomeAssistantClient) -> None:
    if not session.entity_id or session.previous_volume is None:
        LOGGER.info("Admin mode volume restore skipped: entity_id=%s previous_volume=%s", session.entity_id, session.previous_volume)
        return
    try:
        await ha.set_volume(session.entity_id, session.previous_volume)
    except HomeAssistantError:
        LOGGER.exception("Cannot restore admin mode volume: entity_id=%s previous_volume=%s", session.entity_id, session.previous_volume)
        return
    LOGGER.info("Admin mode volume restored: entity_id=%s previous_volume=%s", session.entity_id, session.previous_volume)


async def _reset_sonya_waiting_flags(ha: HomeAssistantClient) -> None:
    for entity_id in SONYA_WAITING_FLAGS:
        try:
            await ha.input_boolean_turn_off(entity_id)
        except HomeAssistantError:
            LOGGER.exception("Cannot reset Sonya waiting flag: entity_id=%s", entity_id)


async def _finish_admin_mode_after_send(
    message: Message,
    admin_modes: AdminModeManager,
    ha: HomeAssistantClient,
    user_id: int,
    mode: str,
) -> None:
    session = admin_modes.clear(user_id)
    if session is not None:
        await _restore_volume(session, ha)
    LOGGER.info("Admin mode stopped after successful send: user_id=%s mode=%s", user_id, mode)
    await message.answer(admin_menu_text(), reply_markup=main_menu())


def split_stop_suffix(text: str) -> tuple[bool, str]:
    match = STOP_SUFFIX_PATTERN.search(text)
    if match is None:
        return False, text
    return True, text[: match.start()].rstrip()


def parse_voice_modifier(text: str) -> VoiceMessage | None:
    match = WHISPER_PATTERN.match(text)
    if match is None:
        return VoiceMessage(text=text)

    clean_text = text[match.end() :].strip()
    if not clean_text:
        return None
    return VoiceMessage(text=clean_text, whisper=True)


def _volume_percent(volume: float) -> str:
    return f"{round(volume * 100)}%"
