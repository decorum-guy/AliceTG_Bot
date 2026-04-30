from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup

from app.keyboards.styles import BUTTON_PRIMARY, inline_button


def room_menu(mode: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                inline_button(text="Зал", callback_data=f"admin_mode:room:{mode}:hall", style=BUTTON_PRIMARY),
                inline_button(text="Спальня", callback_data=f"admin_mode:room:{mode}:bedroom", style=BUTTON_PRIMARY),
            ],
            [inline_button(text="⬅️ Назад", callback_data="menu:main", style=BUTTON_PRIMARY)],
        ]
    )


def volume_menu(mode: str) -> InlineKeyboardMarkup:
    values = [round(index / 10, 1) for index in range(11)]
    rows = []
    for index in range(0, len(values), 3):
        rows.append(
            [
                inline_button(text=f"{value:.1f}", callback_data=f"admin_mode:volume:{mode}:{value:.1f}", style=BUTTON_PRIMARY)
                for value in values[index : index + 3]
            ]
        )
    rows.append([inline_button(text="⬅️ Назад", callback_data="admin_mode:back_to_room", style=BUTTON_PRIMARY)])
    return InlineKeyboardMarkup(inline_keyboard=rows)
