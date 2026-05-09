from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from app.config import Settings
from app.keyboards.coffee import coffee_turn_off_only
from app.messages import coffee as coffee_messages
from app.services.app_state import AppStateStore
from app.services.telegram_messages import TelegramMessages

LOGGER = logging.getLogger(__name__)


class CoffeeAlertScheduler:
    def __init__(self, settings: Settings, app_state: AppStateStore, telegram_messages: TelegramMessages) -> None:
        self._settings = settings
        self._app_state = app_state
        self._telegram_messages = telegram_messages
        self._tasks: dict[str, asyncio.Task[None]] = {}

    async def restore(self) -> None:
        if self._app_state.coffee_machine_state != "on" or not self._app_state.coffee_on_since:
            LOGGER.info("Coffee alert restore skipped: state=%s", self._app_state.coffee_machine_state)
            return
        LOGGER.info("Coffee alert restore started: on_since=%s", self._app_state.coffee_on_since)
        self._schedule_active_alerts()

    async def handle_state(self, state: str, *, changed_at: str | None = None) -> None:
        normalized = state.strip().lower()
        if normalized == "on":
            on_since = _normalize_datetime(changed_at) or datetime.now(timezone.utc).isoformat()
            await self._app_state.mark_coffee_machine_on(on_since)
            LOGGER.info("Coffee machine state received: state=on on_since=%s", on_since)
            self._cancel_tasks()
            self._schedule_active_alerts()
            return
        if normalized == "off":
            LOGGER.info("Coffee machine state received: state=off")
            self._cancel_tasks()
            await self._app_state.mark_coffee_machine_off()
            return
        raise ValueError(f"Unsupported coffee machine state: {state}")

    def reschedule_active_alerts(self) -> None:
        if self._app_state.coffee_machine_state != "on" or not self._app_state.coffee_on_since:
            return
        LOGGER.info("Coffee alert timers rescheduled after settings update")
        self._cancel_tasks()
        self._schedule_active_alerts()

    def _schedule_active_alerts(self) -> None:
        if self._app_state.coffee_warmed_up_alert_enabled and not self._app_state.coffee_warmed_up_alert_sent:
            self._schedule("warmed_up", self._app_state.coffee_warmed_up_alert_delay_seconds)
        if self._app_state.coffee_long_running_alert_enabled and not self._app_state.coffee_long_running_alert_sent:
            self._schedule("long_running", self._app_state.coffee_long_running_alert_delay_seconds)

    def _schedule(self, alert: str, delay_seconds: int) -> None:
        on_since = self._app_state.coffee_on_since
        if not on_since:
            return
        elapsed = _elapsed_seconds(on_since)
        sleep_seconds = max(0, delay_seconds - elapsed)
        task = asyncio.create_task(self._wait_and_send(alert, sleep_seconds))
        self._tasks[alert] = task
        task.add_done_callback(lambda done_task, alert=alert: self._drop_task(alert, done_task))
        LOGGER.info(
            "Coffee alert timer scheduled: alert=%s delay_seconds=%s elapsed_seconds=%s sleep_seconds=%s",
            alert,
            delay_seconds,
            elapsed,
            sleep_seconds,
        )

    async def _wait_and_send(self, alert: str, sleep_seconds: int) -> None:
        try:
            if sleep_seconds > 0:
                await asyncio.sleep(sleep_seconds)
            if self._app_state.coffee_machine_state != "on":
                LOGGER.info("Coffee alert skipped because coffee machine is off: alert=%s", alert)
                return
            if alert == "warmed_up":
                should_send = await self._app_state.mark_coffee_warmed_up_alert_sent()
                text = coffee_messages.coffee_warmed_up()
            else:
                should_send = await self._app_state.mark_coffee_long_running_alert_sent()
                text = coffee_messages.coffee_warning_long_running_text(_runtime_text(self._app_state.coffee_on_since))
            if not should_send:
                LOGGER.info("Coffee alert skipped because already sent or inactive: alert=%s", alert)
                return
            await self._telegram_messages.safe_send(
                self._settings.telegram_admin_chat_id,
                text,
                reply_markup=coffee_turn_off_only(),
            )
            LOGGER.info("Coffee alert sent: alert=%s", alert)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("Coffee alert task failed: alert=%s", alert)

    def _cancel_tasks(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        self._tasks.clear()

    def _drop_task(self, alert: str, task: asyncio.Task[None]) -> None:
        if self._tasks.get(alert) is task:
            self._tasks.pop(alert, None)


def _normalize_datetime(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
    except ValueError:
        LOGGER.warning("Cannot parse coffee state changed_at: %s", value)
        return None


def _elapsed_seconds(on_since: str) -> int:
    try:
        started_at = datetime.fromisoformat(on_since)
    except ValueError:
        return 0
    return max(0, int((datetime.now(timezone.utc) - started_at).total_seconds()))


def _runtime_text(on_since: str | None) -> str:
    if not on_since:
        return "неизвестно"
    total_minutes = _elapsed_seconds(on_since) // 60
    hours = total_minutes // 60
    minutes = total_minutes % 60
    if hours and minutes:
        return f"{hours} ч {minutes} мин"
    if hours:
        return f"{hours} ч"
    return f"{minutes} мин"
