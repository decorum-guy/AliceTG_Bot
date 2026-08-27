from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable, Iterable, Literal

from app.planning.audit import AuditWriter
from app.planning.db import PlanningDatabase
from app.planning.errors import PlanningValidationError, PlanningVersionConflictError
from app.planning.models import MutationContext, utc_now


SpokenEndpoint = Literal["alice", "jarvis"]
PhoneChannel = Literal["telegram", "home_assistant"]

SPOKEN_ENDPOINTS: tuple[SpokenEndpoint, ...] = ("alice", "jarvis")
PHONE_CHANNELS: tuple[PhoneChannel, ...] = ("telegram", "home_assistant")
DEFAULT_SPOKEN_ENDPOINT: SpokenEndpoint = "alice"
DEFAULT_PHONE_CHANNELS: tuple[PhoneChannel, ...] = ("telegram",)


def _validate_spoken_endpoint(value: str) -> SpokenEndpoint:
    if value not in SPOKEN_ENDPOINTS:
        raise PlanningValidationError("reminder delivery spoken endpoint is invalid")
    return value  # type: ignore[return-value]


def normalize_phone_channels(values: Iterable[str]) -> tuple[PhoneChannel, ...]:
    values_list = list(values)
    unique = set(values_list)
    if len(unique) != len(values_list):
        raise PlanningValidationError("reminder delivery phone channels must be unique")
    if not unique or not unique.issubset(PHONE_CHANNELS):
        raise PlanningValidationError("reminder delivery phone channels must contain Telegram or Home Assistant")
    return tuple(channel for channel in PHONE_CHANNELS if channel in unique)  # type: ignore[misc]


@dataclass(frozen=True)
class ReminderDeliveryPreferences:
    spoken_endpoint: SpokenEndpoint = DEFAULT_SPOKEN_ENDPOINT
    phone_channels: tuple[PhoneChannel, ...] = DEFAULT_PHONE_CHANNELS
    revision: int = 0
    updated_at: str = ""

    def __post_init__(self) -> None:
        _validate_spoken_endpoint(self.spoken_endpoint)
        normalized = normalize_phone_channels(self.phone_channels)
        if normalized != self.phone_channels:
            raise PlanningValidationError("reminder delivery phone channels must be unique and canonical")
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 0:
            raise PlanningValidationError("reminder delivery revision is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "schemaVersion": "reminder.delivery-settings.v1",
            "revision": self.revision,
            "updatedAt": self.updated_at,
            "spokenEndpoint": self.spoken_endpoint,
            "phoneChannels": list(self.phone_channels),
        }


def legacy_phone_channels(*, notify_telegram_enabled: bool, notify_iphone_enabled: bool) -> tuple[PhoneChannel, ...]:
    selected: list[str] = []
    if notify_telegram_enabled:
        selected.append("telegram")
    if notify_iphone_enabled:
        selected.append("home_assistant")
    # The new contract intentionally has no "none" state.  Existing legacy
    # configurations with both switches off receive the safe Telegram default.
    return normalize_phone_channels(selected or ["telegram"])


class ReminderDeliveryPreferencesStore:
    """Canonical owner policy persisted in the Planning SQLite database."""

    def __init__(
        self,
        database: PlanningDatabase,
        *,
        now_fn: Callable[[], str] = utc_now,
        audit: AuditWriter | None = None,
    ) -> None:
        self.database = database
        self._now_fn = now_fn
        self.audit = audit or AuditWriter(database, now_fn=now_fn)

    def get(self) -> ReminderDeliveryPreferences:
        row = self.database.connection.execute(
            "SELECT spoken_endpoint, phone_channels_json, revision, updated_at "
            "FROM reminder_delivery_preferences WHERE id = 1"
        ).fetchone()
        if row is None:
            return ReminderDeliveryPreferences(updated_at="")
        try:
            channels = json.loads(str(row["phone_channels_json"]))
        except json.JSONDecodeError as exc:
            raise PlanningValidationError("stored reminder delivery phone channels are invalid") from exc
        if not isinstance(channels, list) or any(not isinstance(item, str) for item in channels):
            raise PlanningValidationError("stored reminder delivery phone channels are invalid")
        return ReminderDeliveryPreferences(
            spoken_endpoint=_validate_spoken_endpoint(str(row["spoken_endpoint"])),
            phone_channels=normalize_phone_channels(channels),
            revision=int(row["revision"]),
            updated_at=str(row["updated_at"]),
        )

    def ensure_from_legacy(self, *, spoken_endpoint: str, notify_telegram_enabled: bool, notify_iphone_enabled: bool) -> ReminderDeliveryPreferences:
        existing = self.get()
        if existing.updated_at:
            return existing
        endpoint = _validate_spoken_endpoint(spoken_endpoint)
        channels = legacy_phone_channels(
            notify_telegram_enabled=notify_telegram_enabled,
            notify_iphone_enabled=notify_iphone_enabled,
        )
        timestamp = self._now_fn()
        preferences = ReminderDeliveryPreferences(
            spoken_endpoint=endpoint,
            phone_channels=channels,
            revision=0,
            updated_at=timestamp,
        )
        with self.database.transaction():
            self.database.connection.execute(
                """
                INSERT OR IGNORE INTO reminder_delivery_preferences(
                    id, spoken_endpoint, phone_channels_json, revision, updated_at
                ) VALUES (1, ?, ?, ?, ?)
                """,
                (preferences.spoken_endpoint, json.dumps(list(preferences.phone_channels)), preferences.revision, timestamp),
            )
        return self.get()

    def update(
        self,
        *,
        expected_revision: int,
        spoken_endpoint: str,
        phone_channels: Iterable[str],
        context: MutationContext,
    ) -> ReminderDeliveryPreferences:
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
            raise PlanningValidationError("reminder delivery expected revision is invalid")
        context = context.validate()
        endpoint = _validate_spoken_endpoint(spoken_endpoint)
        channels = normalize_phone_channels(phone_channels)
        timestamp = self._now_fn()
        with self.database.transaction():
            current = self.get()
            if current.revision != expected_revision:
                raise PlanningVersionConflictError("reminder_delivery_preferences", "1", expected_revision, current.revision)
            updated = ReminderDeliveryPreferences(
                spoken_endpoint=endpoint,
                phone_channels=channels,
                revision=current.revision + 1,
                updated_at=timestamp,
            )
            cursor = self.database.connection.execute(
                """
                INSERT INTO reminder_delivery_preferences(
                    id, spoken_endpoint, phone_channels_json, revision, updated_at
                ) VALUES (1, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    spoken_endpoint = excluded.spoken_endpoint,
                    phone_channels_json = excluded.phone_channels_json,
                    revision = excluded.revision,
                    updated_at = excluded.updated_at
                WHERE reminder_delivery_preferences.revision = ?
                """,
                (
                    updated.spoken_endpoint,
                    json.dumps(list(updated.phone_channels), separators=(",", ":")),
                    updated.revision,
                    updated.updated_at,
                    expected_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise PlanningVersionConflictError("reminder_delivery_preferences", "1", expected_revision, self.get().revision)
            self.audit.record(
                context=context,
                action="update",
                object_domain="reminder_delivery_preferences",
                object_id=_stable_object_id(),
                old_version=max(1, current.revision),
                new_version=max(1, updated.revision),
                before=current.to_dict(),
                after=updated.to_dict(),
                correlation_id=context.correlation_id,
            )
            return updated


def _stable_object_id() -> str:
    # Audit events require UUID object ids.  The singleton row itself has no
    # UUID, so use a fixed v4-shaped identifier for the policy domain.
    return "00000000-0000-4000-8000-000000000001"
