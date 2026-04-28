from aiogram.types import InlineKeyboardMarkup

from app.keyboards.styles import BUTTON_PRIMARY, inline_button


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [inline_button(text="☕ Кофемашина", callback_data="coffee:status", style=BUTTON_PRIMARY)],
            [inline_button(text="🗣 Спросить Соню", callback_data="sonya:menu", style=BUTTON_PRIMARY)],
        ]
    )
