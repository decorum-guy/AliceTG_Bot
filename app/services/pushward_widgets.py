from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp

from app.config import Settings
from app.services.app_state import AppStateStore

LOGGER = logging.getLogger(__name__)
PUSHWARD_API_BASE_URL = "https://api.pushward.app"
PUSHWARD_WIDGET_TIMEOUT_SECONDS = 5


class PushWardCoffeeWidget:
    def __init__(self, settings: Settings, app_state: AppStateStore) -> None:
        self._settings = settings
        self._app_state = app_state
        self._slug = settings.pushward_coffee_widget_slug
        self._name = settings.pushward_coffee_widget_name
        self._error_log_path = Path(settings.pushward_error_log_path)
        self._session: aiohttp.ClientSession | None = None
        self._task: asyncio.Task[None] | None = None
        self._one_shot_tasks: set[asyncio.Task[None]] = set()
        self._missing_key_logged = False
        self._disabled_logged = False

    @property
    def enabled(self) -> bool:
        return self._settings.pushward_coffee_widget_enabled

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def restore(self) -> None:
        if not self._is_available():
            return
        if self._app_state.coffee_machine_state == "on" and self._app_state.coffee_on_since:
            self.start()
            return
        self.update_once()

    def start(self) -> None:
        if not self._is_available():
            return
        if self.is_running:
            LOGGER.info("PushWard coffee widget loop already running, skip start")
            self.update_once()
            return
        self._task = asyncio.create_task(self._run_loop())
        LOGGER.info("PushWard coffee widget loop started")

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            LOGGER.info("PushWard coffee widget loop cancelled")
        self._task = None
        if self._settings.pushward_coffee_widget_enabled and self._settings.pushward_integration_key:
            self.update_once()

    def update_once(self) -> None:
        if not self._is_available():
            return
        self._create_one_shot_task(self._safe_update())

    async def close(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self._task = None
        for task in self._one_shot_tasks:
            task.cancel()
        if self._one_shot_tasks:
            await asyncio.gather(*self._one_shot_tasks, return_exceptions=True)
        self._one_shot_tasks.clear()
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    def _create_one_shot_task(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._one_shot_tasks.add(task)
        task.add_done_callback(self._one_shot_tasks.discard)

    async def _run_loop(self) -> None:
        try:
            await self._safe_update()
            while self._app_state.coffee_machine_state == "on":
                await asyncio.sleep(self._settings.pushward_coffee_widget_update_interval_seconds)
                await self._safe_update()
        except asyncio.CancelledError:
            LOGGER.info("PushWard coffee widget loop cancelled")
            raise
        finally:
            LOGGER.info("PushWard coffee widget loop stopped")

    async def _safe_update(self) -> None:
        try:
            await update_coffee_widget(self)
        except Exception as exc:
            await self._log_error("widget_update", exc)

    async def _patch_widget(self, content: dict[str, Any]) -> bool:
        response = await self._request(
            "PATCH",
            f"/widgets/{self._slug}",
            {"content": content},
            action="patch_widget",
            allow_not_found=True,
        )
        if response == "not_found":
            LOGGER.info("PushWard coffee widget bootstrap attempted after 404")
            await bootstrap_coffee_widget_if_needed(self, content)
            return True
        return True

    async def _bootstrap_widget(self, content: dict[str, Any]) -> None:
        await self._request(
            "POST",
            "/widgets",
            {
                "slug": self._slug,
                "name": self._name,
                "content": content,
            },
            action="bootstrap_widget",
        )

    async def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any],
        *,
        action: str,
        allow_not_found: bool = False,
    ) -> dict[str, Any] | str | None:
        session = self._get_session()
        try:
            async with session.request(method, f"{PUSHWARD_API_BASE_URL}{path}", json=payload) as response:
                if allow_not_found and response.status == 404:
                    return "not_found"
                if response.status >= 400:
                    body = await response.text()
                    raise RuntimeError(f"PushWard API HTTP {response.status}: {body[:300]}")
                if response.content_type == "application/json":
                    return await response.json()
                return None
        except Exception as exc:
            await self._log_error(action, exc)
            raise

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={
                    "Authorization": f"Bearer {self._settings.pushward_integration_key}",
                    "Content-Type": "application/json",
                },
                timeout=aiohttp.ClientTimeout(total=PUSHWARD_WIDGET_TIMEOUT_SECONDS),
            )
        return self._session

    def _is_available(self) -> bool:
        if not self._settings.pushward_coffee_widget_enabled:
            if not self._disabled_logged:
                LOGGER.info("PushWard coffee widget skipped: disabled")
                self._disabled_logged = True
            return False
        if not self._settings.pushward_integration_key:
            if not self._missing_key_logged:
                LOGGER.warning("PushWard coffee widget skipped: missing integration key")
                self._missing_key_logged = True
            return False
        return True

    async def _log_error(self, action: str, exc: Exception) -> None:
        LOGGER.warning(
            "PushWard coffee widget failed: action=%s slug=%s error=%r",
            action,
            self._slug,
            exc,
        )
        safe_error = str(exc).replace("\n", " ")[:500]
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        line = f"[{timestamp}] action={action} slug={self._slug} error={safe_error}\n"
        try:
            await asyncio.to_thread(self._append_error_log, line)
        except Exception:
            LOGGER.exception("Cannot write PushWard widget error log: path=%s", self._error_log_path)

    def _append_error_log(self, line: str) -> None:
        self._error_log_path.parent.mkdir(parents=True, exist_ok=True)
        with self._error_log_path.open("a", encoding="utf-8") as file:
            file.write(line)


def build_coffee_widget_content(app_state: AppStateStore) -> dict[str, Any]:
    is_on = app_state.coffee_machine_state == "on" and bool(app_state.coffee_on_since)
    if not is_on:
        return {
            "template": "stat_list",
            "icon": "cup.and.saucer.fill",
            "accent_color": "#8E8E93",
            "stat_rows": [
                {"label": "Статус", "value": "Выкл"},
                {"label": "Работает", "value": "—"},
            ],
        }

    elapsed_seconds = _elapsed_seconds(app_state.coffee_on_since)
    return {
        "template": "stat_list",
        "icon": "cup.and.saucer.fill",
        "accent_color": _coffee_widget_accent_color(app_state, elapsed_seconds),
        "stat_rows": [
            {"label": "Статус", "value": "Вкл"},
            {"label": "Работает", "value": _runtime_text(elapsed_seconds)},
        ],
    }


async def update_coffee_widget(widget: PushWardCoffeeWidget) -> None:
    content = build_coffee_widget_content(widget._app_state)
    await widget._patch_widget(content)
    rows = content["stat_rows"]
    LOGGER.info(
        "PushWard coffee widget update sent: status=%s runtime=%s",
        rows[0]["value"],
        rows[1]["value"],
    )


async def bootstrap_coffee_widget_if_needed(widget: PushWardCoffeeWidget, content: dict[str, Any] | None = None) -> None:
    await widget._bootstrap_widget(content or build_coffee_widget_content(widget._app_state))


def start_coffee_widget_loop(widget: PushWardCoffeeWidget | None) -> None:
    if widget is not None:
        widget.start()


def stop_coffee_widget_loop(widget: PushWardCoffeeWidget | None) -> None:
    if widget is not None:
        widget.stop()


def _coffee_widget_accent_color(app_state: AppStateStore, elapsed_seconds: int) -> str:
    warmup_delay = max(1, app_state.coffee_warmed_up_alert_delay_seconds)
    long_delay = max(warmup_delay, app_state.coffee_long_running_alert_delay_seconds)
    if elapsed_seconds >= long_delay:
        return "#FF3B30"
    if elapsed_seconds >= max(warmup_delay, long_delay - 5 * 60):
        return "#FF9F0A"
    if elapsed_seconds >= warmup_delay:
        return "#34C759"
    progress = max(0.0, min(0.999, elapsed_seconds / warmup_delay))
    if progress < 0.40:
        return "#0A84FF"
    if progress < 0.70:
        return "#00AEEF"
    return "#00C7A3"


def _runtime_text(total_seconds: int) -> str:
    if total_seconds < 60:
        return "меньше 1 мин"
    total_minutes = total_seconds // 60
    hours = total_minutes // 60
    minutes = total_minutes % 60
    if hours:
        return f"{hours} ч {minutes:02d} мин"
    return f"{minutes} мин"


def _elapsed_seconds(on_since: str | None) -> int:
    if not on_since:
        return 0
    try:
        started_at = datetime.fromisoformat(on_since)
    except ValueError:
        return 0
    return max(0, int((datetime.now(timezone.utc) - started_at).total_seconds()))
