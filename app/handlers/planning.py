from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery

from app.config import Settings
from app.keyboards.planning import planning_view_keyboard
from app.messages import planning as planning_messages
from app.planning.errors import (
    PlanningNotFoundError,
    PlanningValidationError,
    PlanningVersionConflictError,
    TelegramActionTokenBindingError,
    TelegramActionTokenConsumedError,
    TelegramActionTokenExpiredError,
    TelegramActionTokenUnknownError,
)
from app.planning.telegram_ui import (
    PLANNING_MENU_CALLBACK,
    PlanningNavigation,
    PlanningTelegramRateLimited,
    PlanningTelegramService,
    PlanningView,
    parse_navigation_callback,
)
from app.services.telegram_messages import TelegramMessages


router = Router()
LOGGER = logging.getLogger(__name__)


@router.callback_query(F.data == PLANNING_MENU_CALLBACK)
async def planning_menu_handler(
    callback: CallbackQuery,
    settings: Settings,
    planning_telegram_service: PlanningTelegramService,
    telegram_messages: TelegramMessages,
) -> None:
    if not _authorized(callback, settings):
        return await _deny(callback, settings)
    await _refresh(callback, planning_telegram_service.menu_view(), telegram_messages)
    await callback.answer()


@router.callback_query(F.data.startswith("planning:a:"))
async def planning_action_handler(
    callback: CallbackQuery,
    settings: Settings,
    planning_telegram_service: PlanningTelegramService,
    telegram_messages: TelegramMessages,
) -> None:
    if not _authorized(callback, settings):
        return await _deny(callback, settings)
    user_id = callback.from_user.id
    chat_id = _chat_id(callback)
    callback_data = callback.data or ""
    try:
        outcome = planning_telegram_service.execute_action(
            callback_data,
            telegram_user_id=user_id,
            telegram_chat_id=chat_id,
        )
    except PlanningTelegramRateLimited:
        await callback.answer("Слишком много действий. Попробуйте позже.", show_alert=True)
        return
    except TelegramActionTokenUnknownError:
        await callback.answer("Действие не найдено. Откройте Planning заново.", show_alert=True)
        return
    except TelegramActionTokenExpiredError:
        await callback.answer("Меню устарело. Откройте Planning заново.", show_alert=True)
        return
    except TelegramActionTokenConsumedError:
        await callback.answer("Это действие уже выполнено. Обновите список.", show_alert=True)
        return
    except TelegramActionTokenBindingError as exc:
        message = "Нет доступа к этому действию." if exc.reason in {"wrong_user", "wrong_chat"} else "Действие недоступно. Обновите список."
        await callback.answer(message, show_alert=True)
        return
    except PlanningVersionConflictError:
        await callback.answer("Список устарел. Обновите его.", show_alert=True)
        return
    except PlanningNotFoundError:
        await callback.answer("Объект уже недоступен. Обновите список.", show_alert=True)
        return
    except PlanningValidationError:
        await callback.answer("Действие сейчас недоступно. Обновите список.", show_alert=True)
        return
    except Exception:
        LOGGER.exception("Planning Telegram mutation failed without logging callback data")
        await callback.answer("Не удалось выполнить действие. Попробуйте обновить список.", show_alert=True)
        return

    # The action transaction is complete before this best-effort view refresh.
    # Delivery notifications keep their pre-send delete-only markup; these
    # tokens are issued only from this fresh canonical read.
    try:
        view = _view_after_action(
            planning_telegram_service,
            outcome.domain,
            user_id=user_id,
            chat_id=chat_id,
        )
    except Exception:
        # Canonical mutation already committed.  A view-format/token-refresh
        # failure must not turn that successful action into a rollback claim.
        LOGGER.exception("Planning Telegram view refresh failed after mutation")
        await callback.answer(_success_text(outcome.action))
        return
    await _refresh(callback, view, telegram_messages)
    await callback.answer(_success_text(outcome.action))


@router.callback_query(F.data.startswith("planning:"))
async def planning_navigation_handler(
    callback: CallbackQuery,
    settings: Settings,
    planning_telegram_service: PlanningTelegramService,
    telegram_messages: TelegramMessages,
) -> None:
    if not _authorized(callback, settings):
        return await _deny(callback, settings)
    if (callback.data or "").startswith("planning:a:"):
        await callback.answer("Действие недоступно. Откройте Planning заново.", show_alert=True)
        return
    try:
        target = parse_navigation_callback(callback.data)
        if target is None:
            raise ValueError("unknown Planning navigation callback")
        view = _view_for_navigation(
            planning_telegram_service,
            target,
            user_id=callback.from_user.id,
            chat_id=_chat_id(callback),
        )
    except ValueError:
        await callback.answer("Неизвестный раздел или страница.", show_alert=True)
        return
    except Exception:
        LOGGER.exception("Planning Telegram view failed without logging callback data")
        await callback.answer("Не удалось открыть Planning. Попробуйте ещё раз.", show_alert=True)
        return
    await _refresh(callback, view, telegram_messages)
    await callback.answer()


def _authorized(callback: CallbackQuery, settings: Settings) -> bool:
    if not getattr(settings, "planning_telegram_ui_enabled", False):
        return False
    return settings.is_admin_user(callback.from_user.id)


async def _deny(callback: CallbackQuery, settings: Settings) -> None:
    message = "Раздел недоступен" if not getattr(settings, "planning_telegram_ui_enabled", False) else "Нет доступа"
    await callback.answer(message, show_alert=True)


def _chat_id(callback: CallbackQuery) -> int | None:
    message = callback.message
    if message is None:
        return None
    chat = getattr(message, "chat", None)
    chat_id = getattr(chat, "id", None)
    return chat_id if isinstance(chat_id, int) else None


async def _refresh(callback: CallbackQuery, view: PlanningView, telegram_messages: TelegramMessages) -> None:
    chat_id = _chat_id(callback)
    message = callback.message
    message_id = getattr(message, "message_id", None) if message is not None else None
    if chat_id is None or not isinstance(message_id, int):
        return
    try:
        await telegram_messages.safe_edit(
            chat_id,
            message_id,
            view.text,
            planning_view_keyboard(view.rows),
        )
    except Exception:
        LOGGER.exception("Planning Telegram message refresh failed")


def _view_for_navigation(
    service: PlanningTelegramService,
    target: PlanningNavigation,
    *,
    user_id: int,
    chat_id: int | None,
) -> PlanningView:
    if target.kind == "menu":
        return service.menu_view()
    if target.kind == "reminders":
        return service.reminders_view(
            telegram_user_id=user_id,
            telegram_chat_id=chat_id,
            page=target.page,
        )
    if target.kind == "tasks":
        assert target.view is not None
        return service.tasks_view(
            view=target.view,  # type: ignore[arg-type]
            telegram_user_id=user_id,
            telegram_chat_id=chat_id,
            page=target.page,
        )
    assert target.view is not None
    return service.events_view(view=target.view, page=target.page)  # type: ignore[arg-type]


def _view_after_action(
    service: PlanningTelegramService,
    domain: str,
    *,
    user_id: int,
    chat_id: int | None,
) -> PlanningView:
    if domain == "reminder":
        return service.reminders_view(
            telegram_user_id=user_id,
            telegram_chat_id=chat_id,
            page=0,
        )
    return service.tasks_view(
        view="today",
        telegram_user_id=user_id,
        telegram_chat_id=chat_id,
        page=0,
    )


def _success_text(action: str) -> str:
    return {
        "reminder_complete": "Напоминание выполнено.",
        "reminder_cancel": "Напоминание отменено.",
        "reminder_retry": "Повторная доставка поставлена в очередь.",
        "task_complete": "Задача выполнена.",
    }.get(action, "Готово.")
