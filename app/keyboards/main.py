from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Кофемашина", callback_data="coffee:status")],
            [InlineKeyboardButton(text="Спросить Соню", callback_data="sonya:menu")],
        ]
    )
