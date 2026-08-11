from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

LOGGER = logging.getLogger(__name__)

ReminderStatus = Literal["pending", "fired", "cancelled"]
ReminderSource = Literal["alice", "telegram"]
DEFAULT_REMINDER_VOICE_STATION = "media_player.stantsiia_mini_zal"


@dataclass
class ReminderRecord:
    id: str
    text: str
    due_at: str
    delay_seconds: int
    source: ReminderSource
    created_at: str
    status: ReminderStatus = "pending"
    chat_id: int | None = None
    fired_at: str | None = None
    cancelled_at: str | None = None

    @property
    def due_datetime(self) -> datetime:
        return datetime.fromisoformat(self.due_at)


@dataclass
class ReminderSettings:
    voice_enabled: bool = True
    voice_station_entity_id: str = DEFAULT_REMINDER_VOICE_STATION
    notify_telegram_enabled: bool = True
    notify_iphone_enabled: bool = False


class ReminderSettingsStore:
    """Legacy settings-only boundary used while reminder records move to Planning."""

    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._lock = asyncio.Lock()

    async def get_settings(self) -> ReminderSettings:
        async with self._lock:
            return self._settings_from_document(self._read_document())

    async def update_settings(
        self,
        *,
        voice_enabled: bool | None = None,
        voice_station_entity_id: str | None = None,
        notify_telegram_enabled: bool | None = None,
        notify_iphone_enabled: bool | None = None,
    ) -> ReminderSettings:
        async with self._lock:
            document = self._read_document()
            settings = self._settings_from_document(document)
            if voice_enabled is not None:
                settings.voice_enabled = voice_enabled
            if voice_station_entity_id is not None:
                settings.voice_station_entity_id = voice_station_entity_id
            if notify_telegram_enabled is not None:
                settings.notify_telegram_enabled = notify_telegram_enabled
            if notify_iphone_enabled is not None:
                settings.notify_iphone_enabled = notify_iphone_enabled
            document["settings"] = asdict(settings)
            await asyncio.to_thread(self._save_document, document)
            return ReminderSettings(**asdict(settings))

    def _read_document(self) -> dict[str, object]:
        if not self._path.exists():
            return {"settings": asdict(ReminderSettings()), "reminders": []}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("Reminder settings storage could not be read") from exc
        if not isinstance(data, dict):
            raise RuntimeError("Reminder settings storage has an invalid top-level shape")
        return data

    @staticmethod
    def _settings_from_document(document: dict[str, object]) -> ReminderSettings:
        settings = document.get("settings")
        if not isinstance(settings, dict):
            return ReminderSettings()
        return ReminderSettings(
            voice_enabled=bool(settings.get("voice_enabled", True)),
            voice_station_entity_id=str(
                settings.get("voice_station_entity_id") or DEFAULT_REMINDER_VOICE_STATION
            ),
            notify_telegram_enabled=bool(settings.get("notify_telegram_enabled", True)),
            notify_iphone_enabled=bool(settings.get("notify_iphone_enabled", False)),
        )

    def _save_document(self, document: dict[str, object]) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._path.with_suffix(f"{self._path.suffix}.tmp")
            tmp_path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp_path.replace(self._path)
        except OSError as exc:
            raise RuntimeError("Reminder settings storage could not be saved") from exc


class ReminderStore:
    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._lock = asyncio.Lock()
        self._reminders: dict[str, ReminderRecord] = {}
        self._settings = ReminderSettings()
        self._load()
        LOGGER.info(
            "Reminder voice settings loaded: voice_enabled=%s voice_station_entity_id=%s",
            self._settings.voice_enabled,
            self._settings.voice_station_entity_id,
        )

    async def create(
        self,
        *,
        text: str,
        due_at: datetime,
        delay_seconds: int,
        source: ReminderSource,
        chat_id: int,
    ) -> ReminderRecord:
        now = datetime.now(timezone.utc).isoformat()
        reminder = ReminderRecord(
            id=uuid.uuid4().hex[:12],
            text=text,
            due_at=due_at.isoformat(),
            delay_seconds=delay_seconds,
            source=source,
            created_at=now,
            chat_id=chat_id,
        )
        async with self._lock:
            self._reminders[reminder.id] = reminder
            await asyncio.to_thread(self._save)
        return reminder

    async def list_pending(self) -> list[ReminderRecord]:
        async with self._lock:
            return sorted(
                [reminder for reminder in self._reminders.values() if reminder.status == "pending"],
                key=lambda reminder: reminder.due_at,
            )

    async def get(self, reminder_id: str) -> ReminderRecord | None:
        async with self._lock:
            return self._reminders.get(reminder_id)

    async def mark_fired(self, reminder_id: str) -> None:
        async with self._lock:
            reminder = self._reminders.get(reminder_id)
            if reminder is None:
                return
            reminder.status = "fired"
            reminder.fired_at = datetime.now(timezone.utc).isoformat()
            await asyncio.to_thread(self._save)

    async def cancel(self, reminder_id: str) -> bool:
        async with self._lock:
            reminder = self._reminders.get(reminder_id)
            if reminder is None or reminder.status != "pending":
                return False
            reminder.status = "cancelled"
            reminder.cancelled_at = datetime.now(timezone.utc).isoformat()
            await asyncio.to_thread(self._save)
            return True

    async def get_settings(self) -> ReminderSettings:
        async with self._lock:
            return ReminderSettings(
                voice_enabled=self._settings.voice_enabled,
                voice_station_entity_id=self._settings.voice_station_entity_id,
                notify_telegram_enabled=self._settings.notify_telegram_enabled,
                notify_iphone_enabled=self._settings.notify_iphone_enabled,
            )

    async def update_settings(
        self,
        *,
        voice_enabled: bool | None = None,
        voice_station_entity_id: str | None = None,
        notify_telegram_enabled: bool | None = None,
        notify_iphone_enabled: bool | None = None,
    ) -> ReminderSettings:
        async with self._lock:
            if voice_enabled is not None:
                self._settings.voice_enabled = voice_enabled
            if voice_station_entity_id is not None:
                self._settings.voice_station_entity_id = voice_station_entity_id
            if notify_telegram_enabled is not None:
                self._settings.notify_telegram_enabled = notify_telegram_enabled
            if notify_iphone_enabled is not None:
                self._settings.notify_iphone_enabled = notify_iphone_enabled
            await asyncio.to_thread(self._save)
            LOGGER.info(
                "Reminder voice settings updated: voice_enabled=%s voice_station_entity_id=%s notify_telegram_enabled=%s notify_iphone_enabled=%s",
                self._settings.voice_enabled,
                self._settings.voice_station_entity_id,
                self._settings.notify_telegram_enabled,
                self._settings.notify_iphone_enabled,
            )
            return ReminderSettings(
                voice_enabled=self._settings.voice_enabled,
                voice_station_entity_id=self._settings.voice_station_entity_id,
                notify_telegram_enabled=self._settings.notify_telegram_enabled,
                notify_iphone_enabled=self._settings.notify_iphone_enabled,
            )

    def _load(self) -> None:
        if not self._path.exists():
            LOGGER.info("Reminder storage loaded: path=%s total=0 pending=0 missing=true", self._path)
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            LOGGER.exception("Reminder storage load failed: path=%s", self._path)
            return

        reminders = data.get("reminders") if isinstance(data, dict) else None
        if isinstance(reminders, list):
            for item in reminders:
                if not isinstance(item, dict):
                    continue
                try:
                    reminder = ReminderRecord(**item)
                except TypeError:
                    LOGGER.warning("Reminder storage skipped invalid record: item=%s", item)
                    continue
                self._reminders[reminder.id] = reminder

        settings = data.get("settings") if isinstance(data, dict) else None
        if isinstance(settings, dict):
            self._settings = ReminderSettings(
                voice_enabled=bool(settings.get("voice_enabled", True)),
                voice_station_entity_id=str(settings.get("voice_station_entity_id") or DEFAULT_REMINDER_VOICE_STATION),
                notify_telegram_enabled=bool(settings.get("notify_telegram_enabled", True)),
                notify_iphone_enabled=bool(settings.get("notify_iphone_enabled", False)),
            )
        pending_count = sum(1 for reminder in self._reminders.values() if reminder.status == "pending")
        LOGGER.info(
            "Reminder storage loaded: path=%s total=%s pending=%s",
            self._path,
            len(self._reminders),
            pending_count,
        )

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._path.with_suffix(f"{self._path.suffix}.tmp")
            data = {
                "settings": asdict(self._settings),
                "reminders": [asdict(reminder) for reminder in self._reminders.values()],
            }
            tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp_path.replace(self._path)
            LOGGER.info("Reminder saved to storage: path=%s", self._path)
        except OSError:
            LOGGER.exception("Reminder storage save failed: path=%s", self._path)
            raise
