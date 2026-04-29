from __future__ import annotations

from app.messages.base import MessageKind, MessageStyle, h, render_message


def admin_menu_text() -> str:
    return render_message(
        MessageStyle(MessageKind.MENU, "home", "Алиса дома"),
        body=["Я рядом. Что делаем дальше?"],
    )


def smart_devices_menu_text() -> str:
    return render_message(
        MessageStyle(MessageKind.MENU, "device", "Умные устройства"),
        body=["Выбери устройство."],
    )


def sonya_menu_text() -> str:
    return render_message(
        MessageStyle(MessageKind.SONYA, "home", "Алиса рядом"),
        body=[
            "Привет, Соня 💛",
            "Я Алиса, твой умный домашний помощник.",
            "Я могу передать Артёму заказ и помочь быстро попросить кофе или чай.",
            "",
            "Что заказать?",
        ],
    )


def access_denied_text() -> str:
    return render_message(
        MessageStyle(MessageKind.ERROR, "error", "Нет доступа"),
        body=["Я могу выполнять эту команду только для разрешённых пользователей."],
    )


def error_text(message: str = "Не получилось связаться с Home Assistant. Попробуй ещё раз.") -> str:
    return render_message(MessageStyle(MessageKind.ERROR, "error", "Ошибка"), body=[h(message)])


def success_text(message: str) -> str:
    return render_message(MessageStyle(MessageKind.SUCCESS, "success", "Готово"), body=[h(message)])


def reminder_scheduled_text(minutes: int, minute_label: str) -> str:
    return render_message(
        MessageStyle(MessageKind.REMINDER, "reminder", "Напоминание"),
        body=[f"Я напомню через {h(minutes)} {h(minute_label)}."],
    )
