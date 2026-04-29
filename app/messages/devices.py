from __future__ import annotations

from app.messages.base import MessageKind, MessageStyle, h, render_message


def kettle_status_text(
    *,
    current_temperature: str,
    target_temperature: str,
    heat_on: bool,
    keep_warm_on: bool,
    light_on: bool,
    mute_on: bool,
    prefix: str = "",
) -> str:
    status = render_message(
        MessageStyle(MessageKind.DEVICE_STATUS, "tea", "Чайник"),
        details=[
            ("Текущая температура", current_temperature, False),
            ("Целевая температура", target_temperature, False),
            ("Нагрев", "включен" if heat_on else "выключен", False),
            ("Поддержание тепла", "включено" if keep_warm_on else "выключено", False),
            ("Подсветка", "включена" if light_on else "выключена", False),
            ("Без звука", "включено" if mute_on else "выключено", False),
        ],
    )
    return f"{prefix}\n\n{status}" if prefix else status


def device_action_prefix(message: str) -> str:
    return render_message(MessageStyle(MessageKind.SUCCESS, "success", "Готово"), body=[h(message)])

