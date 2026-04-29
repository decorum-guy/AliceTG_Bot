from __future__ import annotations

from app.messages.base import MessageKind, MessageStyle, em, h, render_message


COFFEE_STYLE = MessageStyle(MessageKind.ORDER, "coffee", "Заказ кофе")


def coffee_order_progress(temperature: str | None, syrup: str | None) -> str:
    return render_message(
        COFFEE_STYLE,
        body=["Соня хочет кофе."],
        details=[
            ("Тип", temperature or "уточняю", temperature is None),
            ("Сироп", syrup or "уточняю", syrup is None),
        ],
    )


def coffee_order_confirmation(temperature: str, syrup: str) -> str:
    return render_message(
        COFFEE_STYLE,
        body=["Соня попросила кофе."],
        details=[("Тип", temperature, False), ("Сироп", syrup, False)],
        footer="Включить кофемашину?",
    )


def coffee_auto_enabled(temperature: str, syrup: str) -> str:
    return render_message(
        COFFEE_STYLE,
        body=["Соня заказала кофе."],
        details=[("Тип", temperature, False), ("Сироп", syrup, False)],
        footer="Кофемашина уже включена.",
    )


def coffee_reminder(temperature: str, syrup: str) -> str:
    return render_message(
        MessageStyle(MessageKind.REMINDER, "reminder", "Напоминание"),
        body=["Соня просила кофе."],
        details=[("Тип", temperature, False), ("Сироп", syrup, False)],
        footer="Включить кофемашину?",
    )


def coffee_started(coffee_type: str) -> str:
    return render_message(
        MessageStyle(MessageKind.SUCCESS, "success", "Готово"),
        body=["Я включила кофемашину.", f"Соня просила: {h(coffee_type)}."],
    )


def coffee_declined(coffee_type: str) -> str:
    return render_message(
        MessageStyle(MessageKind.NOTIFICATION, "notification", "Заказ кофе"),
        body=["Я не включаю кофемашину.", f"Соня просила: {h(coffee_type)}."],
    )


def coffee_refused() -> str:
    return render_message(COFFEE_STYLE, body=["Соня отказалась от кофе.", "Не включаю кофемашину."])


def coffee_hall_refused() -> str:
    return render_message(COFFEE_STYLE, body=["Соня отказалась от кофе.", "Кофемашина не включена."])


def coffee_questioning() -> str:
    return render_message(MessageStyle(MessageKind.QUESTION, "question", "Спрашиваю Соню"), body=["Уточняю, хочет ли Соня кофе."])


def coffee_unknown_answer(answer: str) -> str:
    return render_message(
        MessageStyle(MessageKind.QUESTION, "question", "Ответ Сони"),
        body=[f"Соня ответила: {h(answer)}.", "Включить кофемашину?"],
    )


def coffee_status_text(*, is_on: bool, uptime: str, sensors: list[tuple[str, str]]) -> str:
    details: list[tuple[str, object, bool]] = [
        ("Состояние", "включена" if is_on else "выключена", False),
        ("Время работы", uptime, False),
    ]
    details.extend((label, value, False) for label, value in sensors)
    return render_message(MessageStyle(MessageKind.DEVICE_STATUS, "coffee", "Кофемашина"), details=details)


def coffee_warning_long_running(minutes: int) -> str:
    return render_message(
        MessageStyle(MessageKind.WARNING, "warning", "Предупреждение"),
        body=[f"Кофемашина работает уже {h(minutes)} минут."],
        footer="Выключить?",
    )


def coffee_warning_long_running_text(runtime_text: str) -> str:
    return render_message(
        MessageStyle(MessageKind.WARNING, "warning", "Предупреждение"),
        body=["Кофемашина работает непрерывно уже около часа.", f"Время работы: {h(runtime_text)}"],
        footer="Выключить?",
    )


def coffee_warmed_up() -> str:
    return render_message(
        MessageStyle(MessageKind.NOTIFICATION, "notification", "Уведомление"),
        body=["Кофемашина разогрета."],
        footer="Выключить?",
    )


def coffee_alert_turned_off() -> str:
    return render_message(MessageStyle(MessageKind.SUCCESS, "success", "Готово"), body=["Кофемашина выключена."])


def sonya_coffee_temperature_question() -> str:
    return render_message(MessageStyle(MessageKind.SONYA, "coffee", "Кофе"), body=["Какой кофе сделать?"])


def sonya_coffee_syrup_question() -> str:
    return render_message(MessageStyle(MessageKind.SONYA, "coffee", "Кофе"), body=["Добавить сироп?"])


def sonya_coffee_confirm(order_text: str) -> str:
    return render_message(
        MessageStyle(MessageKind.SONYA, "order", "Проверяю заказ"),
        body=[f"{h(order_text)}.", "Всё верно?"],
    )


def sonya_coffee_sent() -> str:
    return render_message(MessageStyle(MessageKind.SONYA, "success", "Заказ передан"), body=["Передаю заказ Артёму.", "Кофе скоро будет готов."])


def sonya_order_cancelled() -> str:
    return render_message(MessageStyle(MessageKind.SONYA, "notification", "Заказ отменён"), body=["Хорошо, отменяю заказ."])
