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
    internal_webhook_secret: str
    shortcuts_secret_token: str
    app_state_path: str = "/app/data/state.json"
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
            internal_webhook_secret=_required("INTERNAL_WEBHOOK_SECRET"),
            shortcuts_secret_token=os.getenv("SHORTCUTS_SECRET_TOKEN", "").strip(),
            app_state_path=os.getenv("APP_STATE_PATH", "/app/data/state.json").strip() or "/app/data/state.json",
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
