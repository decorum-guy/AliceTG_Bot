from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from app.config import Settings
from app.services.app_state import AppStateStore
from app.services.home_assistant import HomeAssistantClient

LOGGER = logging.getLogger(__name__)
WARMUP_UPDATE_INTERVAL_SECONDS = 30
READY_UPDATE_INTERVAL_SECONDS = 5 * 60
NEAR_LONG_RUNNING_SECONDS = 5 * 60
WARMUP_COLOR_STEPS: tuple[tuple[float, str], ...] = (
    (0.40, "#0A84FF"),
    (0.70, "#00AEEF"),
    (0.90, "#00C7A3"),
    (1.0, "#A6D94A"),
)
POST_WARMUP_COLOR_STEPS: tuple[tuple[float, str], ...] = (
    (0.55, "#34C759"),
    (0.75, "#C9D94A"),
    (0.95, "#FF9F0A"),
    (1.0, "#FF6B00"),
)


class PushWardCoffeeActivity:
    def __init__(self, settings: Settings, ha: HomeAssistantClient, app_state: AppStateStore) -> None:
        self._settings = settings
        self._ha = ha
        self._app_state = app_state
        self._slug = settings.pushward_coffee_activity_slug
        self._error_log_path = Path(settings.pushward_error_log_path)
        self._task: asyncio.Task[None] | None = None

    @property
    def enabled(self) -> bool:
        return self._settings.pushward_coffee_activity_enabled

    async def start_or_restore(self) -> None:
        if not self.enabled:
            return
        self.cancel_updates()
        await self._create_activity()
        await self._update_from_state()
        self._task = asyncio.create_task(self._run_updates())
        LOGGER.info("PushWard coffee activity started: slug=%s", self._slug)

    async def stop(self) -> None:
        if not self.enabled:
            return
        self.cancel_updates()
        await self._end_activity()

    def cancel_updates(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None

    async def mark_warmed_up(self) -> None:
        if self.enabled:
            await self._update_ready()

    async def mark_long_running(self) -> None:
        if self.enabled:
            await self._update_long_running()

    async def close(self) -> None:
        self.cancel_updates()

    async def _run_updates(self) -> None:
        try:
            while self._app_state.coffee_machine_state == "on":
                sleep_seconds = await self._update_from_state()
                await asyncio.sleep(sleep_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._log_error("update_activity", exc, state="background_task")

    async def _update_from_state(self) -> int:
        if self._app_state.coffee_machine_state != "on":
            return READY_UPDATE_INTERVAL_SECONDS

        elapsed_seconds = _elapsed_seconds(self._app_state.coffee_on_since)
        warmup_delay = max(1, self._app_state.coffee_warmed_up_alert_delay_seconds)
        long_delay = max(warmup_delay, self._app_state.coffee_long_running_alert_delay_seconds)

        if elapsed_seconds < warmup_delay:
            await self._update_warming_up(elapsed_seconds, warmup_delay)
            return WARMUP_UPDATE_INTERVAL_SECONDS
        if elapsed_seconds >= long_delay:
            await self._update_long_running()
            return READY_UPDATE_INTERVAL_SECONDS

        post_warmup_ratio = _post_warmup_ratio(elapsed_seconds, warmup_delay, long_delay)
        accent_color = _post_warmup_accent_color(post_warmup_ratio)
        if post_warmup_ratio >= 0.55 or long_delay - elapsed_seconds <= NEAR_LONG_RUNNING_SECONDS:
            await self._update_near_long_running(elapsed_seconds, accent_color=accent_color)
            return WARMUP_UPDATE_INTERVAL_SECONDS

        await self._update_ready(accent_color=accent_color)
        return READY_UPDATE_INTERVAL_SECONDS

    async def _create_activity(self) -> None:
        await self._call(
            "create_activity",
            {
                "slug": self._slug,
                "name": "Кофемашина",
                "priority": 5,
                "stale_ttl": 300,
                "ended_ttl": 60,
            },
        )

    async def _update_warming_up(self, elapsed_seconds: int, warmup_delay: int) -> None:
        remaining_seconds = max(0, warmup_delay - elapsed_seconds)
        if elapsed_seconds <= 5:
            state_text = "Кофемашина включена"
            subtitle = f"Разогрев начался · осталось {_duration_text(remaining_seconds)}"
        else:
            state_text = f"Работает {_duration_text(elapsed_seconds)}"
            subtitle = f"Разогрета через {_duration_text(remaining_seconds)}"
        await self._update_activity(
            state_text=state_text,
            subtitle=subtitle,
            progress=min(1.0, elapsed_seconds / warmup_delay),
            icon="cup.and.saucer",
            accent_color=_warmup_accent_color(elapsed_seconds / warmup_delay),
            state="warming_up",
        )

    async def _update_ready(self, *, accent_color: str = "#34C759") -> None:
        await self._update_activity(
            state_text="Кофемашина разогрета",
            subtitle=f"Работает {_duration_text(_elapsed_seconds(self._app_state.coffee_on_since))}",
            progress=1.0,
            icon="cup.and.saucer",
            accent_color=accent_color,
            state="ready",
        )

    async def _update_near_long_running(self, elapsed_seconds: int, *, accent_color: str) -> None:
        await self._update_activity(
            state_text="Кофемашина разогрета",
            subtitle=f"Работает {_duration_text(elapsed_seconds)} · скоро напоминание",
            progress=1.0,
            icon="cup.and.saucer",
            accent_color=accent_color,
            state="near_long_running",
        )

    async def _update_long_running(self) -> None:
        await self._update_activity(
            state_text="Кофемашина работает слишком долго",
            subtitle=f"Работает {_duration_text(_elapsed_seconds(self._app_state.coffee_on_since))} · лучше выключить",
            progress=1.0,
            icon="exclamationmark.triangle",
            accent_color="#FF3B30",
            state="long_running",
        )

    async def _update_activity(
        self,
        *,
        state_text: str,
        subtitle: str,
        progress: float,
        icon: str,
        accent_color: str,
        state: str,
    ) -> None:
        await self._call(
            "update_activity",
            {
                "slug": self._slug,
                "state": "ONGOING",
                "template": "generic",
                "state_text": state_text,
                "subtitle": subtitle,
                "progress": round(max(0.0, min(1.0, progress)), 3),
                "icon": icon,
                "accent_color": accent_color,
            },
            state=state,
        )

    async def _end_activity(self) -> None:
        await self._call(
            "end_activity",
            {
                "slug": self._slug,
                "completion_message": "Кофемашина выключена",
            },
        )

    async def _call(self, service: str, payload: dict[str, object], *, state: str | None = None) -> None:
        try:
            await self._ha.call_service("pushward", service, payload)
        except Exception as exc:
            await self._log_error(service, exc, state=state)

    async def _log_error(self, action: str, exc: Exception, *, state: str | None = None) -> None:
        LOGGER.warning(
            "PushWard coffee activity failed: action=%s slug=%s state=%s error=%r",
            action,
            self._slug,
            state,
            exc,
        )
        safe_error = str(exc).replace("\n", " ")[:500]
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        state_part = f" state={state}" if state else ""
        line = f"[{timestamp}] action={action} slug={self._slug}{state_part} error={safe_error}\n"
        try:
            await asyncio.to_thread(self._append_error_log, line)
        except Exception:
            LOGGER.exception("Cannot write PushWard error log: path=%s", self._error_log_path)

    def _append_error_log(self, line: str) -> None:
        self._error_log_path.parent.mkdir(parents=True, exist_ok=True)
        with self._error_log_path.open("a", encoding="utf-8") as file:
            file.write(line)


def _elapsed_seconds(on_since: str | None) -> int:
    if not on_since:
        return 0
    try:
        started_at = datetime.fromisoformat(on_since)
    except ValueError:
        return 0
    return max(0, int((datetime.now(timezone.utc) - started_at).total_seconds()))


def _warmup_accent_color(progress: float) -> str:
    progress = max(0.0, min(0.999, progress))
    for threshold, color in WARMUP_COLOR_STEPS:
        if progress < threshold:
            return color
    return "#A6D94A"


def _post_warmup_ratio(elapsed_seconds: int, warmup_delay: int, long_delay: int) -> float:
    interval = max(1, long_delay - warmup_delay)
    return max(0.0, min(1.0, (elapsed_seconds - warmup_delay) / interval))


def _post_warmup_accent_color(ratio: float) -> str:
    ratio = max(0.0, min(0.999, ratio))
    for threshold, color in POST_WARMUP_COLOR_STEPS:
        if ratio < threshold:
            return color
    return "#FF6B00"


def _duration_text(total_seconds: int) -> str:
    total_seconds = max(0, int(total_seconds))
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    parts: list[str] = []
    if hours:
        parts.append(f"{hours} ч")
    if minutes:
        parts.append(f"{minutes} мин")
    if seconds and not hours:
        parts.append(f"{seconds} сек")
    if not parts:
        return "0 сек"
    return " ".join(parts)
