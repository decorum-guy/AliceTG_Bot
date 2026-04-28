from aiogram.types import InlineKeyboardMarkup

from app.keyboards.styles import BUTTON_DANGER, BUTTON_PRIMARY, BUTTON_SUCCESS, inline_button


def coffee_status(is_on: bool) -> InlineKeyboardMarkup:
    action = "coffee:turn_off" if is_on else "coffee:turn_on"
    label = "⏹ Выключить" if is_on else "▶️ Включить"
    action_style = BUTTON_DANGER if is_on else BUTTON_SUCCESS
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [inline_button(text=label, callback_data=action, style=action_style)],
            [inline_button(text="🔄 Обновить", callback_data="coffee:status", style=BUTTON_PRIMARY)],
            [inline_button(text="⬅️ Назад", callback_data="menu:main", style=BUTTON_PRIMARY)],
        ]
    )


def sonya_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [inline_button(text="Хочет ли кофе?", callback_data="sonya:ask_coffee", style=BUTTON_PRIMARY)],
            [inline_button(text="⬅️ Назад", callback_data="menu:main", style=BUTTON_PRIMARY)],
        ]
    )


def confirmation() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                inline_button(text="✅ Да", callback_data="coffee_confirm:yes", style=BUTTON_SUCCESS),
                inline_button(text="❌ Нет", callback_data="coffee_confirm:no", style=BUTTON_DANGER),
                inline_button(text="⏰ Попозже", callback_data="coffee_confirm:later", style=BUTTON_PRIMARY),
            ],
            [inline_button(text="🗑 Удалить уведомление", callback_data="message:delete", style=BUTTON_DANGER)],
        ]
    )


def later_options(enable_test_1_min: bool = False) -> InlineKeyboardMarkup:
    rows = []
    if enable_test_1_min:
        rows.append([inline_button(text="+1 минута", callback_data="coffee_later:1", style=BUTTON_PRIMARY)])
    rows.extend(
        [
            [
                inline_button(text="+10 минут", callback_data="coffee_later:10", style=BUTTON_PRIMARY),
                inline_button(text="+15 минут", callback_data="coffee_later:15", style=BUTTON_PRIMARY),
            ],
            [
                inline_button(text="+30 минут", callback_data="coffee_later:30", style=BUTTON_PRIMARY),
                inline_button(text="+45 минут", callback_data="coffee_later:45", style=BUTTON_PRIMARY),
            ],
            [inline_button(text="+1 час", callback_data="coffee_later:60", style=BUTTON_PRIMARY)],
            [inline_button(text="🗑 Удалить уведомление", callback_data="message:delete", style=BUTTON_DANGER)],
        ]
    )
    return InlineKeyboardMarkup(
        inline_keyboard=rows
    )


def delete_only() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [inline_button(text="🗑 Удалить уведомление", callback_data="message:delete", style=BUTTON_DANGER)]
        ]
    )
