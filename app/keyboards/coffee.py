from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def coffee_status(is_on: bool) -> InlineKeyboardMarkup:
    action = "coffee:turn_off" if is_on else "coffee:turn_on"
    label = "Выключить" if is_on else "Включить"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=action)],
            [InlineKeyboardButton(text="Обновить", callback_data="coffee:status")],
            [InlineKeyboardButton(text="Назад", callback_data="menu:main")],
        ]
    )


def sonya_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Хочет ли кофе?", callback_data="sonya:ask_coffee")],
            [InlineKeyboardButton(text="Назад", callback_data="menu:main")],
        ]
    )


def confirmation() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Да", callback_data="coffee_confirm:yes"),
                InlineKeyboardButton(text="Нет", callback_data="coffee_confirm:no"),
                InlineKeyboardButton(text="Попозже", callback_data="coffee_confirm:later"),
            ],
            [InlineKeyboardButton(text="Удалить уведомление", callback_data="message:delete")],
        ]
    )


def later_options() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="+10 минут", callback_data="coffee_later:10"),
                InlineKeyboardButton(text="+15 минут", callback_data="coffee_later:15"),
            ],
            [
                InlineKeyboardButton(text="+30 минут", callback_data="coffee_later:30"),
                InlineKeyboardButton(text="+45 минут", callback_data="coffee_later:45"),
            ],
            [InlineKeyboardButton(text="+1 час", callback_data="coffee_later:60")],
            [InlineKeyboardButton(text="Удалить уведомление", callback_data="message:delete")],
        ]
    )


def delete_only() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Удалить уведомление", callback_data="message:delete")]
        ]
    )
