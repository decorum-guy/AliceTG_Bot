from __future__ import annotations

from app.messages.base import MessageKind, MessageStyle, h, render_message


WATER_STYLE = MessageStyle(MessageKind.ORDER, "water", "Заказ воды")


def water_questioning() -> str:
    return render_message(MessageStyle(MessageKind.QUESTION, "question", "Спрашиваю Соню"), body=["Уточняю, хочет ли Соня воды."])


def water_confirmation(comment: str) -> str:
    return render_message(
        WATER_STYLE,
        body=["Соня попросила воды."],
        details=[("Комментарий", comment, False)],
        footer="Когда сможешь принести?",
    )


def water_done_now(comment: str) -> str:
    return render_message(
        MessageStyle(MessageKind.SUCCESS, "success", "Готово"),
        body=["Соня просила воды.", "Артём скоро принесёт воду."],
        details=[("Комментарий", comment, False)],
    )


def water_scheduled(minutes: int, minute_label: str, comment: str) -> str:
    return render_message(
        MessageStyle(MessageKind.REMINDER, "reminder", "Отложено"),
        body=["Соня просила воды."],
        details=[("Комментарий", comment, False)],
        footer=f"Напомню через {h(minutes)} {h(minute_label)}.",
    )


def water_reminder(comment: str) -> str:
    return render_message(
        MessageStyle(MessageKind.REMINDER, "reminder", "Напоминание"),
        body=["Напоминаю, Соня просила принести воды."],
        details=[("Комментарий", comment, False)],
    )


def sonya_water_comment_question() -> str:
    return render_message(
        MessageStyle(MessageKind.SONYA, "water", "Комментарий к заказу"),
        body=[
            "Напиши пожелания к воде одним сообщением.",
            "Если пожеланий нет, нажми «Без комментария».",
        ],
    )


def sonya_water_sent() -> str:
    return render_message(
        MessageStyle(MessageKind.SONYA, "success", "Заказ передан"),
        body=["Заказ успешно оформлен и передан Артёму."],
    )


def sonya_water_cancelled() -> str:
    return render_message(MessageStyle(MessageKind.SONYA, "error", "Заказ отменен"), body=["Заказ воды отменен."])
