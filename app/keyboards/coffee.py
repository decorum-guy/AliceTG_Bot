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
            [inline_button(text="⚙️ Настройки", callback_data="coffee:settings", style=BUTTON_PRIMARY)],
            [inline_button(text="⬅️ Назад", callback_data="devices:menu", style=BUTTON_PRIMARY)],
        ]
    )


def coffee_settings() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [inline_button(text="Разогрев", callback_data="coffee_alert_settings:warmed_up")],
            [inline_button(text="Долгая работа", callback_data="coffee_alert_settings:long_running")],
            [inline_button(text="PushWard Live Activity", callback_data="coffee:pushward_settings")],
            [inline_button(text="⬅️ Назад", callback_data="coffee:status", style=BUTTON_PRIMARY)],
        ]
    )


def coffee_pushward_settings(*, show_seconds: bool) -> InlineKeyboardMarkup:
    time_text = "Время: минуты и секунды" if show_seconds else "Время: только минуты"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [inline_button(text=time_text, callback_data="coffee:pushward_time_format:toggle")],
            [inline_button(text="⬅️ Назад", callback_data="coffee:settings", style=BUTTON_PRIMARY)],
        ]
    )


def coffee_alert_settings(*, alert: str, enabled: bool, telegram_enabled: bool, iphone_enabled: bool) -> InlineKeyboardMarkup:
    telegram_text = "Telegram: вкл" if telegram_enabled else "Telegram: выкл"
    iphone_text = "iPhone: вкл" if iphone_enabled else "iPhone: выкл"
    toggle_text = "Выключить" if enabled else "Включить"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [inline_button(text=toggle_text, callback_data=f"coffee_alert_toggle:{alert}")],
            [inline_button(text="Настроить время", callback_data=f"coffee_alert_time:{alert}")],
            [inline_button(text=telegram_text, callback_data=f"coffee_alert_channel_toggle:{alert}:telegram")],
            [inline_button(text=iphone_text, callback_data=f"coffee_alert_channel_toggle:{alert}:iphone")],
            [inline_button(text="⬅️ Назад", callback_data="coffee:settings", style=BUTTON_PRIMARY)],
        ]
    )


def coffee_alert_time_confirm(*, alert: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [inline_button(text="✅ Подтвердить", callback_data=f"coffee_alert_time_confirm:{alert}", style=BUTTON_SUCCESS)],
            [inline_button(text="Изменить", callback_data=f"coffee_alert_time_change:{alert}")],
        ]
    )


def coffee_turn_off_only() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [inline_button(text="⏹ Выключить", callback_data="coffee_alert:turn_off", style=BUTTON_DANGER)],
            [inline_button(text="🗑 Удалить уведомление", callback_data="message:delete", style=BUTTON_DANGER)],
        ]
    )


def sonya_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [inline_button(text="Хочет ли кофе?", callback_data="sonya:ask_coffee")],
            [inline_button(text="Хочет ли чай?", callback_data="sonya:ask_tea")],
            [inline_button(text="Хочет ли воды?", callback_data="sonya:ask_water")],
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
