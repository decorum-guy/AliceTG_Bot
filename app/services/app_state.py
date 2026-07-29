from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)
DEFAULT_COFFEE_WARMED_UP_ALERT_DELAY_SECONDS = 13 * 60
DEFAULT_COFFEE_LONG_RUNNING_ALERT_DELAY_SECONDS = 60 * 60


class AppStateRevisionConflict(RuntimeError):
    pass


class AppStatePersistenceError(RuntimeError):
    """Sanitized durable-state persistence failure."""


class AppStateStore:
    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._lock = asyncio.Lock()
        self._state: dict[str, Any] = {}
        self._load()
        self._initialize_notification_metadata_in_memory()

    @property
    def coffee_warmed_up_alert_enabled(self) -> bool:
        return bool(self._state.get("coffee_warmed_up_alert_enabled", self._legacy_coffee_alerts_enabled()))

    @property
    def coffee_long_running_alert_enabled(self) -> bool:
        return bool(self._state.get("coffee_long_running_alert_enabled", self._legacy_coffee_alerts_enabled()))

    @property
    def coffee_warmed_up_alert_delay_seconds(self) -> int:
        return int(self._state.get("coffee_warmed_up_alert_delay_seconds", DEFAULT_COFFEE_WARMED_UP_ALERT_DELAY_SECONDS))

    @property
    def coffee_long_running_alert_delay_seconds(self) -> int:
        return int(self._state.get("coffee_long_running_alert_delay_seconds", DEFAULT_COFFEE_LONG_RUNNING_ALERT_DELAY_SECONDS))

    @property
    def legacy_coffee_warmup_seconds(self) -> int:
        return self.coffee_warmed_up_alert_delay_seconds

    @property
    def legacy_coffee_long_running_seconds(self) -> int:
        return self.coffee_long_running_alert_delay_seconds

    @property
    def coffee_timing_migrated_to_ha(self) -> bool:
        return bool(self._state.get("coffee_timing_migrated_to_ha", False))

    @property
    def explicit_legacy_coffee_timing(self) -> tuple[int, int] | None:
        warmup = self._state.get("coffee_warmed_up_alert_delay_seconds")
        long_running = self._state.get("coffee_long_running_alert_delay_seconds")
        if warmup is None and long_running is None:
            return None
        return (
            int(warmup or DEFAULT_COFFEE_WARMED_UP_ALERT_DELAY_SECONDS),
            int(long_running or DEFAULT_COFFEE_LONG_RUNNING_ALERT_DELAY_SECONDS),
        )

    @property
    def coffee_warmed_up_notify_telegram(self) -> bool:
        return bool(self._state.get("coffee_warmed_up_notify_telegram", True))

    @property
    def coffee_warmed_up_notify_iphone(self) -> bool:
        return bool(self._state.get("coffee_warmed_up_notify_iphone", True))

    @property
    def coffee_long_running_notify_telegram(self) -> bool:
        return bool(self._state.get("coffee_long_running_notify_telegram", True))

    @property
    def coffee_long_running_notify_iphone(self) -> bool:
        return bool(self._state.get("coffee_long_running_notify_iphone", True))

    @property
    def coffee_pushward_show_seconds(self) -> bool:
        return bool(self._state.get("coffee_pushward_show_seconds", True))

    async def set_coffee_warmed_up_alert_enabled(self, enabled: bool) -> None:
        async with self._lock:
            self._state["coffee_warmed_up_alert_enabled"] = enabled
            await asyncio.to_thread(self._save)

    async def set_coffee_long_running_alert_enabled(self, enabled: bool) -> None:
        async with self._lock:
            self._state["coffee_long_running_alert_enabled"] = enabled
            await asyncio.to_thread(self._save)

    async def set_coffee_warmed_up_alert_delay_seconds(self, delay_seconds: int) -> None:
        async with self._lock:
            self._state["coffee_warmed_up_alert_delay_seconds"] = delay_seconds
            await asyncio.to_thread(self._save)

    async def set_coffee_long_running_alert_delay_seconds(self, delay_seconds: int) -> None:
        async with self._lock:
            self._state["coffee_long_running_alert_delay_seconds"] = delay_seconds
            await asyncio.to_thread(self._save)

    async def mark_coffee_timing_migrated_to_ha(self) -> None:
        async with self._lock:
            self._state["coffee_timing_migrated_to_ha"] = True
            await asyncio.to_thread(self._save)

    async def set_coffee_warmed_up_notify_telegram(self, enabled: bool) -> None:
        async with self._lock:
            self._state["coffee_warmed_up_notify_telegram"] = enabled
            await asyncio.to_thread(self._save)

    async def set_coffee_warmed_up_notify_iphone(self, enabled: bool) -> None:
        async with self._lock:
            self._state["coffee_warmed_up_notify_iphone"] = enabled
            await asyncio.to_thread(self._save)

    async def set_coffee_long_running_notify_telegram(self, enabled: bool) -> None:
        async with self._lock:
            self._state["coffee_long_running_notify_telegram"] = enabled
            await asyncio.to_thread(self._save)

    async def set_coffee_long_running_notify_iphone(self, enabled: bool) -> None:
        async with self._lock:
            self._state["coffee_long_running_notify_iphone"] = enabled
            await asyncio.to_thread(self._save)

    async def set_coffee_pushward_show_seconds(self, enabled: bool) -> None:
        async with self._lock:
            self._state["coffee_pushward_show_seconds"] = enabled
            await asyncio.to_thread(self._save)

    def coffee_notification_settings(self) -> dict[str, object]:
        return {
            "warmup": {
                "enabled": self.coffee_warmed_up_alert_enabled,
                "channels": {
                    "telegram": self.coffee_warmed_up_notify_telegram,
                    "iphone": self.coffee_warmed_up_notify_iphone,
                },
            },
            "longRunning": {
                "enabled": self.coffee_long_running_alert_enabled,
                "channels": {
                    "telegram": self.coffee_long_running_notify_telegram,
                    "iphone": self.coffee_long_running_notify_iphone,
                },
            },
        }

    def coffee_notification_revision(self) -> str:
        return str(self._state["coffee_notification_settings_revision"])

    @property
    def coffee_notification_settings_updated_at(self) -> str:
        return str(self._state["coffee_notification_settings_updated_at"])

    async def ensure_coffee_notification_metadata(self) -> None:
        """Persist lazily-created metadata without changing effective settings."""

        async with self._lock:
            if self._path.exists() and self._notification_metadata_is_persisted():
                return
            candidate = dict(self._state)
            await asyncio.to_thread(self._persist_candidate, candidate)
            self._state = candidate

    async def patch_coffee_notification_settings(
        self,
        *,
        expected_revision: str,
        values: dict[str, bool],
    ) -> tuple[dict[str, object], str, bool]:
        key_map = {
            "warmup.enabled": "coffee_warmed_up_alert_enabled",
            "warmup.channels.telegram": "coffee_warmed_up_notify_telegram",
            "warmup.channels.iphone": "coffee_warmed_up_notify_iphone",
            "longRunning.enabled": "coffee_long_running_alert_enabled",
            "longRunning.channels.telegram": "coffee_long_running_notify_telegram",
            "longRunning.channels.iphone": "coffee_long_running_notify_iphone",
        }
        async with self._lock:
            if not hmac_compare(expected_revision, self.coffee_notification_revision()):
                raise AppStateRevisionConflict("Notification settings revision is stale")
            candidate = dict(self._state)
            changed = False
            for path, value in values.items():
                state_key = key_map[path]
                current = bool(candidate.get(state_key, getattr(self, state_key)))
                if current != value:
                    candidate[state_key] = value
                    changed = True
            if changed:
                candidate["coffee_notification_settings_revision"] = _new_revision()
                candidate["coffee_notification_settings_updated_at"] = _now()
                await asyncio.to_thread(self._persist_candidate, candidate)
                self._state = candidate
            return (
                self.coffee_notification_settings(),
                self.coffee_notification_revision(),
                changed,
            )

    @property
    def coffee_machine_state(self) -> str:
        return str(self._state.get("coffee_machine_state") or "off")

    @property
    def coffee_on_since(self) -> str | None:
        value = self._state.get("coffee_on_since")
        return str(value) if value else None

    @property
    def coffee_warmed_up_alert_sent(self) -> bool:
        return bool(self._state.get("coffee_warmed_up_alert_sent", False))

    @property
    def coffee_long_running_alert_sent(self) -> bool:
        return bool(self._state.get("coffee_long_running_alert_sent", False))

    @property
    def coffee_warmed_up_alert_telegram_sent(self) -> bool:
        return bool(self._state.get("coffee_warmed_up_alert_telegram_sent", False))

    @property
    def coffee_warmed_up_alert_iphone_sent(self) -> bool:
        return bool(self._state.get("coffee_warmed_up_alert_iphone_sent", False))

    @property
    def coffee_long_running_alert_telegram_sent(self) -> bool:
        return bool(self._state.get("coffee_long_running_alert_telegram_sent", False))

    @property
    def coffee_long_running_alert_iphone_sent(self) -> bool:
        return bool(self._state.get("coffee_long_running_alert_iphone_sent", False))

    async def mark_coffee_machine_on(self, on_since: str) -> None:
        async with self._lock:
            self._state["coffee_machine_state"] = "on"
            self._state["coffee_on_since"] = on_since
            self._state["coffee_warmed_up_alert_sent"] = False
            self._state["coffee_long_running_alert_sent"] = False
            self._state["coffee_warmed_up_alert_telegram_sent"] = False
            self._state["coffee_warmed_up_alert_iphone_sent"] = False
            self._state["coffee_long_running_alert_telegram_sent"] = False
            self._state["coffee_long_running_alert_iphone_sent"] = False
            await asyncio.to_thread(self._save)

    async def mark_coffee_machine_off(self) -> None:
        async with self._lock:
            self._state["coffee_machine_state"] = "off"
            self._state["coffee_on_since"] = None
            self._state["coffee_warmed_up_alert_sent"] = False
            self._state["coffee_long_running_alert_sent"] = False
            self._state["coffee_warmed_up_alert_telegram_sent"] = False
            self._state["coffee_warmed_up_alert_iphone_sent"] = False
            self._state["coffee_long_running_alert_telegram_sent"] = False
            self._state["coffee_long_running_alert_iphone_sent"] = False
            await asyncio.to_thread(self._save)

    async def mark_coffee_warmed_up_alert_sent(self) -> bool:
        async with self._lock:
            if self.coffee_machine_state != "on" or self.coffee_warmed_up_alert_sent:
                return False
            self._state["coffee_warmed_up_alert_sent"] = True
            await asyncio.to_thread(self._save)
            return True

    async def mark_coffee_long_running_alert_sent(self) -> bool:
        async with self._lock:
            if self.coffee_machine_state != "on" or self.coffee_long_running_alert_sent:
                return False
            self._state["coffee_long_running_alert_sent"] = True
            await asyncio.to_thread(self._save)
            return True

    async def mark_coffee_warmed_up_alert_telegram_sent(self) -> bool:
        async with self._lock:
            if self.coffee_machine_state != "on" or self.coffee_warmed_up_alert_telegram_sent:
                return False
            self._state["coffee_warmed_up_alert_telegram_sent"] = True
            await asyncio.to_thread(self._save)
            return True

    async def mark_coffee_warmed_up_alert_iphone_sent(self) -> bool:
        async with self._lock:
            if self.coffee_machine_state != "on" or self.coffee_warmed_up_alert_iphone_sent:
                return False
            self._state["coffee_warmed_up_alert_iphone_sent"] = True
            await asyncio.to_thread(self._save)
            return True

    async def mark_coffee_long_running_alert_telegram_sent(self) -> bool:
        async with self._lock:
            if self.coffee_machine_state != "on" or self.coffee_long_running_alert_telegram_sent:
                return False
            self._state["coffee_long_running_alert_telegram_sent"] = True
            await asyncio.to_thread(self._save)
            return True

    async def mark_coffee_long_running_alert_iphone_sent(self) -> bool:
        async with self._lock:
            if self.coffee_machine_state != "on" or self.coffee_long_running_alert_iphone_sent:
                return False
            self._state["coffee_long_running_alert_iphone_sent"] = True
            await asyncio.to_thread(self._save)
            return True

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            LOGGER.exception("Cannot read app state from %s", self._path)
            return
        if isinstance(data, dict):
            self._state.update(data)

    def _legacy_coffee_alerts_enabled(self) -> bool:
        return bool(self._state.get("coffee_alerts_enabled", True))

    def _initialize_notification_metadata_in_memory(self) -> None:
        if not isinstance(self._state.get("coffee_notification_settings_revision"), str):
            self._state["coffee_notification_settings_revision"] = _new_revision()
        if not isinstance(self._state.get("coffee_notification_settings_updated_at"), str):
            self._state["coffee_notification_settings_updated_at"] = _now()

    def _notification_metadata_is_persisted(self) -> bool:
        try:
            persisted = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return (
            isinstance(persisted, dict)
            and persisted.get("coffee_notification_settings_revision")
            == self.coffee_notification_revision()
            and persisted.get("coffee_notification_settings_updated_at")
            == self.coffee_notification_settings_updated_at
        )

    def _save(self) -> None:
        self._persist_candidate(dict(self._state))

    def _persist_candidate(self, candidate: dict[str, Any]) -> None:
        """Durably replace the state file before exposing candidate in memory."""

        temp_path: Path | None = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            serialized = json.dumps(candidate, ensure_ascii=False, indent=2)
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self._path.parent,
                prefix=f".{self._path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temp_path = Path(handle.name)
                handle.write(serialized)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self._path)
            temp_path = None
            _fsync_directory_best_effort(self._path.parent)
        except (OSError, TypeError, ValueError) as exc:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise AppStatePersistenceError("Application state could not be persisted") from exc


def hmac_compare(left: str, right: str) -> bool:
    import hmac

    return hmac.compare_digest(left, right)


def _new_revision() -> str:
    return secrets.token_hex(16)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _fsync_directory_best_effort(path: Path) -> None:
    flags = getattr(os, "O_DIRECTORY", 0) | os.O_RDONLY
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)
