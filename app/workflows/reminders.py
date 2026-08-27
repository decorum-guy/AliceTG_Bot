from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.config import Settings
from app.keyboards.coffee import delete_only
from app.messages import reminders as reminder_messages
from app.services.reminder_parser import ParsedReminder, parse_delay_only, parse_reminder_request
from app.services.reminder_store import ReminderRecord, ReminderSettings, ReminderSource, ReminderStore
from app.services.home_assistant import HomeAssistantClient, HomeAssistantError
from app.services.telegram_messages import TelegramMessages
from app.planning.delivery_settings import ReminderDeliveryPreferences, legacy_phone_channels

LOGGER = logging.getLogger(__name__)

REMINDER_VOICE_STATIONS = {
    "zal": ("Зал", "media_player.stantsiia_mini_zal"),
    "spalnia": ("Спальня", "media_player.stantsiia_mini_spalnia"),
}


@dataclass
class ReminderDraft:
    text: str | None = None
    step: str = "text"


class ReminderWorkflow:
    def __init__(
        self,
        store: ReminderStore,
        telegram_messages: TelegramMessages,
        admin_chat_id: int,
        settings: Settings,
        ha: HomeAssistantClient,
        durable_scheduler_enabled: bool = False,
    ) -> None:
        self._store = store
        self._telegram_messages = telegram_messages
        self._admin_chat_id = admin_chat_id
        self._settings = settings
        self._ha = ha
        self._durable_scheduler_enabled = durable_scheduler_enabled
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._drafts: dict[int, ReminderDraft] = {}

    async def restore_pending(self) -> None:
        if self._durable_scheduler_enabled:
            return
        reminders = await self._store.list_pending()
        LOGGER.info("Reminder restore started: pending_count=%s", len(reminders))
        for reminder in reminders:
            self._schedule(reminder)

    async def create_from_text(
        self,
        text: str,
        *,
        source: ReminderSource,
        chat_id: int | None = None,
    ) -> tuple[ReminderRecord, ParsedReminder] | None:
        LOGGER.info("Reminder create request received: source=%s", source)
        parsed = parse_reminder_request(text)
        if parsed is None:
            LOGGER.warning("Reminder parse failed: source=%s", source)
            return None
        reminder = await self._create(parsed, source=source, chat_id=chat_id or self._admin_chat_id)
        return reminder, parsed

    async def create_from_parts(
        self,
        text: str,
        delay_text: str,
        *,
        source: ReminderSource,
        chat_id: int | None = None,
    ) -> tuple[ReminderRecord, str] | None:
        LOGGER.info("Reminder create request received: source=%s parts=true", source)
        parsed_delay = parse_delay_only(delay_text)
        if parsed_delay is None:
            LOGGER.warning("Reminder delay parse failed: source=%s", source)
            return None
        delay_seconds, human_delay_text = parsed_delay
        parsed = ParsedReminder(text=text.strip(), delay_seconds=delay_seconds, human_delay_text=human_delay_text)
        if not parsed.text:
            LOGGER.warning("Reminder text parse failed: source=%s", source)
            return None
        reminder = await self._create(parsed, source=source, chat_id=chat_id or self._admin_chat_id)
        return reminder, human_delay_text

    async def list_pending(self) -> list[ReminderRecord]:
        return await self._store.list_pending()

    async def cancel(self, reminder_id: str) -> bool:
        cancelled = await self._store.cancel(reminder_id)
        task = self._tasks.pop(reminder_id, None)
        if task is not None:
            task.cancel()
        if cancelled:
            LOGGER.info("Reminder cancelled: id=%s", reminder_id)
        return cancelled

    async def get_settings(self) -> ReminderSettings:
        return await self._store.get_settings()

    async def get_delivery_preferences(self) -> ReminderDeliveryPreferences:
        getter = getattr(self._store, "get_delivery_preferences", None)
        if callable(getter):
            return await getter()
        settings = await self._store.get_settings()
        return ReminderDeliveryPreferences(
            spoken_endpoint=settings.spoken_endpoint,  # type: ignore[arg-type]
            phone_channels=(
                tuple(settings.phone_channels)
                if settings.phone_channels is not None
                else legacy_phone_channels(
                    notify_telegram_enabled=settings.notify_telegram_enabled,
                    notify_iphone_enabled=settings.notify_iphone_enabled,
                )
            ),
            revision=settings.delivery_revision,
            updated_at=settings.delivery_updated_at,
        )

    async def update_delivery_preferences(
        self,
        *,
        expected_revision: int,
        spoken_endpoint: str,
        phone_channels: tuple[str, ...],
    ) -> ReminderDeliveryPreferences:
        updater = getattr(self._store, "update_delivery_preferences", None)
        if not callable(updater):
            raise RuntimeError("reminder delivery preferences are unavailable")
        return await updater(
            expected_revision=expected_revision,
            spoken_endpoint=spoken_endpoint,
            phone_channels=phone_channels,
        )

    async def toggle_voice(self) -> ReminderSettings:
        settings = await self._store.get_settings()
        updated = await self._store.update_settings(voice_enabled=not settings.voice_enabled)
        if not updated.voice_enabled:
            LOGGER.info("Reminder voice announcement skipped because voice_enabled=false")
        return updated

    async def set_voice_station(self, station_key: str) -> ReminderSettings | None:
        station = REMINDER_VOICE_STATIONS.get(station_key)
        if station is None:
            return None
        _, entity_id = station
        return await self._store.update_settings(voice_station_entity_id=entity_id)

    async def toggle_voice_station(self) -> ReminderSettings:
        settings = await self._store.get_settings()
        next_entity_id = (
            "media_player.stantsiia_mini_spalnia"
            if settings.voice_station_entity_id == "media_player.stantsiia_mini_zal"
            else "media_player.stantsiia_mini_zal"
        )
        return await self._store.update_settings(voice_station_entity_id=next_entity_id)

    async def toggle_notification_channel(self, channel: str) -> ReminderSettings | None:
        settings = await self._store.get_settings()
        if channel == "telegram":
            return await self._store.update_settings(notify_telegram_enabled=not settings.notify_telegram_enabled)
        if channel == "iphone":
            return await self._store.update_settings(notify_iphone_enabled=not settings.notify_iphone_enabled)
        return None

    @staticmethod
    def station_label(entity_id: str) -> str:
        for label, station_entity_id in REMINDER_VOICE_STATIONS.values():
            if entity_id == station_entity_id:
                return label
        return entity_id

    def start_draft(self, user_id: int) -> None:
        self._drafts[user_id] = ReminderDraft()

    def get_draft(self, user_id: int | None) -> ReminderDraft | None:
        if user_id is None:
            return None
        return self._drafts.get(user_id)

    def set_draft_waiting_delay(self, user_id: int, text: str) -> None:
        self._drafts[user_id] = ReminderDraft(text=text, step="delay")

    def clear_draft(self, user_id: int) -> None:
        self._drafts.pop(user_id, None)

    async def _create(self, parsed: ParsedReminder, *, source: ReminderSource, chat_id: int) -> ReminderRecord:
        due_at = datetime.now(timezone.utc) + timedelta(seconds=parsed.delay_seconds)
        reminder = await self._store.create(
            text=parsed.text,
            due_at=due_at,
            delay_seconds=parsed.delay_seconds,
            source=source,
            chat_id=chat_id,
        )
        LOGGER.info(
            "Reminder scheduled: id=%s source=%s delay_seconds=%s due_at=%s text_len=%s",
            reminder.id,
            source,
            parsed.delay_seconds,
            reminder.due_at,
            len(parsed.text),
        )
        if not self._durable_scheduler_enabled:
            self._schedule(reminder)
        return reminder

    def _schedule(self, reminder: ReminderRecord) -> None:
        task = self._tasks.get(reminder.id)
        if task is not None and not task.done():
            return
        self._tasks[reminder.id] = asyncio.create_task(self._wait_and_fire(reminder.id))

    async def _wait_and_fire(self, reminder_id: str) -> None:
        try:
            reminder = await self._store.get(reminder_id)
            if reminder is None or reminder.status != "pending":
                return
            delay = (reminder.due_datetime - datetime.now(timezone.utc)).total_seconds()
            if delay > 0:
                await asyncio.sleep(delay)
            else:
                LOGGER.warning("Restored overdue reminder after restart: id=%s overdue_seconds=%s", reminder.id, int(abs(delay)))

            reminder = await self._store.get(reminder_id)
            if reminder is None or reminder.status != "pending":
                return
            LOGGER.info("Reminder fired: id=%s source=%s", reminder.id, reminder.source)
            settings = await self._store.get_settings()
            delivered = False
            attempted = False

            if settings.notify_telegram_enabled:
                attempted = True
                message_id = await self._telegram_messages.safe_send(
                    reminder.chat_id or self._admin_chat_id,
                    reminder_messages.reminder_notification(reminder.text),
                    reply_markup=delete_only(),
                )
                if message_id is None:
                    LOGGER.error("Reminder send Telegram failed: id=%s chat_id=%s", reminder.id, reminder.chat_id or self._admin_chat_id)
                else:
                    delivered = True
                    LOGGER.info("Reminder sent to Telegram: id=%s chat_id=%s", reminder.id, reminder.chat_id or self._admin_chat_id)

            if settings.notify_iphone_enabled or (
                settings.phone_channels is not None and "home_assistant" in settings.phone_channels
            ):
                attempted = True
                if not self._settings.ha_mobile_notify_services:
                    LOGGER.info("Reminder mobile push skipped, HA mobile notify services are not configured: id=%s", reminder.id)
                else:
                    for service in self._settings.ha_mobile_notify_services:
                        try:
                            await self._ha.notify(
                                service,
                                title="Напоминание",
                                message=reminder.text,
                            )
                        except HomeAssistantError as exc:
                            LOGGER.exception("Reminder mobile push failed id=%s service=%s reason=%r", reminder.id, service, exc)
                        else:
                            delivered = True
                            LOGGER.info(
                                "Reminder mobile push sent: id=%s service=%s",
                                reminder.id,
                                service,
                            )

            if not attempted:
                LOGGER.warning("Reminder fired but all notification channels are disabled: id=%s", reminder.id)
                await self._store.mark_fired(reminder.id)
                return
            if delivered:
                await self._store.mark_fired(reminder.id)
                return
            LOGGER.error("Reminder delivery failed through all enabled channels: id=%s", reminder.id)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("Reminder delivery failed: id=%s", reminder_id)
        finally:
            self._tasks.pop(reminder_id, None)
