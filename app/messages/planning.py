from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from app.messages.base import MessageKind, MessageStyle, h, render_message


@dataclass(frozen=True)
class PlanningDisplayEntry:
    title: str
    primary: str
    secondary: str | None = None


def planning_menu_text() -> str:
    return render_message(
        MessageStyle(MessageKind.MENU, "home", "Дела"),
        body=[
            "Напоминания, задачи и календарь из Planning.",
            "Доступ: только администратор.",
        ],
    )


def planning_list_text(
    *,
    title: str,
    emoji_key: str,
    entries: Sequence[PlanningDisplayEntry],
    page: int,
    empty_text: str,
) -> str:
    if not entries:
        body = [empty_text]
    else:
        body = [f"Страница {page + 1}", ""]
        for index, entry in enumerate(entries, start=1):
            body.append(f"{index}. {h(_truncate(entry.title))}")
            body.append(h(entry.primary))
            if entry.secondary:
                body.append(h(entry.secondary))
            body.append("")
        if body[-1] == "":
            body.pop()
    return render_message(MessageStyle(MessageKind.MENU, emoji_key, title), body=body)


def planning_action_error(message: str) -> str:
    return render_message(MessageStyle(MessageKind.ERROR, "error", "Действие недоступно"), body=[h(message)])


def planning_action_success(message: str) -> str:
    return render_message(MessageStyle(MessageKind.SUCCESS, "success", "Готово"), body=[h(message)])


def _truncate(value: str, limit: int = 140) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"
