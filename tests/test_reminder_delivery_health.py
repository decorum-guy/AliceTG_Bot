from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from app.services.reminder_store import ReminderSettings
from app.web.internal_routes import _delivery_channel_health


class ReminderDeliveryHealthTests(unittest.TestCase):
    def settings(self, *, ha_url: str = "https://ha.example", token: str = "ha-secret"):
        return SimpleNamespace(
            ha_url=ha_url,
            ha_long_lived_token=token,
            telegram_bot_token="telegram-secret",
            telegram_admin_chat_id=123,
            ha_mobile_notify_services=(),
        )

    def test_voice_disabled_is_truthfully_unavailable_without_secrets(self):
        health = _delivery_channel_health(
            self.settings(),
            ReminderSettings(voice_enabled=False, voice_station_entity_id="media_player.office"),
        )
        self.assertEqual(health["spoken"]["alice"], {"status": "unavailable", "code": "alice_voice_disabled"})
        encoded = json.dumps(health, ensure_ascii=False)
        self.assertNotIn("ha-secret", encoded)
        self.assertNotIn("telegram-secret", encoded)
        self.assertNotIn("ha.example", encoded)

    def test_enabled_configured_alice_is_available(self):
        health = _delivery_channel_health(
            self.settings(),
            ReminderSettings(voice_enabled=True, voice_station_entity_id="media_player.office"),
        )
        self.assertEqual(health["spoken"]["alice"], {"status": "available", "code": None})

    def test_missing_configuration_is_not_configured(self):
        health = _delivery_channel_health(
            self.settings(ha_url="", token=""),
            ReminderSettings(voice_enabled=True, voice_station_entity_id="media_player.office"),
        )
        self.assertEqual(health["spoken"]["alice"], {"status": "not_configured", "code": "alice_not_configured"})


if __name__ == "__main__":
    unittest.main()
