from __future__ import annotations

import inspect
from typing import Any

from aiogram.types import InlineKeyboardButton, KeyboardButton

BUTTON_SUCCESS = "success"
BUTTON_DANGER = "danger"
BUTTON_PRIMARY = "primary"

_INLINE_SUPPORTS_STYLE = "style" in inspect.signature(InlineKeyboardButton).parameters
_KEYBOARD_SUPPORTS_STYLE = "style" in inspect.signature(KeyboardButton).parameters


def inline_button(
    *,
    text: str,
    callback_data: str,
    style: str | None = None,
    **kwargs: Any,
) -> InlineKeyboardButton:
    if style is None:
        return InlineKeyboardButton(text=text, callback_data=callback_data, **kwargs)

    if _INLINE_SUPPORTS_STYLE:
        return InlineKeyboardButton(text=text, callback_data=callback_data, style=style, **kwargs)

    return InlineKeyboardButton(text=text, callback_data=callback_data, **kwargs, style=style)


def keyboard_button(
    *,
    text: str,
    style: str | None = None,
    **kwargs: Any,
) -> KeyboardButton:
    if style is None:
        return KeyboardButton(text=text, **kwargs)

    if _KEYBOARD_SUPPORTS_STYLE:
        return KeyboardButton(text=text, style=style, **kwargs)

    return KeyboardButton(text=text, **kwargs, style=style)
