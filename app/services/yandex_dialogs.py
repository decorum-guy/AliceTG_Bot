from __future__ import annotations

from app.config import Settings


def yandex_dialog_content_type(settings: Settings, dialog_id: str) -> str:
    return f"dialog:{settings.yandex_dialog_skill_name}:{dialog_id}"
