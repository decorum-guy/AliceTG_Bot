from __future__ import annotations

import tempfile
import unittest
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.handlers.station import (_music_keyboard, station_preset_play, station_preset_add,
                                  station_preset_title, station_preset_command, station_preset_save,
                                  station_preset_delete_prompt, PresetDraft)
from app.services.app_state import AppStatePersistenceError, AppStateRevisionConflict, AppStateStore
from app.services.home_assistant import HomeAssistantDispatchUncertain
from app.services.station_actions import StationActionService


class FakeHa:
    def __init__(self):
        self.calls = []
        self.uncertain = False

    async def call_service_once(self, domain, service, payload):
        self.calls.append((domain, service, payload))
        if self.uncertain:
            raise HomeAssistantDispatchUncertain("uncertain")


class FakeState:
    def __init__(self):
        self.data = {}
        self.current = None

    async def clear(self):
        self.data = {}
        self.current = None

    async def update_data(self, **values):
        self.data.update(values)

    async def set_state(self, state):
        self.current = state.state

    async def get_data(self):
        return dict(self.data)

    async def get_state(self):
        return self.current


class StationPresetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.json"
        self.store = AppStateStore(str(self.path))
        self.ha = FakeHa()
        self.settings = SimpleNamespace(station_player_entity="media_player.test",
                                        is_admin_user=lambda user_id: user_id == 1)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_defaults_persist_once_and_deletion_never_reseeds(self):
        original = await self.store.station_presets()
        self.assertEqual([p["title"] for p in original["presets"]],
                         ["Избранное", "Спокойная", "Энергичная"])
        self.assertTrue(self.path.exists())
        restarted = AppStateStore(str(self.path))
        self.assertEqual(await restarted.station_presets(), original)
        revision = original["revision"]
        for preset in original["presets"]:
            result = await restarted.delete_station_preset(expected_revision=revision, preset_id=preset["id"])
            revision = result["revision"]
        self.assertEqual(result["presets"], [])
        self.assertEqual((await AppStateStore(str(self.path)).station_presets())["presets"], [])

    async def test_add_limits_duplicates_revision_and_atomic_failure(self):
        original = await self.store.station_presets()
        revision = original["revision"]
        added = await self.store.add_station_preset(expected_revision=revision,
                                                    title="  Тест  ", command="  Включи тест  ")
        self.assertNotEqual(added["revision"], revision)
        self.assertEqual(added["presets"][-1]["title"], "Тест")
        self.assertEqual(added["presets"][-1]["command"], "Включи тест")
        self.assertEqual(await AppStateStore(str(self.path)).station_presets(), added)
        with self.assertRaises(AppStateRevisionConflict):
            await self.store.add_station_preset(expected_revision=revision, title="Другое", command="Команда")
        for title, command in (("тЕст", "Команда"), ("x" * 33, "Команда"),
                               ("Новая", "x" * 161), (" ", "Команда"), ("Новая", " ")):
            with self.assertRaises(ValueError):
                await self.store.add_station_preset(expected_revision=added["revision"],
                                                    title=title, command=command)
        with patch("app.services.app_state.os.replace", side_effect=OSError("secret path")):
            with self.assertRaises(AppStatePersistenceError) as caught:
                await self.store.add_station_preset(expected_revision=added["revision"],
                                                    title="Новая", command="Команда")
        self.assertNotIn("secret", str(caught.exception))
        self.assertEqual(await self.store.station_presets(), added)
        revision = added["revision"]
        for number in range(16):
            result = await self.store.add_station_preset(expected_revision=revision,
                title=f"Новая {number}", command="Команда")
            revision = result["revision"]
        self.assertEqual(len(result["presets"]), 20)
        with self.assertRaises(ValueError):
            await self.store.add_station_preset(expected_revision=revision, title="Лишняя", command="Команда")

    async def test_execute_uses_saved_command_once_and_unknown_sends_nothing(self):
        snapshot = await self.store.station_presets()
        service = StationActionService(self.ha, self.settings)
        with self.assertRaises(ValueError):
            await service.dispatch_preset("missing", self.store)
        self.assertEqual(self.ha.calls, [])
        preset = snapshot["presets"][0]
        self.assertEqual(await service.dispatch_preset(preset["id"], self.store), "dispatched")
        self.assertEqual(len(self.ha.calls), 1)
        self.assertEqual(self.ha.calls[0][2]["media_content_id"], preset["command"])
        self.ha.uncertain = True
        self.assertEqual(await service.dispatch_preset(preset["id"], self.store), "uncertain")
        self.assertEqual(len(self.ha.calls), 2)

    async def test_telegram_forged_callback_denied_and_ids_only(self):
        snapshot = await self.store.station_presets()
        markup = _music_keyboard(snapshot["presets"])
        data = [button.callback_data for row in markup.inline_keyboard for button in row]
        self.assertIn(snapshot["presets"][0]["id"], data[0])
        self.assertFalse(any(preset["command"] in item for preset in snapshot["presets"] for item in data))
        callback = SimpleNamespace(data=data[0], from_user=SimpleNamespace(id=2), answer=AsyncMock())
        await station_preset_play(callback, self.settings, self.ha, self.store)
        self.assertEqual(self.ha.calls, [])
        callback.answer.assert_awaited_once_with("Нет доступа", show_alert=True)

    async def test_duplicate_persisted_ids_fail_closed(self):
        snapshot = await self.store.station_presets()
        corrupted = json.loads(self.path.read_text())
        corrupted["station_presets"][1]["id"] = corrupted["station_presets"][0]["id"]
        self.path.write_text(json.dumps(corrupted))
        with self.assertRaises(AppStatePersistenceError):
            await AppStateStore(str(self.path)).station_presets()
        self.assertEqual(len(snapshot["presets"]), 3)

    async def test_telegram_add_requires_admin_and_explicit_confirmation(self):
        state = FakeState()
        callback = SimpleNamespace(from_user=SimpleNamespace(id=2), answer=AsyncMock(),
                                   message=SimpleNamespace(edit_text=AsyncMock()))
        await station_preset_add(callback, self.settings, state, self.store)
        self.assertIsNone(await state.get_state())
        self.assertFalse(self.path.exists())
        callback.from_user.id = 1
        await station_preset_add(callback, self.settings, state, self.store)
        self.assertEqual(await state.get_state(), PresetDraft.title.state)
        message = SimpleNamespace(from_user=SimpleNamespace(id=1), text="Новая <кнопка>", answer=AsyncMock())
        await station_preset_title(message, self.settings, state)
        self.assertEqual(await state.get_state(), PresetDraft.command.state)
        message.text = "Включи <музыку>"
        await station_preset_command(message, self.settings, state)
        self.assertEqual(await state.get_state(), PresetDraft.confirm.state)
        self.assertIn("&lt;музыку&gt;", message.answer.call_args.args[0])
        self.assertEqual(len((await self.store.station_presets())["presets"]), 3)
        await station_preset_save(callback, self.settings, state, self.store)
        self.assertEqual(len((await self.store.station_presets())["presets"]), 4)

    async def test_telegram_forged_delete_denied(self):
        snapshot = await self.store.station_presets()
        callback = SimpleNamespace(data=f"station:preset:delete:{snapshot['presets'][0]['id']}",
            from_user=SimpleNamespace(id=2), answer=AsyncMock(),
            message=SimpleNamespace(edit_text=AsyncMock()))
        await station_preset_delete_prompt(callback, self.settings, self.store, FakeState())
        self.assertEqual(len((await self.store.station_presets())["presets"]), 3)
        callback.answer.assert_awaited_once_with("Нет доступа", show_alert=True)
