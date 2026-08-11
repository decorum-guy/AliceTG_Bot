from __future__ import annotations

from typing import Sequence

from aiogram.types import InlineKeyboardMarkup

from app.keyboards.styles import BUTTON_DANGER, BUTTON_PRIMARY, BUTTON_SUCCESS, inline_button
from app.planning.telegram_ui import PlanningButton


def planning_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [inline_button(text="🔔 Напоминания", callback_data="planning:reminders:0")],
            [inline_button(text="✅ Задачи", callback_data="planning:tasks:today:0")],
            [inline_button(text="📅 Календарь", callback_data="planning:events:today:0")],
            [inline_button(text="⬅️ Назад", callback_data="menu:main", style=BUTTON_PRIMARY)],
        ]
    )


def planning_view_keyboard(rows: Sequence[Sequence[PlanningButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                inline_button(text=button.text, callback_data=button.callback_data, style=button.style)
                for button in row
            ]
            for row in rows
        ]
    )


def style_for_action(action: str) -> str | None:
    if action in {"complete", "task_complete"}:
        return BUTTON_SUCCESS
    if action == "cancel":
        return BUTTON_DANGER
    if action == "retry":
        return BUTTON_PRIMARY
    return None
