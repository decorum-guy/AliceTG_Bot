from aiogram.types import InlineKeyboardMarkup

from app.keyboards.styles import BUTTON_PRIMARY, BUTTON_SUCCESS, inline_button


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
