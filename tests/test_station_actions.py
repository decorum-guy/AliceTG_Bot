from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app.handlers.station import show_station_menu, station_action
from app.services.home_assistant import HomeAssistantClient, HomeAssistantDispatchUncertain
from app.services.station_actions import STATION_COMMANDS, StationActionService
from app.web.internal_routes import setup_internal_routes


class FakeHa:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.uncertain = False

    async def call_service_once(self, domain: str, service: str, payload: dict) -> None:
        self.calls.append((domain, service, payload))
        if self.uncertain:
            raise HomeAssistantDispatchUncertain("uncertain")


class StationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.ha = FakeHa()
        self.settings = SimpleNamespace(
            station_player_entity="media_player.stantsiia_mini_zal",
            shortcuts_secret_token="shortcut-test-token",
            control_center_api_token="control-test-token",
        )
        app = web.Application()
        app["settings"] = self.settings
        app["ha"] = self.ha
        setup_internal_routes(app)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()

    async def test_all_reviewed_commands_and_unknown(self) -> None:
        self.assertEqual(len(STATION_COMMANDS), 7)
        service = StationActionService(self.ha, self.settings)
        for action, command in STATION_COMMANDS.items():
            self.assertEqual(await service.dispatch(action), "dispatched")
            self.assertEqual(self.ha.calls[-1], (
                "media_player", "play_media", {
                    "entity_id": "media_player.stantsiia_mini_zal",
                    "media_content_id": command,
                    "media_content_type": "command",
                },
            ))
        with self.assertRaises(ValueError):
            await service.dispatch("arbitrary")
        self.assertEqual(len(self.ha.calls), 7)

    async def test_internal_auth_and_exact_body(self) -> None:
        path = "/internal/control-center/station/action"
        body = {"action": "next", "requestId": str(uuid4())}
        self.assertEqual((await self.client.post(path, json=body)).status, 401)
        self.assertEqual((await self.client.post(path, json=body, headers={
            "Authorization": "Bearer shortcut-test-token"})).status, 403)
        headers = {"Authorization": "Bearer control-test-token"}
        for extra in ({"entity_id": "media_player.other"}, {"command": "hello"},
                      {"service": "turn_on"}, {"media_content_type": "music"}):
            self.assertEqual((await self.client.post(path, json={**body, **extra}, headers=headers)).status, 400)
        self.assertEqual((await self.client.post(path, json={**body, "action": "bad"}, headers=headers)).status, 400)
        response = await self.client.post(path, json=body, headers=headers)
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["status"], "dispatched")
        self.assertEqual(len(self.ha.calls), 1)

    async def test_shortcut_only_play_pause(self) -> None:
        path = "/shortcut/station"
        self.assertEqual((await self.client.post(path, json={"action": "play"})).status, 401)
        self.assertEqual((await self.client.post(path, json={"action": "play"}, headers={
            "Authorization": "Bearer control-test-token"})).status, 403)
        headers = {"Authorization": "Bearer shortcut-test-token"}
        for action in ("next", "like", "bad"):
            self.assertEqual((await self.client.post(path, json={"action": action}, headers=headers)).status, 400)
        self.assertEqual((await self.client.post(path, json={"action": "play", "entity_id": "x"}, headers=headers)).status, 400)
        for action in ("play", "pause"):
            response = await self.client.post(path, json={"action": action}, headers=headers)
            self.assertEqual(response.status, 200)
            self.assertEqual((await response.json())["status"], "dispatched")
        self.assertEqual(len(self.ha.calls), 2)

    async def test_uncertain_routes_do_not_claim_success(self) -> None:
        self.ha.uncertain = True
        response = await self.client.post("/shortcut/station", json={"action": "pause"},
                                          headers={"Authorization": "Bearer shortcut-test-token"})
        self.assertEqual(response.status, 202)
        self.assertFalse((await response.json())["ok"])
        response = await self.client.post("/internal/control-center/station/action",
                                          json={"action": "next", "requestId": str(uuid4())},
                                          headers={"Authorization": "Bearer control-test-token"})
        self.assertEqual(response.status, 202)
        self.assertEqual((await response.json())["status"], "uncertain")

    async def test_telegram_admin_only_and_same_service(self) -> None:
        callback = SimpleNamespace(data="station:play", from_user=SimpleNamespace(id=5),
                                   answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock()))
        settings = SimpleNamespace(**vars(self.settings), is_admin_user=lambda user: user == 1)
        await show_station_menu(callback, settings)
        await station_action(callback, settings, self.ha)
        self.assertEqual(len(self.ha.calls), 0)
        callback.from_user.id = 1
        await show_station_menu(callback, settings)
        for action, command in STATION_COMMANDS.items():
            callback.data = f"station:{action}"
            await station_action(callback, settings, self.ha)
            self.assertEqual(self.ha.calls[-1][2]["media_content_id"], command)
        self.assertEqual(len(self.ha.calls), 7)

    async def test_mutation_transport_is_single_attempt(self) -> None:
        ha = HomeAssistantClient("http://example.invalid", "test")
        class BrokenSession:
            closed = False
            def __init__(self): self.calls = 0
            def request(self, *args, **kwargs):
                self.calls += 1
                raise aiohttp.ClientConnectionError("uncertain")
        await ha.close()
        broken = BrokenSession()
        ha._session = broken
        result = await StationActionService(ha, self.settings).dispatch("next")
        self.assertEqual(result, "uncertain")
        self.assertEqual(broken.calls, 1)
