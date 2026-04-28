from __future__ import annotations

import os
from dataclasses import dataclass, field


COFFEE_SENSORS: dict[str, str] = {
    "voltage": "sensor.kofemashina_tekushchee_napriazhenie",
    "power": "",
    "current": "",
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


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    telegram_webhook_secret: str
    telegram_allowed_user_ids: set[int]
    telegram_admin_chat_id: int
    telegram_proxy: str | None
    ha_url: str
    ha_long_lived_token: str
    internal_webhook_secret: str
    webhook_path: str = "/webhook"
    listen_host: str = "0.0.0.0"
    listen_port: int = 8088
    coffee_switch_entity: str = "switch.kofemashina"
    bedroom_player_entity: str = "media_player.stantsiia_mini_spalnia"
    living_room_player_entity: str = "media_player.stantsiia_mini_zal"
    coffee_sensors: dict[str, str] = field(default_factory=lambda: COFFEE_SENSORS.copy())

    @property
    def telegram_proxy_url(self) -> str | None:
        return _telegram_proxy_url(self.telegram_proxy or "")

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            telegram_bot_token=_required("TELEGRAM_BOT_TOKEN"),
            telegram_webhook_secret=_required("TELEGRAM_WEBHOOK_SECRET"),
            telegram_allowed_user_ids=_split_ids(_required("TELEGRAM_ALLOWED_USER_IDS")),
            telegram_admin_chat_id=int(_required("TELEGRAM_ADMIN_CHAT_ID")),
            telegram_proxy=os.getenv("TELEGRAM_PROXY", ""),
            ha_url=os.getenv("HA_URL", "http://homeassistant:8123").rstrip("/"),
            ha_long_lived_token=_required("HA_LONG_LIVED_TOKEN"),
            internal_webhook_secret=_required("INTERNAL_WEBHOOK_SECRET"),
        )

    def is_allowed_user(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.telegram_allowed_user_ids


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Environment variable {name} is required")
    return value
