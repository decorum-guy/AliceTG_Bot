from aiogram.types import InlineKeyboardMarkup

from app.keyboards.styles import BUTTON_DANGER, BUTTON_PRIMARY, BUTTON_SUCCESS, inline_button


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [inline_button(text="☕ Кофемашина", callback_data="coffee:status")],
            [inline_button(text="🗣 Спросить Соню", callback_data="sonya:menu")],
        ]
    )


def sonya_order_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [inline_button(text="☕️ Кофе", callback_data="sonya_order:coffee")],
            [inline_button(text="🍵 Чай", callback_data="sonya_order:tea")],
        ]
    )


def sonya_temperature_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                inline_button(text="🧊 Холодный", callback_data="sonya_order_temp:cold"),
                inline_button(text="🔥 Горячий", callback_data="sonya_order_temp:hot"),
            ],
            [inline_button(text="⬅️ Назад", callback_data="menu:sonya", style=BUTTON_PRIMARY)],
        ]
    )


def sonya_syrup_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                inline_button(text="🍯 С сиропом", callback_data="sonya_order_syrup:yes"),
                inline_button(text="Без сиропа", callback_data="sonya_order_syrup:no"),
            ],
            [inline_button(text="⬅️ Назад", callback_data="sonya_order:coffee", style=BUTTON_PRIMARY)],
        ]
    )


def sonya_order_confirm_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [inline_button(text="✅ Подтвердить", callback_data="sonya_order:confirm", style=BUTTON_SUCCESS)],
            [inline_button(text="⬅️ Назад", callback_data="sonya_order:coffee", style=BUTTON_PRIMARY)],
            [inline_button(text="❌ Отменить", callback_data="sonya_order:cancel", style=BUTTON_DANGER)],
        ]
    )
