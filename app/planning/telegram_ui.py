from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from typing import Callable, Literal
from zoneinfo import ZoneInfo

from app.messages import planning as planning_messages
from app.planning.errors import PlanningNotFoundError, PlanningValidationError
from app.planning.models import CalendarEvent, MutationContext, Reminder, Task, utc_now, validate_timezone
from app.planning.repositories import PlanningRepository
from app.planning.telegram_actions import (
    TelegramActionToken,
    TelegramActionTokenStore,
    TelegramMutationRateLimiter,
    validate_action_token_ttl,
)


PLANNING_PAGE_SIZE = 6
PLANNING_MAX_PAGE = 100
PLANNING_MENU_CALLBACK = "planning:menu"
_PAGE_PATTERN = r"(?:0|[1-9][0-9]{0,2})"
_REMINDER_CALLBACK_RE = re.compile(rf"^planning:reminders(?::({_PAGE_PATTERN}))?$")
_TASK_CALLBACK_RE = re.compile(rf"^planning:tasks:(today|overdue|upcoming)(?::({_PAGE_PATTERN}))?$")
_EVENT_CALLBACK_RE = re.compile(rf"^planning:events:(today|tomorrow|upcoming)(?::({_PAGE_PATTERN}))?$")


@dataclass(frozen=True)
class PlanningButton:
    text: str
    callback_data: str
    style: str | None = None


@dataclass(frozen=True)
class PlanningView:
    text: str
    rows: tuple[tuple[PlanningButton, ...], ...]


@dataclass(frozen=True)
class PlanningActionOutcome:
    action: str
    domain: str


class PlanningTelegramRateLimited(Exception):
    pass


NavigationKind = Literal["menu", "reminders", "tasks", "events"]


@dataclass(frozen=True)
class PlanningNavigation:
    kind: NavigationKind
    view: str | None
    page: int


def parse_navigation_callback(value: str | None) -> PlanningNavigation | None:
    """Parse only the closed A6 navigation callback grammar."""

    if value == PLANNING_MENU_CALLBACK:
        return PlanningNavigation("menu", None, 0)
    reminder_match = _REMINDER_CALLBACK_RE.fullmatch(value or "")
    if reminder_match:
        return PlanningNavigation("reminders", None, _bounded_page(reminder_match.group(1)))
    task_match = _TASK_CALLBACK_RE.fullmatch(value or "")
    if task_match:
        return PlanningNavigation("tasks", task_match.group(1), _bounded_page(task_match.group(2)))
    event_match = _EVENT_CALLBACK_RE.fullmatch(value or "")
    if event_match:
        return PlanningNavigation("events", event_match.group(1), _bounded_page(event_match.group(2)))
    return None


def _bounded_page(value: str | None) -> int:
    page = 0 if value is None else int(value)
    if page < 0 or page > PLANNING_MAX_PAGE:
        raise ValueError("Planning page is out of range")
    return page


class PlanningTelegramService:
    """Telegram-facing Planning application service.

    This layer owns compact view models and explicit domain actions.  It never
    accepts titles, commands or arbitrary service/entity arguments from a
    callback; mutation targets come only from the persistent token record.
    """

    def __init__(
        self,
        database,
        *,
        repository: PlanningRepository | None = None,
        action_tokens: TelegramActionTokenStore | None = None,
        default_timezone: str = "Europe/Moscow",
        action_token_ttl_seconds: int = 900,
        callback_rate_limit_per_minute: int = 30,
        now_fn: Callable[[], str] = utc_now,
        rate_now_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        validate_timezone(default_timezone, "planning.telegram.default_timezone")
        validate_action_token_ttl(action_token_ttl_seconds)
        self.database = database
        self.repository = repository or PlanningRepository(database, now_fn=now_fn)
        self.default_timezone = default_timezone
        self._zone = ZoneInfo(default_timezone)
        self._now_fn = now_fn
        self.action_tokens = action_tokens or TelegramActionTokenStore(
            database,
            ttl_seconds=action_token_ttl_seconds,
            now_fn=now_fn,
        )
        self.rate_limiter = TelegramMutationRateLimiter(
            limit=callback_rate_limit_per_minute,
            now_fn=rate_now_fn,
        )

    def menu_view(self) -> PlanningView:
        return PlanningView(
            text=planning_messages.planning_menu_text(),
            rows=(
                (PlanningButton("🔔 Напоминания", "planning:reminders:0"),),
                (PlanningButton("✅ Задачи", "planning:tasks:today:0"),),
                (PlanningButton("📅 Календарь", "planning:events:today:0"),),
                (PlanningButton("⬅️ Назад", "menu:main", "primary"),),
            ),
        )

    def reminders_view(
        self,
        *,
        telegram_user_id: int,
        telegram_chat_id: int | None,
        page: int = 0,
        now: str | None = None,
    ) -> PlanningView:
        page = _validate_page(page)
        now_value = now or self._now_fn()
        now_dt = self._utc_datetime(now_value)
        reminders = self.repository.list_active_reminders(
            limit=PLANNING_PAGE_SIZE + 1,
            offset=page * PLANNING_PAGE_SIZE,
        )
        visible = reminders[:PLANNING_PAGE_SIZE]
        rows: list[tuple[PlanningButton, ...]] = []
        entries: list[planning_messages.PlanningDisplayEntry] = []
        for index, reminder in enumerate(visible, start=1):
            due_dt = self._utc_datetime(reminder.due_at_utc)
            due_local = due_dt.astimezone(self._zone)
            entries.append(
                planning_messages.PlanningDisplayEntry(
                    title=reminder.title,
                    primary=f"{due_local.strftime('%d.%m.%Y %H:%M')} · {_relative_label(due_dt, now_dt)}",
                    secondary=_reminder_state_label(reminder, due_dt, now_dt),
                )
            )
            action_buttons = [
                PlanningButton(
                    f"✅ Выполнить {index}",
                    self.issue_reminder_complete_token(
                        reminder=reminder,
                        telegram_user_id=telegram_user_id,
                        telegram_chat_id=telegram_chat_id,
                    ),
                    "success",
                ),
                PlanningButton(
                    f"❌ Отменить {index}",
                    self.issue_reminder_cancel_token(
                        reminder=reminder,
                        telegram_user_id=telegram_user_id,
                        telegram_chat_id=telegram_chat_id,
                    ),
                    "danger",
                ),
            ]
            if reminder.delivery_state == "failed":
                action_buttons.append(
                    PlanningButton(
                        f"🔁 Повторить {index}",
                        self.issue_reminder_retry_token(
                            reminder=reminder,
                            telegram_user_id=telegram_user_id,
                            telegram_chat_id=telegram_chat_id,
                        ),
                        "primary",
                    )
                )
            rows.append(tuple(action_buttons))
        rows.extend(self._navigation_rows("reminders", None, page, len(reminders) > PLANNING_PAGE_SIZE))
        return PlanningView(
            text=planning_messages.planning_list_text(
                title="Активные напоминания",
                emoji_key="reminder",
                entries=entries,
                page=page,
                empty_text="Активных напоминаний нет.",
            ),
            rows=tuple(rows),
        )

    def tasks_view(
        self,
        *,
        view: Literal["today", "overdue", "upcoming"],
        telegram_user_id: int,
        telegram_chat_id: int | None,
        page: int = 0,
        now: str | None = None,
    ) -> PlanningView:
        page = _validate_page(page)
        if view not in {"today", "overdue", "upcoming"}:
            raise PlanningValidationError("Planning task view is not allowlisted")
        today = self._local_date(now).isoformat()
        tasks = self.repository.list_tasks(
            view=view,
            today=today,
            limit=PLANNING_PAGE_SIZE + 1,
            offset=page * PLANNING_PAGE_SIZE,
        )
        visible = tasks[:PLANNING_PAGE_SIZE]
        entries = [
            planning_messages.PlanningDisplayEntry(
                title=task.title,
                primary=_task_when_label(task),
                secondary=_task_state_label(view),
            )
            for task in visible
        ]
        rows: list[tuple[PlanningButton, ...]] = []
        for index, task in enumerate(visible, start=1):
            rows.append(
                (
                    PlanningButton(
                        f"✅ Выполнить {index}",
                        self.issue_task_complete_token(
                            task=task,
                            telegram_user_id=telegram_user_id,
                            telegram_chat_id=telegram_chat_id,
                        ),
                        "success",
                    ),
                )
            )
        rows.extend(self._section_tabs("tasks", view))
        rows.extend(self._navigation_rows("tasks", view, page, len(tasks) > PLANNING_PAGE_SIZE))
        labels = {"today": "сегодня", "overdue": "просроченные", "upcoming": "предстоящие"}
        return PlanningView(
            text=planning_messages.planning_list_text(
                title=f"Задачи · {labels[view]}",
                emoji_key="success",
                entries=entries,
                page=page,
                empty_text="Открытых задач нет.",
            ),
            rows=tuple(rows),
        )

    def events_view(
        self,
        *,
        view: Literal["today", "tomorrow", "upcoming"],
        page: int = 0,
        now: str | None = None,
    ) -> PlanningView:
        page = _validate_page(page)
        if view not in {"today", "tomorrow", "upcoming"}:
            raise PlanningValidationError("Planning event view is not allowlisted")
        local_start, local_end = self._event_date_range(view, now)
        from_utc = self._local_midnight_utc(local_start)
        to_utc = self._local_midnight_utc(local_end)
        # Fetch a bounded window before applying the caller-timezone sort so a
        # UTC-order tie around midnight cannot hide a local-day event.
        events = self.repository.list_calendar_events(
            from_utc=from_utc,
            to_utc=to_utc,
            from_local_date=local_start.isoformat(),
            to_local_date=local_end.isoformat(),
            limit=1001,
            offset=0,
        )
        events.sort(key=self._event_sort_key)
        start_index = page * PLANNING_PAGE_SIZE
        visible = events[start_index : start_index + PLANNING_PAGE_SIZE]
        entries = [
            planning_messages.PlanningDisplayEntry(
                title=event.title,
                primary=_event_when_label(event, self._zone),
                secondary=_event_sync_label(event),
            )
            for event in visible
        ]
        rows = list(self._section_tabs("events", view))
        rows.extend(self._navigation_rows("events", view, page, len(events) > start_index + PLANNING_PAGE_SIZE))
        labels = {"today": "сегодня", "tomorrow": "завтра", "upcoming": "предстоящие"}
        return PlanningView(
            text=planning_messages.planning_list_text(
                title=f"Календарь · {labels[view]}",
                emoji_key="notification",
                entries=entries,
                page=page,
                empty_text="Событий нет.",
            ),
            rows=tuple(rows),
        )

    def issue_reminder_complete_token(
        self, *, reminder: Reminder, telegram_user_id: int, telegram_chat_id: int | None
    ) -> str:
        return self.action_tokens.issue(
            action="reminder_complete",
            domain="reminder",
            object_id=reminder.id,
            expected_version=reminder.version,
            telegram_user_id=telegram_user_id,
            telegram_chat_id=telegram_chat_id,
        ).callback_data

    def issue_reminder_cancel_token(
        self, *, reminder: Reminder, telegram_user_id: int, telegram_chat_id: int | None
    ) -> str:
        return self.action_tokens.issue(
            action="reminder_cancel",
            domain="reminder",
            object_id=reminder.id,
            expected_version=reminder.version,
            telegram_user_id=telegram_user_id,
            telegram_chat_id=telegram_chat_id,
        ).callback_data

    def issue_reminder_retry_token(
        self, *, reminder: Reminder, telegram_user_id: int, telegram_chat_id: int | None
    ) -> str:
        return self.action_tokens.issue(
            action="reminder_retry",
            domain="reminder",
            object_id=reminder.id,
            expected_version=reminder.version,
            telegram_user_id=telegram_user_id,
            telegram_chat_id=telegram_chat_id,
        ).callback_data

    def issue_task_complete_token(
        self, *, task: Task, telegram_user_id: int, telegram_chat_id: int | None
    ) -> str:
        return self.action_tokens.issue(
            action="task_complete",
            domain="task",
            object_id=task.id,
            expected_version=task.version,
            telegram_user_id=telegram_user_id,
            telegram_chat_id=telegram_chat_id,
        ).callback_data

    def execute_action(
        self,
        callback_data: str,
        *,
        telegram_user_id: int,
        telegram_chat_id: int | None,
        now: str | None = None,
    ) -> PlanningActionOutcome:
        if not self.rate_limiter.allow(telegram_user_id):
            raise PlanningTelegramRateLimited()
        return self.action_tokens.consume(
            callback_data,
            telegram_user_id=telegram_user_id,
            telegram_chat_id=telegram_chat_id,
            now=now,
            mutation=self._mutate_from_token,
        )

    def _mutate_from_token(self, token: TelegramActionToken) -> PlanningActionOutcome:
        context = MutationContext(
            audience="operator",
            actor_id=f"telegram:{token.telegram_user_id}",
            actor_type="operator",
            surface="telegram",
        )
        if token.domain == "reminder" and token.action == "reminder_complete":
            self.repository.complete_reminder(
                token.object_id,
                expected_version=token.expected_version,
                context=context,
            )
            return PlanningActionOutcome("reminder_complete", "reminder")
        if token.domain == "reminder" and token.action == "reminder_cancel":
            self.repository.cancel_reminder(
                token.object_id,
                expected_version=token.expected_version,
                context=context,
            )
            return PlanningActionOutcome("reminder_cancel", "reminder")
        if token.domain == "reminder" and token.action == "reminder_retry":
            self.repository.manual_retry_reminder(
                token.object_id,
                expected_version=token.expected_version,
                context=context,
            )
            return PlanningActionOutcome("reminder_retry", "reminder")
        if token.domain == "task" and token.action == "task_complete":
            self.repository.complete_task(
                token.object_id,
                expected_version=token.expected_version,
                context=context,
            )
            return PlanningActionOutcome("task_complete", "task")
        raise PlanningValidationError("Telegram action record is not allowlisted")

    def _navigation_rows(
        self,
        kind: NavigationKind,
        view: str | None,
        page: int,
        has_more: bool,
    ) -> list[tuple[PlanningButton, ...]]:
        rows: list[tuple[PlanningButton, ...]] = []
        if page > 0 or has_more:
            previous = self._navigation_callback(kind, view, page - 1) if page > 0 else None
            following = self._navigation_callback(kind, view, page + 1) if has_more else None
            buttons: list[PlanningButton] = []
            if previous:
                buttons.append(PlanningButton("⬅️ Пред.", previous, "primary"))
            if following:
                buttons.append(PlanningButton("След. ➡️", following, "primary"))
            rows.append(tuple(buttons))
        rows.append(
            (
                PlanningButton("🔄 Обновить", self._navigation_callback(kind, view, page), "primary"),
                PlanningButton("📋 Дела", PLANNING_MENU_CALLBACK, "primary"),
            )
        )
        return rows

    @staticmethod
    def _section_tabs(kind: Literal["tasks", "events"], view: str) -> list[tuple[PlanningButton, ...]]:
        if kind == "tasks":
            choices = (("сегодня", "today"), ("просроченные", "overdue"), ("предстоящие", "upcoming"))
            prefix = "planning:tasks"
        else:
            choices = (("сегодня", "today"), ("завтра", "tomorrow"), ("предстоящие", "upcoming"))
            prefix = "planning:events"
        return [
            tuple(
                PlanningButton(
                    label if selected != value else f"• {label}",
                    f"{prefix}:{value}:0",
                    "primary",
                )
                for label, value in choices
                for selected in [view]
            )
        ]

    @staticmethod
    def _navigation_callback(kind: NavigationKind, view: str | None, page: int) -> str:
        if kind == "reminders":
            return f"planning:reminders:{page}"
        if kind == "tasks" and view in {"today", "overdue", "upcoming"}:
            return f"planning:tasks:{view}:{page}"
        if kind == "events" and view in {"today", "tomorrow", "upcoming"}:
            return f"planning:events:{view}:{page}"
        raise ValueError("invalid Planning navigation target")

    def _local_date(self, now: str | None) -> date:
        now_value = now or self._now_fn()
        return self._utc_datetime(now_value).astimezone(self._zone).date()

    def _event_date_range(self, view: str, now: str | None) -> tuple[date, date]:
        today = self._local_date(now)
        if view == "today":
            start = today
            end = today + timedelta(days=1)
        elif view == "tomorrow":
            start = today + timedelta(days=1)
            end = today + timedelta(days=2)
        else:
            start = today + timedelta(days=2)
            end = today + timedelta(days=367)
        return start, end

    def _local_midnight_utc(self, value: date) -> str:
        local = datetime.combine(value, datetime_time.min, tzinfo=self._zone)
        return local.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    @staticmethod
    def _utc_datetime(value: str) -> datetime:
        return datetime.fromisoformat(value[:-1] + "+00:00")

    def _event_sort_key(self, event: CalendarEvent) -> tuple[date, int, str, str]:
        if event.all_day:
            assert event.start_date is not None
            return (date.fromisoformat(event.start_date), 0, "", event.id)
        assert event.start_at_utc is not None
        start = self._utc_datetime(event.start_at_utc).astimezone(self._zone)
        return (start.date(), 1, start.time().isoformat(), event.id)


def _validate_page(page: int) -> int:
    if isinstance(page, bool) or not isinstance(page, int) or not 0 <= page <= PLANNING_MAX_PAGE:
        raise ValueError("Planning page is out of range")
    return page


def _relative_label(due: datetime, now: datetime) -> str:
    seconds = int((due - now).total_seconds())
    if abs(seconds) < 60:
        return "сейчас"
    if seconds > 0:
        return f"через {_duration_text(seconds)}"
    return f"просрочено на {_duration_text(-seconds)}"


def _duration_text(seconds: int) -> str:
    minutes = max(1, seconds // 60)
    if minutes < 60:
        return f"{minutes} {_minute_word(minutes)}"
    hours, remainder = divmod(minutes, 60)
    if remainder:
        return f"{hours} {_hour_word(hours)} {remainder} {_minute_word(remainder)}"
    return f"{hours} {_hour_word(hours)}"


def _minute_word(value: int) -> str:
    if 11 <= value % 100 <= 14:
        return "минут"
    if value % 10 == 1:
        return "минуту"
    if value % 10 in {2, 3, 4}:
        return "минуты"
    return "минут"


def _hour_word(value: int) -> str:
    if 11 <= value % 100 <= 14:
        return "часов"
    if value % 10 == 1:
        return "час"
    if value % 10 in {2, 3, 4}:
        return "часа"
    return "часов"


def _reminder_state_label(reminder: Reminder, due: datetime, now: datetime) -> str:
    if reminder.delivery_state == "failed":
        return "доставка не удалась"
    if reminder.delivery_state == "retrying":
        return "повторная доставка"
    if reminder.delivery_state == "queued":
        return "в очереди"
    if reminder.delivery_state == "delivered":
        return "доставлено · ждёт выполнения"
    if reminder.status == "due" or due <= now:
        return "срок наступил"
    return "не наступило"


def _task_when_label(task: Task) -> str:
    if task.due_date is None:
        return "без даты"
    value = date.fromisoformat(task.due_date).strftime("%d.%m.%Y")
    if task.due_time is not None:
        return f"{value} {task.due_time}"
    return f"{value} · весь день"


def _task_state_label(view: str) -> str:
    return {
        "today": "срок сегодня",
        "overdue": "просрочено",
        "upcoming": "предстоящее",
    }[view]


def _event_when_label(event: CalendarEvent, zone: ZoneInfo) -> str:
    if event.all_day:
        assert event.start_date is not None and event.end_date_exclusive is not None
        start = date.fromisoformat(event.start_date)
        last = date.fromisoformat(event.end_date_exclusive) - timedelta(days=1)
        if start == last:
            return f"{start.strftime('%d.%m.%Y')} · весь день"
        return f"{start.strftime('%d.%m.%Y')}–{last.strftime('%d.%m.%Y')} · весь день"
    assert event.start_at_utc is not None and event.end_at_utc is not None
    start = datetime.fromisoformat(event.start_at_utc[:-1] + "+00:00").astimezone(zone)
    end = datetime.fromisoformat(event.end_at_utc[:-1] + "+00:00").astimezone(zone)
    return f"{start.strftime('%d.%m.%Y %H:%M')}–{end.strftime('%H:%M')}"


def _event_sync_label(event: CalendarEvent) -> str | None:
    if event.sync_state == "local_only":
        return "локально · не синхронизировано"
    return f"состояние синхронизации: {event.sync_state}"
