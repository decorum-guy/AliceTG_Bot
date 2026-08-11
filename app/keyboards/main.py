from aiogram.types import InlineKeyboardMarkup

from app.keyboards.styles import BUTTON_DANGER, BUTTON_PRIMARY, BUTTON_SUCCESS, inline_button


def main_menu(*, planning_enabled: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [inline_button(text="🏠 Умные устройства", callback_data="devices:menu")],
        [inline_button(text="🗣 Спросить Соню", callback_data="sonya:menu")],
        [inline_button(text="⏰ Напоминания", callback_data="reminders:menu")],
    ]
    if planning_enabled:
        rows.append([inline_button(text="📋 Дела", callback_data="planning:menu")])
    rows.extend(
        [
            [
                inline_button(text="🔊 Озвучить", callback_data="admin_mode:start:announce"),
                inline_button(text="💬 Разговор", callback_data="admin_mode:start:talk"),
            ],
            [inline_button(text="⚙️ Настройки", callback_data="admin_settings:menu")],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_settings_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [inline_button(text="♻️ Сбросить флаги и режимы", callback_data="admin_settings:reset:confirm", style=BUTTON_DANGER)],
            [inline_button(text="⬅️ Назад", callback_data="menu:main", style=BUTTON_PRIMARY)],
        ]
    )


def admin_reset_confirm_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [inline_button(text="✅ Да, сбросить", callback_data="admin_settings:reset:yes", style=BUTTON_DANGER)],
            [inline_button(text="❌ Отмена", callback_data="admin_settings:menu", style=BUTTON_PRIMARY)],
        ]
    )


def smart_devices_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [inline_button(text="☕ Кофемашина", callback_data="coffee:status")],
            [inline_button(text="🍵 Чайник", callback_data="kettle:status")],
            [inline_button(text="⬅️ Назад", callback_data="menu:main", style=BUTTON_PRIMARY)],
        ]
    )


def sonya_order_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [inline_button(text="☕️ Кофе", callback_data="sonya_order:coffee")],
            [inline_button(text="🍵 Чай", callback_data="sonya_order:tea")],
            [inline_button(text="💧 Вода", callback_data="sonya_order:water")],
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
