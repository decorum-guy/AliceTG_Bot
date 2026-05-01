from aiogram.types import InlineKeyboardMarkup

from app.keyboards.styles import BUTTON_DANGER, BUTTON_PRIMARY, BUTTON_SUCCESS, inline_button


def water_confirmation() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                inline_button(text="✅ Сейчас", callback_data="water:now", style=BUTTON_SUCCESS),
                inline_button(text="⏰ Попозже", callback_data="water:later", style=BUTTON_PRIMARY),
            ],
        ]
    )


def water_later_options() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                inline_button(text=f"{minutes} мин", callback_data=f"water_later:{minutes}", style=BUTTON_PRIMARY)
                for minutes in range(1, 6)
            ],
        ]
    )


def sonya_water_comment_options() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [inline_button(text="Без комментария", callback_data="sonya_water_comment:none", style=BUTTON_SUCCESS)],
            [inline_button(text="❌ Отменить", callback_data="sonya_water_comment:cancel", style=BUTTON_DANGER)],
        ]
    )
