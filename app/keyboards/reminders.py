from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup

from app.keyboards.styles import BUTTON_DANGER, BUTTON_PRIMARY, BUTTON_SUCCESS, inline_button
from app.services.reminder_store import ReminderRecord, ReminderSettings


def reminders_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [inline_button(text="Управление напоминаниями", callback_data="reminders:list")],
            [inline_button(text="Создать напоминание", callback_data="reminders:create", style=BUTTON_SUCCESS)],
            [inline_button(text="⚙️ Настройки", callback_data="reminders:settings", style=BUTTON_PRIMARY)],
            [inline_button(text="⬅️ Назад", callback_data="menu:main", style=BUTTON_PRIMARY)],
        ]
    )


def reminders_back_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [inline_button(text="⬅️ Назад", callback_data="reminders:menu", style=BUTTON_PRIMARY)],
        ]
    )


def reminders_list_menu(reminders: list[ReminderRecord]) -> InlineKeyboardMarkup:
    rows = [
        [inline_button(text=f"Удалить {index}", callback_data=f"reminders:cancel:{reminder.id}", style=BUTTON_DANGER)]
        for index, reminder in enumerate(reminders, start=1)
    ]
    rows.append([inline_button(text="⬅️ Назад", callback_data="reminders:menu", style=BUTTON_PRIMARY)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def reminders_settings_menu(settings: ReminderSettings) -> InlineKeyboardMarkup:
    telegram_text = "Telegram: вкл" if settings.notify_telegram_enabled else "Telegram: выкл"
    iphone_text = "iPhone: вкл" if settings.notify_iphone_enabled else "iPhone: выкл"
    toggle_text = "Выключить озвучивание" if settings.voice_enabled else "Включить озвучивание"
    station_text = "Колонка: Зал" if settings.voice_station_entity_id == "media_player.stantsiia_mini_zal" else "Колонка: Спальня"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [inline_button(text=toggle_text, callback_data="reminders:voice:toggle")],
            [inline_button(text=station_text, callback_data="reminders:voice_station:toggle")],
            [inline_button(text=telegram_text, callback_data="reminders:notify:telegram")],
            [inline_button(text=iphone_text, callback_data="reminders:notify:iphone")],
            [inline_button(text="⬅️ Назад", callback_data="reminders:menu", style=BUTTON_PRIMARY)],
        ]
    )
