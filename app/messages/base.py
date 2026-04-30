from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from html import escape


class MessageKind(StrEnum):
    NOTIFICATION = "notification"
    ORDER = "order"
    QUESTION = "question"
    WARNING = "warning"
    SUCCESS = "success"
    ERROR = "error"
    REMINDER = "reminder"
    DEVICE_STATUS = "device_status"
    MENU = "menu"
    SONYA = "sonya"


class EmojiProvider:
    def __init__(self) -> None:
        self._emoji = {
            "notification": "🔔",
            "coffee": "☕",
            "tea": "🫖",
            "water": "💧",
            "warning": "⚠️",
            "success": "✅",
            "error": "❌",
            "reminder": "⏰",
            "device": "🔌",
            "home": "🏠",
            "question": "❔",
            "settings": "⚙️",
            "delete": "🗑",
            "order": "📝",
        }

    def get(self, key: str) -> str:
        return self._emoji.get(key, "")


emoji = EmojiProvider()


@dataclass(frozen=True)
class MessageStyle:
    kind: MessageKind
    emoji_key: str
    title: str


def h(value: object) -> str:
    return escape("" if value is None else str(value), quote=False)


def em(value: object) -> str:
    return f"<em>{h(value)}</em>"


def render_message(
    style: MessageStyle,
    *,
    body: list[str] | None = None,
    details: list[tuple[str, object, bool]] | None = None,
    footer: str | None = None,
) -> str:
    lines = [f"{emoji.get(style.emoji_key)} <b>{h(style.title)}</b>", ""]
    if body:
        lines.extend(body)
    if details:
        for label, value, emphasize in details:
            rendered_value = em(value) if emphasize else h(value)
            lines.append(f"{h(label)}: {rendered_value}")
    if footer:
        lines.extend(["", footer])
    return "\n".join(lines)
