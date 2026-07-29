from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from app.config import Settings
from app.keyboards.coffee import coffee_turn_off_only
from app.messages import coffee as coffee_messages
from app.services.app_state import AppStateStore
from app.services.coffee_timing_policy import CoffeeTimingPolicyService
from app.services.home_assistant import HomeAssistantClient, HomeAssistantError
from app.services.pushward import PushWardCoffeeActivity
from app.services.pushward_widgets import PushWardCoffeeWidget
from app.services.telegram_messages import TelegramMessages

LOGGER = logging.getLogger(__name__)
COFFEE_ALERT_RETRY_DELAYS_SECONDS = (0, 30, 60, 180, 300)


class CoffeeAlertScheduler:
    def __init__(
        self,
        settings: Settings,
        app_state: AppStateStore,
        telegram_messages: TelegramMessages,
        ha: HomeAssistantClient,
        timing_policy: CoffeeTimingPolicyService,
        pushward_activity: PushWardCoffeeActivity | None = None,
        pushward_widget: PushWardCoffeeWidget | None = None,
    ) -> None:
        self._settings = settings
        self._app_state = app_state
        self._telegram_messages = telegram_messages
        self._ha = ha
        self._timing_policy = timing_policy
        self._pushward_activity = pushward_activity
        self._pushward_widget = pushward_widget
        self._tasks: dict[str, asyncio.Task[None]] = {}

    async def restore(self) -> None:
        if self._app_state.coffee_machine_state != "on" or not self._app_state.coffee_on_since:
            LOGGER.info("Coffee alert restore skipped: state=%s", self._app_state.coffee_machine_state)
            return
        LOGGER.info("Coffee alert restore started: on_since=%s", self._app_state.coffee_on_since)
        if self._pushward_activity:
            await self._pushward_activity.start_or_restore()
        if self._pushward_widget:
            self._pushward_widget.start()
        self._schedule_active_alerts()

    async def handle_state(self, state: str, *, changed_at: str | None = None) -> None:
        normalized = state.strip().lower()
        if normalized == "on":
            if self._app_state.coffee_machine_state == "on" and self._app_state.coffee_on_since:
                LOGGER.info(
                    "Coffee machine duplicate state=on ignored: existing_on_since=%s changed_at=%s",
                    self._app_state.coffee_on_since,
                    changed_at,
                )
                if self._pushward_activity:
                    await self._pushward_activity.start_or_restore()
                if self._pushward_widget:
                    self._pushward_widget.start()
                return
            on_since = _normalize_datetime(changed_at) or datetime.now(timezone.utc).isoformat()
            await self._app_state.mark_coffee_machine_on(on_since)
            LOGGER.info("Coffee machine state received: state=on on_since=%s", on_since)
            self._cancel_tasks()
            if self._pushward_activity:
                await self._pushward_activity.start_or_restore()
            if self._pushward_widget:
                self._pushward_widget.start()
            self._schedule_active_alerts()
            return
        if normalized == "off":
            LOGGER.info("Coffee machine state received: state=off")
            self._cancel_tasks(reason="coffee_machine_off")
            if self._pushward_activity:
                await self._pushward_activity.stop()
            await self._app_state.mark_coffee_machine_off()
            if self._pushward_widget:
                self._pushward_widget.stop()
            return
        raise ValueError(f"Unsupported coffee machine state: {state}")

    def reschedule_active_alerts(self) -> None:
        if self._app_state.coffee_machine_state != "on" or not self._app_state.coffee_on_since:
            return
        LOGGER.info("Coffee alert timers rescheduled after settings update")
        self._cancel_tasks()
        if self._pushward_activity:
            asyncio.create_task(self._pushward_activity.start_or_restore())
        self._schedule_active_alerts()

    def _schedule_active_alerts(self) -> None:
        warmup_seconds = self._timing_policy.warmup_duration_seconds
        long_running_seconds = self._timing_policy.long_running_threshold_seconds
        if warmup_seconds is None or long_running_seconds is None:
            LOGGER.warning("Coffee alerts not scheduled: canonical HA timing policy is unavailable")
            return
        if self._app_state.coffee_warmed_up_alert_enabled and not self._is_alert_delivered("warmed_up"):
            self._schedule("warmed_up", warmup_seconds)
        if self._app_state.coffee_long_running_alert_enabled and not self._is_alert_delivered("long_running"):
            self._schedule("long_running", long_running_seconds)

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
                LOGGER.info("Coffee alert cancelled because coffee machine is off: alert=%s", alert)
                return
            if self._is_alert_delivered(alert):
                LOGGER.info("Coffee alert skipped because already sent or inactive: alert=%s", alert)
                return

            if self._pushward_activity:
                if alert == "warmed_up":
                    await self._pushward_activity.mark_warmed_up()
                else:
                    await self._pushward_activity.mark_long_running()

            text = self._alert_text(alert)
            title, push_message = self._push_text(alert)
            for attempt, retry_delay in enumerate(COFFEE_ALERT_RETRY_DELAYS_SECONDS, start=1):
                if retry_delay > 0:
                    LOGGER.info(
                        "Coffee alert retry scheduled: alert=%s retry_in_seconds=%s",
                        alert,
                        retry_delay,
                    )
                    await asyncio.sleep(retry_delay)
                if self._app_state.coffee_machine_state != "on":
                    LOGGER.info("Coffee alert cancelled because coffee machine is off: alert=%s", alert)
                    return
                if self._is_alert_delivered(alert):
                    LOGGER.info("Coffee alert skipped because already sent or inactive: alert=%s", alert)
                    return

                if self._telegram_channel_enabled(alert) and not self._telegram_channel_delivered(alert):
                    LOGGER.info("Coffee alert send attempt: alert=%s channel=telegram attempt=%s", alert, attempt)
                    message_id = await self._telegram_messages.safe_send(
                        self._settings.telegram_admin_chat_id,
                        text,
                        reply_markup=coffee_turn_off_only(),
                    )
                    if message_id is None:
                        LOGGER.warning(
                            "Coffee alert send failed: alert=%s channel=telegram attempt=%s reason=safe_send_returned_none",
                            alert,
                            attempt,
                        )
                    else:
                        await self._mark_telegram_channel_delivered(alert)
                        LOGGER.info(
                            "Coffee alert sent: alert=%s channel=telegram message_id=%s",
                            alert,
                            message_id,
                        )

                if self._iphone_channel_enabled(alert) and not self._iphone_channel_delivered(alert):
                    if await self._send_mobile_alert(alert, title=title, message=push_message, attempt=attempt):
                        await self._mark_iphone_channel_delivered(alert)

                if self._is_alert_delivered(alert):
                    LOGGER.info("Coffee alert delivered through all enabled channels: alert=%s", alert)
                    return

            LOGGER.error(
                "Coffee alert remains pending after retry exhaustion: alert=%s attempts=%s",
                alert,
                len(COFFEE_ALERT_RETRY_DELAYS_SECONDS),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("Coffee alert task failed: alert=%s", alert)

    def _cancel_tasks(self, *, reason: str = "reschedule") -> None:
        for alert, task in self._tasks.items():
            if reason == "coffee_machine_off":
                LOGGER.info("Coffee alert cancelled because coffee machine is off: alert=%s", alert)
            task.cancel()
        self._tasks.clear()

    def _drop_task(self, alert: str, task: asyncio.Task[None]) -> None:
        if self._tasks.get(alert) is task:
            self._tasks.pop(alert, None)

    def _is_alert_delivered(self, alert: str) -> bool:
        effective_channels = []
        if self._telegram_channel_enabled(alert):
            effective_channels.append(self._telegram_channel_delivered(alert))
        if self._iphone_channel_enabled(alert) and self._settings.ha_mobile_notify_services:
            effective_channels.append(self._iphone_channel_delivered(alert))
        if not effective_channels:
            LOGGER.info("Coffee alert has no effective delivery channels: alert=%s", alert)
            return True
        return all(effective_channels)

    def _telegram_channel_enabled(self, alert: str) -> bool:
        if alert == "warmed_up":
            return self._app_state.coffee_warmed_up_notify_telegram
        return self._app_state.coffee_long_running_notify_telegram

    def _iphone_channel_enabled(self, alert: str) -> bool:
        if alert == "warmed_up":
            return self._app_state.coffee_warmed_up_notify_iphone
        return self._app_state.coffee_long_running_notify_iphone

    def _telegram_channel_delivered(self, alert: str) -> bool:
        if alert == "warmed_up":
            return self._app_state.coffee_warmed_up_alert_sent or self._app_state.coffee_warmed_up_alert_telegram_sent
        return self._app_state.coffee_long_running_alert_sent or self._app_state.coffee_long_running_alert_telegram_sent

    def _iphone_channel_delivered(self, alert: str) -> bool:
        if alert == "warmed_up":
            return self._app_state.coffee_warmed_up_alert_sent or self._app_state.coffee_warmed_up_alert_iphone_sent
        return self._app_state.coffee_long_running_alert_sent or self._app_state.coffee_long_running_alert_iphone_sent

    async def _mark_telegram_channel_delivered(self, alert: str) -> bool:
        if alert == "warmed_up":
            return await self._app_state.mark_coffee_warmed_up_alert_telegram_sent()
        return await self._app_state.mark_coffee_long_running_alert_telegram_sent()

    async def _mark_iphone_channel_delivered(self, alert: str) -> bool:
        if alert == "warmed_up":
            return await self._app_state.mark_coffee_warmed_up_alert_iphone_sent()
        return await self._app_state.mark_coffee_long_running_alert_iphone_sent()

    def _alert_text(self, alert: str) -> str:
        if alert == "warmed_up":
            return coffee_messages.coffee_warmed_up()
        return coffee_messages.coffee_warning_long_running_text(_runtime_text(self._app_state.coffee_on_since))

    def _push_text(self, alert: str) -> tuple[str, str]:
        if alert == "warmed_up":
            return "\u041a\u043e\u0444\u0435\u043c\u0430\u0448\u0438\u043d\u0430", "\u2615 \u041a\u043e\u0444\u0435\u043c\u0430\u0448\u0438\u043d\u0430 \u0440\u0430\u0437\u043e\u0433\u0440\u0435\u0442\u0430"
        return "\u041a\u043e\u0444\u0435\u043c\u0430\u0448\u0438\u043d\u0430", "\u26a0\ufe0f \u041a\u043e\u0444\u0435\u043c\u0430\u0448\u0438\u043d\u0430 \u0440\u0430\u0431\u043e\u0442\u0430\u0435\u0442 \u0441\u043b\u0438\u0448\u043a\u043e\u043c \u0434\u043e\u043b\u0433\u043e"

    def _warmup_gif_data(self, alert: str) -> dict[str, str]:
        if alert != "warmed_up":
            return {}
        if not self._settings.coffee_warmup_gif_url:
            LOGGER.info("Coffee warmup gif skipped because COFFEE_WARMUP_GIF_URL is not set")
            return {}
        return {"image": self._settings.coffee_warmup_gif_url}

    async def _send_mobile_alert(self, alert: str, *, title: str, message: str, attempt: int) -> bool:
        services = self._settings.ha_mobile_notify_services
        if not services:
            LOGGER.warning("Coffee alert iPhone push skipped, HA mobile notify services are not configured: alert=%s", alert)
            return False

        sent = False
        for service in services:
            LOGGER.info("Coffee alert send attempt: alert=%s service=%s attempt=%s", alert, service, attempt)
            try:
                await self._ha.notify(
                    service,
                    title=title,
                    message=message,
                    data={
                        "tag": "coffee_machine_alert",
                        **self._warmup_gif_data(alert),
                        "actions": [
                            {
                                "action": "COFFEE_TURN_OFF",
                                "title": "Выключить",
                                "destructive": True,
                            }
                        ],
                    },
                )
            except HomeAssistantError as exc:
                LOGGER.exception("Coffee alert failed: alert=%s service=%s error=%r", alert, service, exc)
            else:
                sent = True
                LOGGER.info("Coffee alert sent: alert=%s service=%s", alert, service)
        return sent


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
