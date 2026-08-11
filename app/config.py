from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Literal


COFFEE_SENSORS: dict[str, str] = {
    "voltage": "sensor.kofemashina_tekushchee_napriazhenie",
    "power": "sensor.kofemashina_potrebliaemaia_moshchnost",
    "current": "sensor.kofemashina_potreblenie_toka",
}


def _split_ids(value: str) -> set[int]:
    ids: set[int] = set()
    for item in value.replace(";", ",").split(","):
        item = item.strip()
        if item:
            ids.add(int(item))
    return ids


def _telegram_proxy_url(value: str) -> str | None:
    value = value.strip()
    if not value:
        return None
    if value.startswith(("http://", "https://", "socks4://", "socks5://")):
        return value
    return f"http://{value}"


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name, "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "y", "on"}


def _split_csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    telegram_webhook_secret: str
    telegram_allowed_user_ids: set[int]
    telegram_sonya_user_ids: set[int]
    telegram_admin_chat_id: int
    telegram_mode: Literal["polling", "webhook"]
    telegram_drop_pending_updates: bool
    telegram_polling_timeout: int
    telegram_polling_max_errors: int
    telegram_enable_test_1_min_reminder: bool
    telegram_proxy: str | None
    ha_url: str
    ha_long_lived_token: str
    ha_mobile_notify_service: str
    ha_mobile_notify_services: tuple[str, ...]
    internal_webhook_secret: str
    shortcuts_secret_token: str
    control_center_api_token: str
    app_version: str = "unknown"
    app_commit: str = "unknown"
    reminders_state_path: str = "/app/data/reminders.json"
    app_state_path: str = "/app/data/state.json"
    planning_db_path: str = "/app/data/planning.sqlite3"
    pushward_coffee_activity_enabled: bool = False
    pushward_coffee_activity_slug: str = "ha-coffee-machine"
    pushward_error_log_path: str = "/app/data/pushward_errors.log"
    pushward_coffee_ended_ttl_seconds: int = 3
    pushward_coffee_off_hold_seconds: int = 5
    coffee_warmup_gif_url: str = ""
    pushward_coffee_widget_enabled: bool = False
    pushward_integration_key: str = ""
    pushward_coffee_widget_slug: str = "ha-coffee-machine-widget"
    pushward_coffee_widget_name: str = "Кофемашина"
    pushward_coffee_widget_update_interval_seconds: int = 60
    coffee_timing_refresh_interval_seconds: int = 30
    coffee_timing_stale_after_seconds: int = 90
    coffee_timing_refresh_max_backoff_seconds: int = 120
    webhook_path: str = "/webhook"
    listen_host: str = "0.0.0.0"
    listen_port: int = 8088
    coffee_switch_entity: str = "switch.kofemashina"
    kettle_entity: str = "water_heater.chainik"
    kettle_keep_warm_switch_entity: str = "switch.chainik_podderzhanie_tepla"
    kettle_light_switch_entity: str = "switch.chainik_podsvetka"
    kettle_mute_switch_entity: str = "switch.chainik_bez_zvuka"
    yandex_dialog_skill_name: str = "домашний помощник"
    bedroom_player_entity: str = "media_player.stantsiia_mini_spalnia"
    living_room_player_entity: str = "media_player.stantsiia_mini_zal"
    coffee_sensors: dict[str, str] = field(default_factory=lambda: COFFEE_SENSORS.copy())

    @property
    def telegram_proxy_url(self) -> str | None:
        return _telegram_proxy_url(self.telegram_proxy or "")

    @classmethod
    def from_env(cls) -> "Settings":
        telegram_mode = os.getenv("TELEGRAM_MODE", "polling").strip().lower()
        if telegram_mode not in {"polling", "webhook"}:
            raise RuntimeError("TELEGRAM_MODE must be polling or webhook")

        return cls(
            telegram_bot_token=_required("TELEGRAM_BOT_TOKEN"),
            telegram_webhook_secret=_required("TELEGRAM_WEBHOOK_SECRET"),
            telegram_allowed_user_ids=_split_ids(_required("TELEGRAM_ALLOWED_USER_IDS")),
            telegram_sonya_user_ids=_split_ids(os.getenv("TELEGRAM_SONYA_USER_IDS", "")),
            telegram_admin_chat_id=int(_required("TELEGRAM_ADMIN_CHAT_ID")),
            telegram_mode=telegram_mode,  # type: ignore[arg-type]
            telegram_drop_pending_updates=_bool_env("TELEGRAM_DROP_PENDING_UPDATES", True),
            telegram_polling_timeout=int(os.getenv("TELEGRAM_POLLING_TIMEOUT", "30")),
            telegram_polling_max_errors=int(os.getenv("TELEGRAM_POLLING_MAX_ERRORS", "10")),
            telegram_enable_test_1_min_reminder=_bool_env("TELEGRAM_ENABLE_TEST_1_MIN_REMINDER", False),
            telegram_proxy=os.getenv("TELEGRAM_PROXY", ""),
            ha_url=os.getenv("HA_URL", "http://homeassistant:8123").rstrip("/"),
            ha_long_lived_token=_required("HA_LONG_LIVED_TOKEN"),
            ha_mobile_notify_service=os.getenv("HA_MOBILE_NOTIFY_SERVICE", "").strip(),
            ha_mobile_notify_services=_ha_mobile_notify_services_from_env(),
            internal_webhook_secret=_required("INTERNAL_WEBHOOK_SECRET"),
            shortcuts_secret_token=os.getenv("SHORTCUTS_SECRET_TOKEN", "").strip(),
            control_center_api_token=os.getenv("CONTROL_CENTER_API_TOKEN", "").strip(),
            app_version=os.getenv("APP_VERSION", "unknown").strip() or "unknown",
            app_commit=os.getenv("APP_COMMIT", "unknown").strip() or "unknown",
            reminders_state_path=os.getenv("REMINDERS_STATE_PATH", "/app/data/reminders.json").strip()
            or "/app/data/reminders.json",
            app_state_path=os.getenv("APP_STATE_PATH", "/app/data/state.json").strip() or "/app/data/state.json",
            planning_db_path=os.getenv("PLANNING_DB_PATH", "/app/data/planning.sqlite3").strip()
            or "/app/data/planning.sqlite3",
            pushward_coffee_activity_enabled=_bool_env("PUSHWARD_COFFEE_ACTIVITY_ENABLED", False),
            pushward_coffee_activity_slug=os.getenv("PUSHWARD_COFFEE_ACTIVITY_SLUG", "ha-coffee-machine").strip()
            or "ha-coffee-machine",
            pushward_error_log_path=os.getenv("PUSHWARD_ERROR_LOG_PATH", "/app/data/pushward_errors.log").strip()
            or "/app/data/pushward_errors.log",
            pushward_coffee_ended_ttl_seconds=max(1, int(os.getenv("PUSHWARD_COFFEE_ENDED_TTL_SECONDS", "3"))),
            pushward_coffee_off_hold_seconds=max(0, int(os.getenv("PUSHWARD_COFFEE_OFF_HOLD_SECONDS", "5"))),
            coffee_warmup_gif_url=os.getenv("COFFEE_WARMUP_GIF_URL", "").strip(),
            pushward_coffee_widget_enabled=_bool_env("PUSHWARD_COFFEE_WIDGET_ENABLED", False),
            pushward_integration_key=os.getenv("PUSHWARD_INTEGRATION_KEY", "").strip(),
            pushward_coffee_widget_slug=os.getenv(
                "PUSHWARD_COFFEE_WIDGET_SLUG",
                "ha-coffee-machine-widget",
            ).strip()
            or "ha-coffee-machine-widget",
            pushward_coffee_widget_name=os.getenv("PUSHWARD_COFFEE_WIDGET_NAME", "Кофемашина").strip()
            or "Кофемашина",
            pushward_coffee_widget_update_interval_seconds=max(
                10,
                int(os.getenv("PUSHWARD_COFFEE_WIDGET_UPDATE_INTERVAL_SECONDS", "60")),
            ),
            coffee_timing_refresh_interval_seconds=max(
                5,
                int(os.getenv("COFFEE_TIMING_REFRESH_INTERVAL_SECONDS", "30")),
            ),
            coffee_timing_stale_after_seconds=max(
                15,
                int(os.getenv("COFFEE_TIMING_STALE_AFTER_SECONDS", "90")),
            ),
            coffee_timing_refresh_max_backoff_seconds=max(
                30,
                int(os.getenv("COFFEE_TIMING_REFRESH_MAX_BACKOFF_SECONDS", "120")),
            ),
            yandex_dialog_skill_name=os.getenv("YANDEX_DIALOG_SKILL_NAME", "домашний помощник").strip()
            or "домашний помощник",
        )

    def is_allowed_user(self, user_id: int | None) -> bool:
        return self.is_admin_user(user_id) or self.is_sonya_user(user_id)

    def is_admin_user(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.telegram_allowed_user_ids

    def is_sonya_user(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.telegram_sonya_user_ids


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Environment variable {name} is required")
    return value


def _ha_mobile_notify_services_from_env() -> tuple[str, ...]:
    services = _split_csv(os.getenv("HA_MOBILE_NOTIFY_SERVICES", ""))
    if services:
        return services
    fallback = os.getenv("HA_MOBILE_NOTIFY_SERVICE", "").strip()
    return (fallback,) if fallback else ()
