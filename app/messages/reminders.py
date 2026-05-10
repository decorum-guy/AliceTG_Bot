from __future__ import annotations

from datetime import datetime, timezone

from app.messages.base import MessageKind, MessageStyle, h, render_message
from app.services.reminder_store import ReminderRecord, ReminderSettings


def reminder_menu_text() -> str:
    return render_message(MessageStyle(MessageKind.MENU, "reminder", "Напоминания"), body=["Управление напоминаниями."])


def reminder_create_prompt() -> str:
    return render_message(
        MessageStyle(MessageKind.QUESTION, "question", "Новое напоминание"),
        body=["Напиши, о чём напомнить.", "Можно сразу указать время, например: напомни убрать посуду через 10 минут"],
    )


def reminder_delay_prompt(text: str) -> str:
    return render_message(
        MessageStyle(MessageKind.QUESTION, "question", "Через сколько напомнить?"),
        details=[("Текст", text, False)],
        footer="Напиши, например: через 10 минут или через 2 часа.",
    )


def reminder_time_parse_error() -> str:
    return render_message(
        MessageStyle(MessageKind.ERROR, "error", "Не понял время"),
        body=["Напиши, например: через 10 минут или через 2 часа."],
    )


def reminder_text_parse_error() -> str:
    return render_message(
        MessageStyle(MessageKind.ERROR, "error", "Не понял текст"),
        body=["Напиши, о чём напомнить, например: напомни убрать посуду через 10 минут."],
    )


def reminder_created(text: str, human_delay_text: str) -> str:
    return render_message(
        MessageStyle(MessageKind.SUCCESS, "success", "Напоминание создано"),
        details=[("Текст", text, False), ("Когда", f"через {human_delay_text}", False)],
    )


def reminder_notification(text: str) -> str:
    return render_message(MessageStyle(MessageKind.REMINDER, "reminder", "Напоминание"), body=[f"Напоминание: {h(text)}"])


def reminders_empty() -> str:
    return render_message(MessageStyle(MessageKind.REMINDER, "reminder", "Напоминания"), body=["Активных напоминаний нет."])


def reminders_list(reminders: list[ReminderRecord]) -> str:
    lines = []
    now = datetime.now(timezone.utc)
    for index, reminder in enumerate(reminders, start=1):
        due_at = reminder.due_datetime.astimezone()
        remaining_seconds = max(0, int((reminder.due_datetime - now).total_seconds()))
        lines.append(f"{index}. {h(reminder.text)}")
        lines.append(f"Когда: {h(due_at.strftime('%d.%m %H:%M'))} ({h(_remaining_text(remaining_seconds))})")
        lines.append(f"ID: {h(reminder.id)}")
        lines.append("")
    return render_message(MessageStyle(MessageKind.REMINDER, "reminder", "Активные напоминания"), body=lines)


def reminder_cancelled() -> str:
    return render_message(MessageStyle(MessageKind.SUCCESS, "success", "Напоминание удалено"), body=["Напоминание не сработает."])


def reminder_settings(settings: ReminderSettings) -> str:
    voice_status = "включено" if settings.voice_enabled else "выключено"
    station_label = _station_label(settings.voice_station_entity_id)
    telegram_status = "включено" if settings.notify_telegram_enabled else "выключено"
    iphone_status = "включено" if settings.notify_iphone_enabled else "выключено"
    details = [
        ("Озвучивание", voice_status, False),
        ("Колонка", station_label, False),
        ("Telegram", telegram_status, False),
        ("iPhone", iphone_status, False),
    ]
    if not settings.notify_telegram_enabled and not settings.notify_iphone_enabled:
        details.append(("Каналы уведомлений", "отключены", False))
    return render_message(
        MessageStyle(MessageKind.MENU, "settings", "Настройки напоминаний"),
        details=details,
    )
def _station_label(entity_id: str) -> str:
    labels = {
        "media_player.stantsiia_mini_zal": "Зал",
        "media_player.stantsiia_mini_spalnia": "Спальня",
    }
    return labels.get(entity_id, entity_id)


def _remaining_text(seconds: int) -> str:
    minutes = max(1, seconds // 60)
    if minutes < 60:
        return f"через {minutes} {_minute_word(minutes)}"
    hours = minutes // 60
    rest_minutes = minutes % 60
    if rest_minutes:
        return f"через {hours} {_hour_word(hours)} {rest_minutes} {_minute_word(rest_minutes)}"
    return f"через {hours} {_hour_word(hours)}"


def _minute_word(minutes: int) -> str:
    if 11 <= minutes % 100 <= 14:
        return "минут"
    if minutes % 10 == 1:
        return "минуту"
    if minutes % 10 in {2, 3, 4}:
        return "минуты"
    return "минут"


def _hour_word(hours: int) -> str:
    if 11 <= hours % 100 <= 14:
        return "часов"
    if hours % 10 == 1:
        return "час"
    if hours % 10 in {2, 3, 4}:
        return "часа"
    return "часов"
