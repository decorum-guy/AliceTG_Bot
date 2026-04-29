from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)


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

    async def set_coffee_warmed_up_alert_enabled(self, enabled: bool) -> None:
        async with self._lock:
            self._state["coffee_warmed_up_alert_enabled"] = enabled
            await asyncio.to_thread(self._save)

    async def set_coffee_long_running_alert_enabled(self, enabled: bool) -> None:
        async with self._lock:
            self._state["coffee_long_running_alert_enabled"] = enabled
            await asyncio.to_thread(self._save)

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
