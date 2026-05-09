from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup

from app.keyboards.styles import BUTTON_DANGER, BUTTON_PRIMARY, BUTTON_SUCCESS, inline_button
from app.services.reminder_store import ReminderRecord, ReminderSettings


def reminders_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [inline_button(text="Управление напоминаниями", callback_data="reminders:list", style=BUTTON_PRIMARY)],
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
    toggle_text = "Выключить озвучивание" if settings.voice_enabled else "Включить озвучивание"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [inline_button(text=toggle_text, callback_data="reminders:voice:toggle", style=BUTTON_PRIMARY)],
            [
                inline_button(text="Колонка: Зал", callback_data="reminders:voice_station:zal", style=BUTTON_PRIMARY),
                inline_button(text="Колонка: Спальня", callback_data="reminders:voice_station:spalnia", style=BUTTON_PRIMARY),
            ],
            [inline_button(text="⬅️ Назад", callback_data="reminders:menu", style=BUTTON_PRIMARY)],
        ]
    )
