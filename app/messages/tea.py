from __future__ import annotations

from app.messages.base import MessageKind, MessageStyle, h, render_message


TEA_STYLE = MessageStyle(MessageKind.ORDER, "tea", "Заказ чая")


def tea_progress(keep_warm: str = "уточняю") -> str:
    return render_message(
        TEA_STYLE,
        body=["Соня хочет чай."],
        details=[("Поддержание тепла", keep_warm, keep_warm == "уточняю")],
    )


def tea_confirmation(keep_warm: str, comment: str) -> str:
    return render_message(
        TEA_STYLE,
        body=["Соня попросила чай."],
        details=[("Поддержание тепла", keep_warm, False), ("Комментарий", comment, False)],
        footer="Включить чайник?",
    )


def tea_auto_enabled(keep_warm: str, comment: str) -> str:
    return render_message(
        TEA_STYLE,
        body=["Соня заказала чай."],
        details=[("Поддержание тепла", keep_warm, False), ("Комментарий", comment, False)],
        footer="Чайник уже включен.",
    )


def tea_reminder(keep_warm: str, comment: str) -> str:
    return render_message(
        MessageStyle(MessageKind.REMINDER, "reminder", "Напоминание"),
        body=["Соня просила чай."],
        details=[("Поддержание тепла", keep_warm, False), ("Комментарий", comment, False)],
        footer="Включить чайник?",
    )


def tea_scheduled(keep_warm: str, comment: str, minutes: int, minute_label: str) -> str:
    return render_message(
        MessageStyle(MessageKind.REMINDER, "reminder", "Отложено"),
        body=["Соня просила чай."],
        details=[("Поддержание тепла", keep_warm, False), ("Комментарий", comment, False)],
        footer=f"Напомню через {h(minutes)} {h(minute_label)}.",
    )


def tea_started(keep_warm_temperature: int | None, comment: str) -> str:
    if keep_warm_temperature is None:
        body = ["Я включила чайник.", "Грею до кипения."]
        keep_warm = "нет"
    else:
        body = ["Я включила чайник.", f"После закипания включу поддержание тепла на {h(keep_warm_temperature)} °C."]
        keep_warm = f"{keep_warm_temperature} °C"
    body.extend(["Соня просила чай."])
    return render_message(
        MessageStyle(MessageKind.SUCCESS, "success", "Готово"),
        body=body,
        details=[("Поддержание тепла", keep_warm, False), ("Комментарий", comment, False)],
    )


def tea_declined(comment: str) -> str:
    return render_message(
        MessageStyle(MessageKind.NOTIFICATION, "notification", "Заказ чая"),
        body=["Я не включаю чайник.", "Соня просила чай."],
        details=[("Комментарий", comment, False)],
    )


def tea_refused() -> str:
    return render_message(TEA_STYLE, body=["Соня отказалась от чая.", "Не включаю чайник."])


def tea_hall_refused() -> str:
    return render_message(TEA_STYLE, body=["Соня отказалась от чая.", "Чайник не включен."])


def tea_questioning() -> str:
    return render_message(MessageStyle(MessageKind.QUESTION, "question", "Спрашиваю Соню"), body=["Уточняю, хочет ли Соня чай."])


def kettle_action_done(message: str) -> str:
    return render_message(MessageStyle(MessageKind.SUCCESS, "success", "Готово"), body=[h(message)])


def sonya_tea_keep_warm_question() -> str:
    return render_message(MessageStyle(MessageKind.SONYA, "tea", "Чай"), body=["Нужно поддержание тепла?"])


def sonya_tea_temperature_question() -> str:
    return render_message(MessageStyle(MessageKind.SONYA, "tea", "Чай"), body=["На какой температуре поддерживать?"])


def sonya_tea_confirm(keep_warm: str) -> str:
    return render_message(
        MessageStyle(MessageKind.SONYA, "order", "Проверяю заказ"),
        body=[f"Чай, поддержание тепла: {h(keep_warm)}.", "Всё верно?"],
    )


def sonya_tea_sent() -> str:
    return render_message(MessageStyle(MessageKind.SONYA, "success", "Заказ передан"), body=["Передаю заказ Артёму.", "Чай скоро будет готов."])


def sonya_tea_cancelled() -> str:
    return render_message(MessageStyle(MessageKind.SONYA, "notification", "Заказ отменён"), body=["Хорошо, отменяю заказ."])
