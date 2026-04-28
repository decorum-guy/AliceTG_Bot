from aiogram.types import InlineKeyboardMarkup

from app.keyboards.styles import BUTTON_DANGER, BUTTON_PRIMARY, BUTTON_SUCCESS, inline_button

KEEP_WARM_TEMPERATURES = (40, 50, 60, 70, 80, 90)


def kettle_status(*, heat_on: bool, keep_warm_on: bool, light_on: bool, mute_on: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [inline_button(text="▶️ Вскипятить", callback_data="kettle:boil", style=BUTTON_SUCCESS)],
            [inline_button(text="⏹ Остановить", callback_data="kettle:stop", style=BUTTON_DANGER)],
            [inline_button(text="🌡 Поддержание тепла", callback_data="kettle:keep_warm", style=BUTTON_PRIMARY)],
            [
                inline_button(
                    text=f"💡 Подсветка: {'Выкл' if light_on else 'Вкл'}",
                    callback_data="kettle:toggle_light",
                    style=BUTTON_PRIMARY,
                )
            ],
            [
                inline_button(
                    text=f"🔇 Без звука: {'Выкл' if mute_on else 'Вкл'}",
                    callback_data="kettle:toggle_mute",
                    style=BUTTON_PRIMARY,
                )
            ],
            [inline_button(text="🔄 Обновить", callback_data="kettle:status", style=BUTTON_PRIMARY)],
            [inline_button(text="⬅️ Назад", callback_data="devices:menu", style=BUTTON_PRIMARY)],
        ]
    )


def keep_warm_menu() -> InlineKeyboardMarkup:
    rows = [
        [
            inline_button(text=f"{left} °C", callback_data=f"kettle:keep_warm:{left}", style=BUTTON_PRIMARY),
            inline_button(text=f"{right} °C", callback_data=f"kettle:keep_warm:{right}", style=BUTTON_PRIMARY),
        ]
        for left, right in ((40, 50), (60, 70), (80, 90))
    ]
    rows.append([inline_button(text="⏹ Выключить поддержание", callback_data="kettle:keep_warm_off", style=BUTTON_DANGER)])
    rows.append([inline_button(text="⬅️ Назад", callback_data="kettle:status", style=BUTTON_PRIMARY)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def tea_confirmation() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                inline_button(text="✅ Да", callback_data="tea_confirm:yes", style=BUTTON_SUCCESS),
                inline_button(text="❌ Нет", callback_data="tea_confirm:no", style=BUTTON_DANGER),
                inline_button(text="⏰ Попозже", callback_data="tea_confirm:later", style=BUTTON_PRIMARY),
            ],
        ]
    )


def tea_later_options(enable_test_1_min: bool = False) -> InlineKeyboardMarkup:
    rows = []
    if enable_test_1_min:
        rows.append([inline_button(text="+1 минута", callback_data="tea_later:1", style=BUTTON_PRIMARY)])
    rows.extend(
        [
            [
                inline_button(text="+10 минут", callback_data="tea_later:10", style=BUTTON_PRIMARY),
                inline_button(text="+15 минут", callback_data="tea_later:15", style=BUTTON_PRIMARY),
            ],
            [
                inline_button(text="+30 минут", callback_data="tea_later:30", style=BUTTON_PRIMARY),
                inline_button(text="+45 минут", callback_data="tea_later:45", style=BUTTON_PRIMARY),
            ],
            [inline_button(text="+1 час", callback_data="tea_later:60", style=BUTTON_PRIMARY)],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def sonya_tea_keep_warm_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                inline_button(text="✅ Да", callback_data="sonya_tea_keep_warm:yes", style=BUTTON_SUCCESS),
                inline_button(text="❌ Нет", callback_data="sonya_tea_keep_warm:no", style=BUTTON_DANGER),
            ],
            [inline_button(text="⬅️ Назад", callback_data="menu:sonya", style=BUTTON_PRIMARY)],
            [inline_button(text="❌ Отменить", callback_data="sonya_tea:cancel", style=BUTTON_DANGER)],
        ]
    )


def sonya_tea_temperature_menu() -> InlineKeyboardMarkup:
    rows = [
        [
            inline_button(text=f"{left} °C", callback_data=f"sonya_tea_temp:{left}", style=BUTTON_PRIMARY),
            inline_button(text=f"{right} °C", callback_data=f"sonya_tea_temp:{right}", style=BUTTON_PRIMARY),
        ]
        for left, right in ((40, 50), (60, 70), (80, 90))
    ]
    rows.append([inline_button(text="⬅️ Назад", callback_data="sonya_order:tea", style=BUTTON_PRIMARY)])
    rows.append([inline_button(text="❌ Отменить", callback_data="sonya_tea:cancel", style=BUTTON_DANGER)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def sonya_tea_confirm_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [inline_button(text="✅ Подтвердить", callback_data="sonya_tea:confirm", style=BUTTON_SUCCESS)],
            [inline_button(text="⬅️ Назад", callback_data="sonya_order:tea", style=BUTTON_PRIMARY)],
            [inline_button(text="❌ Отменить", callback_data="sonya_tea:cancel", style=BUTTON_DANGER)],
        ]
    )
