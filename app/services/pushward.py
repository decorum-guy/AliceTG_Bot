from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from app.config import Settings
from app.services.app_state import AppStateStore
from app.services.home_assistant import HomeAssistantClient, HomeAssistantError

LOGGER = logging.getLogger(__name__)
WARMUP_UPDATE_INTERVAL_SECONDS = 30
MINUTES_ONLY_UPDATE_INTERVAL_SECONDS = 60
NEAR_LONG_RUNNING_SECONDS = 5 * 60
PUSHWARD_CALL_TIMEOUT_SECONDS = 5
PUSHWARD_MAX_CONSECUTIVE_FAILURES = 3
PUSHWARD_DEGRADED_BACKOFF_SECONDS = 5 * 60
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


def _normalize_update_activity_payload(payload: dict[str, object]) -> dict[str, object]:
    activity_state = payload.get("state")
    if isinstance(activity_state, str):
        return {**payload, "state": activity_state.lower()}
    return payload


class PushWardCoffeeActivity:
    def __init__(self, settings: Settings, ha: HomeAssistantClient, app_state: AppStateStore) -> None:
        self._settings = settings
        self._ha = ha
        self._app_state = app_state
        self._slug = settings.pushward_coffee_activity_slug
        self._error_log_path = Path(settings.pushward_error_log_path)
        self._task: asyncio.Task[None] | None = None
        self._off_cleanup_task: asyncio.Task[None] | None = None
        self._delete_cleanup_task: asyncio.Task[None] | None = None
        self._activity_active = False
        self._cycle_id: str | None = None
        self._degraded = False
        self._consecutive_failures = 0
        self._update_service: str | None = None

    @property
    def enabled(self) -> bool:
        return self._settings.pushward_coffee_activity_enabled

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start_or_restore(self) -> None:
        if not self.enabled:
            return
        self._reset_cycle_state_if_needed()
        self._cancel_off_cleanup()
        self._cancel_delete_cleanup()
        if self.is_running:
            LOGGER.info("PushWard loop already running, skip start: slug=%s", self._slug)
            return
        if self._degraded:
            LOGGER.info("PushWard coffee activity start skipped because current cycle is degraded: slug=%s", self._slug)
            return
        self._task = asyncio.create_task(self._run_updates())
        LOGGER.info("PushWard loop started: slug=%s", self._slug)

    async def stop(self) -> None:
        if not self.enabled:
            return
        if self._off_cleanup_task and not self._off_cleanup_task.done():
            LOGGER.info("PushWard coffee activity off cleanup already running: slug=%s", self._slug)
            return
        self.cancel_updates()
        self._off_cleanup_task = asyncio.create_task(self._run_off_cleanup())

    async def _run_off_cleanup(self) -> None:
        was_active = self._activity_active
        LOGGER.info("PushWard coffee activity off cleanup started: slug=%s active=%s", self._slug, was_active)
        await self._update_off()
        await asyncio.sleep(self._settings.pushward_coffee_off_hold_seconds)
        if was_active:
            await self._end_activity()
        else:
            LOGGER.info("PushWard end_activity skipped because activity was not marked active: slug=%s", self._slug)
        self._activity_active = False
        if was_active:
            self._delete_cleanup_task = asyncio.create_task(self._delete_activity_later())

    def cancel_updates(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            LOGGER.info("PushWard loop cancelled: slug=%s", self._slug)
        self._task = None

    async def mark_warmed_up(self) -> None:
        if self.enabled and not self._degraded:
            asyncio.create_task(self._safe_one_shot_update(self._update_ready(), "warmed_up"))

    async def mark_long_running(self) -> None:
        if self.enabled and not self._degraded:
            asyncio.create_task(self._safe_one_shot_update(self._update_long_running(), "long_running"))

    async def close(self) -> None:
        self.cancel_updates()
        self._cancel_off_cleanup()
        self._cancel_delete_cleanup()

    def _cancel_off_cleanup(self) -> None:
        if self._off_cleanup_task and not self._off_cleanup_task.done():
            self._off_cleanup_task.cancel()
        self._off_cleanup_task = None

    def _cancel_delete_cleanup(self) -> None:
        if self._delete_cleanup_task and not self._delete_cleanup_task.done():
            self._delete_cleanup_task.cancel()
        self._delete_cleanup_task = None

    async def _run_updates(self) -> None:
        try:
            while self._app_state.coffee_machine_state == "on":
                if self._degraded:
                    LOGGER.info(
                        "PushWard coffee activity disabled for current cycle after consecutive failures: slug=%s",
                        self._slug,
                    )
                    return
                if not self._activity_active:
                    created = await self._create_activity()
                    if not created:
                        if self._degraded:
                            return
                        LOGGER.info(
                            "PushWard coffee activity backoff started: seconds=%s slug=%s",
                            PUSHWARD_DEGRADED_BACKOFF_SECONDS,
                            self._slug,
                        )
                        await asyncio.sleep(PUSHWARD_DEGRADED_BACKOFF_SECONDS)
                        continue
                    self._activity_active = True
                sleep_seconds = await self._update_from_state()
                await asyncio.sleep(sleep_seconds)
        except asyncio.CancelledError:
            LOGGER.info("PushWard loop cancelled: slug=%s", self._slug)
            raise
        except Exception as exc:
            await self._log_error("update_activity", exc, state="background_task")
        finally:
            LOGGER.info("PushWard loop stopped: slug=%s", self._slug)

    async def _update_from_state(self) -> int:
        if self._app_state.coffee_machine_state != "on":
            return self._regular_update_interval()

        elapsed_seconds = _elapsed_seconds(self._app_state.coffee_on_since)
        warmup_delay = max(1, self._app_state.coffee_warmed_up_alert_delay_seconds)
        long_delay = max(warmup_delay, self._app_state.coffee_long_running_alert_delay_seconds)
        next_sleep = self._next_update_sleep(elapsed_seconds, warmup_delay, long_delay)

        if elapsed_seconds < warmup_delay:
            await self._update_warming_up(elapsed_seconds, warmup_delay)
            return next_sleep
        if elapsed_seconds >= long_delay:
            await self._update_long_running()
            return next_sleep

        post_warmup_ratio = _post_warmup_ratio(elapsed_seconds, warmup_delay, long_delay)
        accent_color = _post_warmup_accent_color(post_warmup_ratio)
        if post_warmup_ratio >= 0.55 or long_delay - elapsed_seconds <= NEAR_LONG_RUNNING_SECONDS:
            await self._update_near_long_running(elapsed_seconds, accent_color=accent_color)
            return next_sleep

        await self._update_ready(accent_color=accent_color)
        return next_sleep

    def _regular_update_interval(self) -> int:
        if self._app_state.coffee_pushward_show_seconds:
            return WARMUP_UPDATE_INTERVAL_SECONDS
        return MINUTES_ONLY_UPDATE_INTERVAL_SECONDS

    def _next_update_sleep(self, elapsed_seconds: int, warmup_delay: int, long_delay: int) -> int:
        candidates = [self._regular_update_interval()]
        if elapsed_seconds < warmup_delay:
            candidates.append(max(1, warmup_delay - elapsed_seconds))
        if elapsed_seconds < long_delay:
            candidates.append(max(1, long_delay - elapsed_seconds))
        return max(1, min(candidates))

    async def _create_activity(self) -> bool:
        return await self._call(
            "create_activity",
            {
                "slug": self._slug,
                "name": "Кофемашина",
                "priority": 5,
                "stale_ttl": 300,
                "ended_ttl": self._settings.pushward_coffee_ended_ttl_seconds,
            },
        )

    async def _update_warming_up(self, elapsed_seconds: int, warmup_delay: int) -> None:
        remaining_seconds = max(0, warmup_delay - elapsed_seconds)
        show_seconds = self._app_state.coffee_pushward_show_seconds
        if elapsed_seconds <= 5:
            state_text = "Кофемашина включена"
            subtitle = f"Разогрев начался · осталось {_remaining_duration_text(remaining_seconds, show_seconds=show_seconds)}"
        else:
            state_text = f"Работает {_elapsed_duration_text(elapsed_seconds, show_seconds=show_seconds)}"
            if show_seconds or remaining_seconds >= 60:
                subtitle = f"Разогрета через {_remaining_duration_text(remaining_seconds, show_seconds=show_seconds)}"
            else:
                subtitle = "Почти разогрета · осталось меньше 1 мин"
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
            subtitle=f"Работает {_elapsed_duration_text(_elapsed_seconds(self._app_state.coffee_on_since), show_seconds=self._app_state.coffee_pushward_show_seconds)}",
            progress=1.0,
            icon="cup.and.saucer",
            accent_color=accent_color,
            state="ready",
        )

    async def _update_near_long_running(self, elapsed_seconds: int, *, accent_color: str) -> None:
        await self._update_activity(
            state_text="Кофемашина разогрета",
            subtitle=f"Работает {_elapsed_duration_text(elapsed_seconds, show_seconds=self._app_state.coffee_pushward_show_seconds)} · скоро напоминание",
            progress=1.0,
            icon="cup.and.saucer",
            accent_color=accent_color,
            state="near_long_running",
        )

    async def _update_long_running(self) -> None:
        await self._update_activity(
            state_text="Кофемашина работает слишком долго",
            subtitle=f"Работает {_elapsed_duration_text(_elapsed_seconds(self._app_state.coffee_on_since), show_seconds=self._app_state.coffee_pushward_show_seconds)} · лучше выключить",
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
        LOGGER.info(
            "PushWard coffee activity update: phase=%s elapsed_seconds=%s progress=%s color=%s",
            state,
            _elapsed_seconds(self._app_state.coffee_on_since),
            round(max(0.0, min(1.0, progress)), 3),
            accent_color,
        )
        await self._call(
            "update_activity",
            {
                "slug": self._slug,
                "state": "ongoing",
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
        LOGGER.info("PushWard coffee activity ended: slug=%s", self._slug)

    async def _update_off(self) -> None:
        await self._call(
            "update_activity",
            {
                "slug": self._slug,
                "state": "ongoing",
                "state_text": "Кофемашина выключена",
                "subtitle": " ",
                "progress": 0.0,
                "icon": "power",
                "accent_color": "#8E8E93",
            },
            state="off",
        )

    async def _delete_activity_later(self) -> None:
        await asyncio.sleep(15)
        await self._call("delete_activity", {"slug": self._slug}, state="cleanup")

    async def _call(self, service: str, payload: dict[str, object], *, state: str | None = None) -> bool:
        if service == "update_activity":
            payload = _normalize_update_activity_payload(payload)
            return await self._call_update_activity(payload, state=state)
        try:
            await self._ha.call_service(
                "pushward",
                service,
                payload,
                timeout_seconds=PUSHWARD_CALL_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            await self._log_error(service, exc, state=state, payload=payload)
            self._record_failure(service, exc, state=state)
            return False
        self._consecutive_failures = 0
        return True

    async def _call_update_activity(self, payload: dict[str, object], *, state: str | None = None) -> bool:
        service = await self._select_update_service()
        service_payload = self._payload_for_update_service(service, payload)
        try:
            await self._ha.call_service(
                "pushward",
                service,
                service_payload,
                timeout_seconds=PUSHWARD_CALL_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            if service == "update_activity_generic" and _is_missing_service_error(exc):
                LOGGER.warning(
                    "PushWard activity update service unavailable, falling back: selected=pushward.%s fallback=pushward.update_activity slug=%s",
                    service,
                    self._slug,
                )
                self._update_service = "update_activity"
                return await self._call_update_activity(payload, state=state)
            await self._log_error(service, exc, state=state, payload=service_payload)
            self._record_failure(service, exc, state=state)
            return False
        self._consecutive_failures = 0
        return True

    async def _select_update_service(self) -> str:
        if self._update_service:
            return self._update_service
        selected = "update_activity"
        try:
            services = await self._ha.get_services()
        except Exception as exc:
            LOGGER.warning(
                "Cannot resolve PushWard activity update services, using fallback: fallback=pushward.update_activity slug=%s error=%r",
                self._slug,
                exc,
            )
        else:
            pushward_services = services.get("pushward", set())
            if "update_activity_generic" in pushward_services:
                selected = "update_activity_generic"
            elif "update_activity" not in pushward_services:
                LOGGER.warning(
                    "PushWard activity update service not advertised by Home Assistant: slug=%s services=%s",
                    self._slug,
                    ",".join(sorted(pushward_services)),
                )
        self._update_service = selected
        LOGGER.info("PushWard activity update service selected: pushward.%s", selected)
        return selected

    def _payload_for_update_service(self, service: str, payload: dict[str, object]) -> dict[str, object]:
        if service == "update_activity_generic":
            return {key: value for key, value in payload.items() if key != "template"}
        return {**payload, "template": "generic"}

    async def _safe_one_shot_update(self, update_coro, state: str) -> None:
        try:
            await update_coro
        except Exception as exc:
            await self._log_error("update_activity", exc, state=state)

    def _reset_cycle_state_if_needed(self) -> None:
        cycle_id = self._app_state.coffee_on_since
        if cycle_id == self._cycle_id:
            return
        self._cycle_id = cycle_id
        self._degraded = False
        self._consecutive_failures = 0
        self._activity_active = False

    def _record_failure(self, action: str, exc: Exception, *, state: str | None = None) -> None:
        self._consecutive_failures += 1
        if _is_rate_limit_error(exc):
            self._degraded = True
            LOGGER.warning(
                "PushWard coffee activity disabled for current cycle after rate limit: action=%s slug=%s state=%s",
                action,
                self._slug,
                state,
            )
            return
        if self._consecutive_failures >= PUSHWARD_MAX_CONSECUTIVE_FAILURES:
            self._degraded = True
            LOGGER.warning(
                "PushWard coffee activity disabled for current cycle after consecutive failures: action=%s slug=%s state=%s failures=%s",
                action,
                self._slug,
                state,
                self._consecutive_failures,
            )

    async def _log_error(
        self,
        action: str,
        exc: Exception,
        *,
        state: str | None = None,
        payload: dict[str, object] | None = None,
    ) -> None:
        status = exc.status if isinstance(exc, HomeAssistantError) else None
        body = exc.body if isinstance(exc, HomeAssistantError) else None
        safe_payload = _sanitize_payload(payload)
        LOGGER.warning(
            "PushWard coffee activity failed: service=pushward.%s slug=%s state=%s payload=%s response_status=%s response_body=%s error=%r",
            action,
            self._slug,
            state,
            safe_payload,
            status,
            _sanitize_response_body(body),
            exc,
        )
        safe_error = str(exc).replace("\n", " ")[:500]
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        state_part = f" state={state}" if state else ""
        status_part = f" response_status={status}" if status is not None else ""
        body_part = f" response_body={_sanitize_response_body(body)}" if body else ""
        payload_part = f" payload={safe_payload}" if safe_payload is not None else ""
        line = (
            f"[{timestamp}] service=pushward.{action} slug={self._slug}{state_part}"
            f"{payload_part}{status_part}{body_part} error={safe_error}\n"
        )
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


def _sanitize_payload(payload: dict[str, object] | None) -> dict[str, object] | None:
    if payload is None:
        return None
    sensitive_parts = ("token", "secret", "key", "password", "authorization")
    safe: dict[str, object] = {}
    for key, value in payload.items():
        if any(part in key.lower() for part in sensitive_parts):
            safe[key] = "<redacted>"
        else:
            safe[key] = value
    return safe


def _sanitize_response_body(body: str | None) -> str | None:
    if body is None:
        return None
    return body.replace("\n", " ")[:1000]


def _is_missing_service_error(exc: Exception) -> bool:
    if not isinstance(exc, HomeAssistantError):
        return False
    if exc.status == 404:
        return True
    body = (exc.body or "").lower()
    message = str(exc).lower()
    return "service not found" in body or "service not found" in message


def _post_warmup_accent_color(ratio: float) -> str:
    ratio = max(0.0, min(0.999, ratio))
    for threshold, color in POST_WARMUP_COLOR_STEPS:
        if ratio < threshold:
            return color
    return "#FF6B00"


def _elapsed_duration_text(total_seconds: int, *, show_seconds: bool) -> str:
    if show_seconds:
        return _duration_text(total_seconds, include_seconds=True)
    total_minutes = max(0, int(total_seconds) // 60)
    hours = total_minutes // 60
    minutes = total_minutes % 60
    if hours and minutes:
        return f"{hours} ч {minutes} мин"
    if hours:
        return f"{hours} ч"
    return f"{minutes} мин"


def _remaining_duration_text(total_seconds: int, *, show_seconds: bool) -> str:
    if show_seconds:
        return _duration_text(total_seconds, include_seconds=True)
    if total_seconds < 60:
        return "меньше 1 мин"
    total_minutes = (max(0, int(total_seconds)) + 59) // 60
    hours = total_minutes // 60
    minutes = total_minutes % 60
    if hours and minutes:
        return f"{hours} ч {minutes} мин"
    if hours:
        return f"{hours} ч"
    return f"{minutes} мин"


def _duration_text(total_seconds: int, *, include_seconds: bool) -> str:
    total_seconds = max(0, int(total_seconds))
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    parts: list[str] = []
    if hours:
        parts.append(f"{hours} ч")
    if minutes:
        parts.append(f"{minutes} мин")
    if include_seconds and seconds:
        parts.append(f"{seconds} сек")
    if not parts:
        return "0 сек"
    return " ".join(parts)


def _is_rate_limit_error(exc: Exception) -> bool:
    message = repr(exc).lower()
    return "rate limit" in message or "rate limited" in message or "retrying in" in message
