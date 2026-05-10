from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)
DEFAULT_COFFEE_WARMED_UP_ALERT_DELAY_SECONDS = 13 * 60
DEFAULT_COFFEE_LONG_RUNNING_ALERT_DELAY_SECONDS = 60 * 60


class AppStateStore:
    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._lock = asyncio.Lock()
        self._state: dict[str, Any] = {}
        self._load()

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

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_suffix(f"{self._path.suffix}.tmp")
        tmp_path.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(self._path)
