from __future__ import annotations

import asyncio
import copy
import hmac
import logging
import os
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app.config import Settings
from app.services.home_assistant import (
    HomeAssistantClient,
    HomeAssistantDispatchUncertain,
    HomeAssistantError,
)
from app.services.kettle_shortcut import KettleShortcutError, KettleShortcutService
from app.web.internal_routes import setup_internal_routes


BEFORE = "2026-09-30T18:15:54+00:00"
STARTED = datetime(2026, 9, 30, 18, 15, 55, tzinfo=timezone.utc)
AFTER_TARGET = "2026-09-30T18:15:55.100000+00:00"
AFTER_ON = "2026-09-30T18:15:55.200000+00:00"
TARGET = "water_heater.shortcut_configured_kettle"


def kettle_state(state="off", operation="off", target=80, updated=BEFORE) -> dict:
    # YandexStation plain-boil read-back uses on/on/100, never a symbolic tea label.
    return {"state": state, "attributes": {"operation_mode": operation,
            "temperature": target, "current_temperature": 25},
            "last_updated": updated, "last_changed": BEFORE}


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.value

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


class FakeHa:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.current = kettle_state()
        self.scripted: list = []
        self.calls: list[tuple[str, str, dict]] = []
        self.reads: list[str] = []
        self.events: list = []
        self.failures: dict[str, Exception] = {}

    async def get_state_once(self, entity_id: str) -> dict | None:
        self.reads.append(entity_id)
        self.events.append(("read", entity_id))
        result = self.scripted[0] if self.scripted else self.current
        if len(self.scripted) > 1:
            self.scripted.pop(0)
        if isinstance(result, Exception):
            raise result
        return copy.deepcopy(result)

    async def call_service_once(self, domain: str, service: str, payload: dict) -> None:
        self.calls.append((domain, service, payload))
        self.events.append(("command", service))
        if service in self.failures:
            raise self.failures[service]
        if service == "set_temperature":
            self.current["attributes"]["temperature"] = payload["temperature"]
            self.current["last_updated"] = AFTER_TARGET
        elif service == "set_operation_mode":
            self.current["state"] = "on"
            self.current["attributes"]["operation_mode"] = payload["operation_mode"]
            self.current["last_updated"] = AFTER_ON


class KettleShortcutTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.ha = FakeHa()
        self.clock = FakeClock()
        self.settings = SimpleNamespace(kettle_entity=TARGET,
            shortcuts_secret_token="shortcut-test-token",
            control_center_api_token="control-test-token")
        self.auth = {"Authorization": "Bearer shortcut-test-token"}
        self.factory = patch("app.web.internal_routes.KettleShortcutService",
            side_effect=lambda ha, settings: KettleShortcutService(
                ha, settings, clock=self.clock, sleep=self.clock.sleep, utcnow=lambda: STARTED))
        self.factory.start()
        self.addCleanup(self.factory.stop)
        app = web.Application()
        app["settings"] = self.settings
        app["ha"] = self.ha
        setup_internal_routes(app)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()

    async def boil(self):
        return await self.client.post("/shortcut/kettle", json={"action": "boil"}, headers=self.auth)

    async def assert_failure(self, response, code: str, status: int) -> None:
        self.assertEqual(response.status, status)
        self.assertEqual(await response.json(),
            {"ok": False, "error": code, "message": "Не удалось включить чайник"})

    async def test_post_only_and_no_generic_ha_route(self) -> None:
        for method in ("GET", "HEAD", "PUT", "PATCH", "DELETE", "OPTIONS"):
            with self.subTest(method=method):
                response = await self.client.request(method, "/shortcut/kettle", headers=self.auth)
                self.assertEqual(response.status, 405)
        self.assertEqual((await self.client.post("/shortcut/home-assistant", headers=self.auth)).status, 404)
        self.assertEqual(self.ha.reads, [])
        self.assertEqual(self.ha.calls, [])

    async def test_shortcut_auth_is_separate_and_precedes_ha(self) -> None:
        for token, expected in ((None, 401), ("Basic x", 401), ("Bearer wrong", 403),
                ("Bearer control-test-token", 403), ("Bearer ", 403),
                ("Bearer неправильный", 403)):
            with self.subTest(token=token):
                response = await self.client.post("/shortcut/kettle", json={"action": "boil"},
                    headers={"Authorization": token} if token is not None else {})
                self.assertEqual(response.status, expected)
                self.assertEqual((await response.json())["error"], "unauthorized")
        self.assertEqual(self.ha.reads, [])
        self.assertEqual(self.ha.calls, [])

    async def test_empty_shortcut_secret_disables_route(self) -> None:
        self.settings.shortcuts_secret_token = ""
        response = await self.boil()
        self.assertEqual(response.status, 503)
        self.assertFalse((await response.json())["ok"])
        self.assertEqual(self.ha.reads, [])
        self.assertEqual(self.ha.calls, [])

    async def test_correct_bearer_uses_existing_constant_time_check(self) -> None:
        with patch("app.web.internal_routes.hmac.compare_digest", wraps=hmac.compare_digest) as compare:
            response = await self.boil()
        self.assertEqual(response.status, 200)
        compare.assert_called_once_with("shortcut-test-token", "shortcut-test-token")

    async def test_json_only_malformed_and_exact_action(self) -> None:
        for content_type in ("text/plain", "application/x-www-form-urlencoded", "text/json"):
            response = await self.client.post("/shortcut/kettle", data='{"action":"boil"}',
                headers={**self.auth, "Content-Type": content_type})
            self.assertEqual(response.status, 400)
        for raw in (b"", b"{", b"\xff", b"{}", b"[]", b"null", b'"boil"',
                b'{"action":null}', b'{"action":true}', b'{"action":100}',
                b'{"action":"play"}', b'{"action":"turn_on"}', b'{"action":"green_tea"}',
                b'{"action":"boil","action":"boil"}', b'{"action":"boil"} garbage',
                b'{"action":"boil"}' + b" " * 513):
            with self.subTest(raw=raw):
                response = await self.client.post("/shortcut/kettle", data=raw,
                    headers={**self.auth, "Content-Type": "application/json"})
                self.assertEqual(response.status, 400)
                self.assertEqual((await response.json())["error"], "invalid_action")
        self.assertEqual(self.ha.reads, [])
        self.assertEqual(self.ha.calls, [])

    async def test_all_injection_keys_are_rejected(self) -> None:
        for key in ("entity", "entity_id", "entityId", "temperature", "service", "domain",
                    "operation_mode", "mode", "payload", "command", "requestId", "extra"):
            with self.subTest(key=key):
                response = await self.client.post("/shortcut/kettle",
                    json={"action": "boil", key: "injected"}, headers=self.auth)
                self.assertEqual(response.status, 400)
                self.assertEqual((await response.json())["error"], "invalid_action")
        self.assertEqual(self.ha.reads, [])
        self.assertEqual(self.ha.calls, [])

    async def test_chunked_body_is_fully_validated(self) -> None:
        async def injected_body():
            yield b'{"action":"boil",'
            await asyncio.sleep(0)
            yield b'"temperature":80}'
        response = await self.client.post("/shortcut/kettle", data=injected_body(),
            headers={**self.auth, "Content-Type": "application/json"})
        self.assertEqual(response.status, 400)
        self.assertEqual(self.ha.calls, [])

        async def valid_body():
            yield b'{"action":'
            await asyncio.sleep(0)
            yield b'"boil"}'
        response = await self.client.post("/shortcut/kettle", data=valid_body(),
            headers={**self.auth, "Content-Type": "application/json; charset=utf-8"})
        self.assertEqual(response.status, 200)

    async def test_invalid_configured_target_fails_closed(self) -> None:
        for entity in (None, 100, "", "switch.kettle", "climate.kettle", "water_heater.",
                "water_heater.kettle/other", "water_heater.kettle?x=1", "water_heater.Kettle",
                "water_heater.kettle\n", "water_heater.kettle.other", "water_heater.чайник"):
            with self.subTest(entity=entity):
                self.settings.kettle_entity = entity
                await self.assert_failure(await self.boil(), "kettle_invalid_target", 503)
        self.assertEqual(self.ha.reads, [])
        self.assertEqual(self.ha.calls, [])

    async def test_fixed_boil_uses_only_configured_target_and_fresh_reads(self) -> None:
        response = await self.boil()
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), {"ok": True, "action": "boil",
            "status": "boiling", "message": "Чайник включён"})
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(self.ha.calls, [
            ("water_heater", "set_temperature", {"entity_id": TARGET, "temperature": 100}),
            ("water_heater", "set_operation_mode", {"entity_id": TARGET, "operation_mode": "on"})])
        self.assertEqual(self.ha.reads, [TARGET] * 3)
        self.assertEqual(self.ha.events, [("read", TARGET), ("command", "set_temperature"),
            ("read", TARGET), ("command", "set_operation_mode"), ("read", TARGET)])

    async def test_already_boiling_is_idempotent_even_with_old_timestamp(self) -> None:
        self.ha.current = kettle_state("on", "on", 100.0)
        for _ in range(2):
            response = await self.boil()
            self.assertEqual(response.status, 200)
            self.assertEqual(await response.json(), {"ok": True, "action": "boil",
                "status": "already_boiling", "message": "Чайник уже включён"})
        self.assertEqual(self.ha.calls, [])
        self.assertEqual(self.ha.reads, [TARGET, TARGET])

    async def test_active_tea_temperature_is_not_already_boiling(self) -> None:
        self.ha.current = kettle_state("on", "on", 80)
        response = await self.boil()
        self.assertEqual((await response.json())["status"], "boiling")
        self.assertEqual(len(self.ha.calls), 2)

    async def test_delayed_target_and_active_state_propagation(self) -> None:
        self.ha.scripted = [kettle_state(), kettle_state(),
            kettle_state(target=100, updated=AFTER_TARGET),
            kettle_state(target=100, updated=AFTER_TARGET),
            kettle_state("on", "on", 100, BEFORE),
            kettle_state("on", "on", 100, AFTER_ON)]
        response = await self.boil()
        self.assertEqual(response.status, 200)
        self.assertEqual(self.clock.sleeps, [0.25] * 3)
        self.assertEqual(len(self.ha.calls), 2)
        self.assertEqual(len(self.ha.reads), 6)

    async def test_unconfirmed_target_never_activates_and_window_is_bounded(self) -> None:
        for target in (80, 99, "100", None, True):
            with self.subTest(target=target):
                self.ha.reset()
                self.clock.value = 0
                self.ha.scripted = [kettle_state(), kettle_state(target=target, updated=AFTER_TARGET)]
                await self.assert_failure(await self.boil(), "ha_verification_timeout", 504)
                self.assertEqual(self.clock.value, 5.0)
                self.assertEqual(len(self.ha.calls), 1)
                self.assertEqual(self.ha.calls[0][1], "set_temperature")

    async def test_http_success_and_nonoff_alone_cannot_confirm(self) -> None:
        for state, operation, target in (("off", "on", 100), ("on", "off", 100),
                ("on", "unknown", 100), ("on", "green_tea", 100), ("on", "on", 80)):
            with self.subTest(state=state, operation=operation, target=target):
                self.ha.reset()
                self.clock.value = 0
                self.ha.scripted = [kettle_state(), kettle_state(target=100, updated=AFTER_TARGET),
                    kettle_state(state, operation, target, AFTER_ON)]
                await self.assert_failure(await self.boil(), "ha_verification_timeout", 504)
                self.assertEqual(len(self.ha.calls), 2)
                self.assertEqual(self.clock.value, 5.0)

    async def test_stale_or_missing_post_command_timestamp_cannot_confirm(self) -> None:
        for updated in (BEFORE, "2026-09-30T18:15:54.900000Z", None, "bad", "2026-09-30T18:15:56",
                        "9999-12-31T23:59:59-12:00"):
            with self.subTest(updated=updated):
                self.ha.reset()
                self.clock.value = 0
                self.ha.scripted = [kettle_state(), kettle_state(target=100, updated=AFTER_TARGET),
                    kettle_state("on", "on", 100, updated)]
                await self.assert_failure(await self.boil(), "ha_verification_timeout", 504)
                self.assertEqual(len(self.ha.calls), 2)

    async def test_missing_initial_watermark_fails_before_mutation(self) -> None:
        for updated in (None, "bad", "2026-09-30T18:15:54"):
            self.ha.current["last_updated"] = updated
            await self.assert_failure(await self.boil(), "kettle_invalid_state", 503)
        self.assertEqual(self.ha.calls, [])

    async def test_unknown_unavailable_missing_or_malformed_state_fails_closed(self) -> None:
        for state in (None, {}, {"state": "off", "attributes": []},
                kettle_state("unknown"), kettle_state("unavailable"), kettle_state([])):
            with self.subTest(state=state):
                self.ha.scripted = [state]
                await self.assert_failure(await self.boil(), "kettle_invalid_state", 503)
        self.assertEqual(self.ha.calls, [])

    async def test_unavailable_after_mutation_never_claims_success(self) -> None:
        self.ha.scripted = [kettle_state(), kettle_state(target=100, updated=AFTER_TARGET),
                            kettle_state("unavailable", "on", 100, AFTER_ON)]
        await self.assert_failure(await self.boil(), "kettle_invalid_state", 503)
        self.assertEqual(len(self.ha.calls), 2)

    async def test_target_and_active_checks_share_one_five_second_window(self) -> None:
        self.ha.scripted = [kettle_state(), *([kettle_state()] * 16),
            kettle_state(target=100, updated=AFTER_TARGET),
            *([kettle_state(target=100, updated=AFTER_TARGET)] * 4),
            kettle_state("on", "on", 100, AFTER_ON)]
        await self.assert_failure(await self.boil(), "ha_verification_timeout", 504)
        self.assertEqual(self.clock.value, 5.0)
        self.assertEqual(self.clock.sleeps, [0.25] * 20)
        self.assertEqual(len(self.ha.calls), 2)

    async def test_command_failures_and_uncertainty_stop_without_retry(self) -> None:
        for service in ("set_temperature", "set_operation_mode"):
            for error, code in ((HomeAssistantError("private details"), "kettle_command_failed"),
                    (HomeAssistantDispatchUncertain("uncertain details"), "kettle_dispatch_uncertain"),
                    (asyncio.TimeoutError(), "kettle_dispatch_uncertain"),
                    (RuntimeError("private runtime details"), "kettle_command_failed")):
                with self.subTest(service=service, error=type(error).__name__):
                    self.ha.reset()
                    self.ha.failures[service] = error
                    await self.assert_failure(await self.boil(), code, 502)
                    self.assertEqual(len(self.ha.calls), 1 if service == "set_temperature" else 2)

    async def test_ha_unavailable_is_bounded_and_private_details_never_escape(self) -> None:
        private = "ha-test-secret http://private.invalid/api water_heater.private_entity raw-payload"
        for stage in ("read", "set_temperature", "set_operation_mode"):
            with self.subTest(stage=stage):
                self.ha.reset()
                error = HomeAssistantError(private, status=500, body=private)
                if stage == "read":
                    self.ha.scripted = [error]
                else:
                    self.ha.failures[stage] = error
                with self.assertLogs("app", level=logging.INFO) as logs:
                    response = await self.boil()
                await self.assert_failure(response,
                    "home_assistant_unavailable" if stage == "read" else "kettle_command_failed",
                    503 if stage == "read" else 502)
                output = "\n".join(logs.output) + await response.text()
                for secret in (private, "ha-test-secret", "http://private.invalid",
                        "water_heater.private_entity", "shortcut-test-token", "control-test-token", "Traceback"):
                    self.assertNotIn(secret, output)
                self.assertTrue(all(record.exc_info is None for record in logs.records))

    async def test_blocked_verification_read_is_cancelled_at_deadline(self) -> None:
        calls = 0
        cancelled = False
        original = self.ha.get_state_once

        async def blocked_read(entity):
            nonlocal calls, cancelled
            calls += 1
            if calls == 3:
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled = True
            return await original(entity)

        self.ha.get_state_once = blocked_read
        service = KettleShortcutService(self.ha, self.settings, verification_timeout=0.03,
                                       utcnow=lambda: STARTED)
        with self.assertRaises(KettleShortcutError) as caught:
            await service.boil()
        self.assertEqual(caught.exception.code, "ha_verification_timeout")
        self.assertEqual(len(self.ha.calls), 2)
        self.assertTrue(cancelled)

    async def test_blocked_activation_is_uncertain_and_not_retried(self) -> None:
        cancelled = False
        original = self.ha.call_service_once

        async def blocked_command(domain, service, payload):
            nonlocal cancelled
            if service == "set_operation_mode":
                self.ha.calls.append((domain, service, payload))
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled = True
            else:
                await original(domain, service, payload)

        self.ha.call_service_once = blocked_command
        service = KettleShortcutService(self.ha, self.settings, verification_timeout=0.03,
                                       utcnow=lambda: STARTED)
        with self.assertRaises(KettleShortcutError) as caught:
            await service.boil()
        self.assertEqual(caught.exception.code, "kettle_dispatch_uncertain")
        self.assertEqual(len(self.ha.calls), 2)
        self.assertTrue(cancelled)


class KettleConfigurationTests(unittest.TestCase):
    def test_kettle_entity_environment_override_preserves_default_and_fails_closed(self) -> None:
        baseline = {"TELEGRAM_BOT_TOKEN": "test", "TELEGRAM_WEBHOOK_SECRET": "test",
            "TELEGRAM_ALLOWED_USER_IDS": "1", "TELEGRAM_ADMIN_CHAT_ID": "1",
            "HA_LONG_LIVED_TOKEN": "test", "INTERNAL_WEBHOOK_SECRET": "test"}
        with patch.dict(os.environ, baseline, clear=True):
            self.assertEqual(Settings.from_env().kettle_entity, Settings.kettle_entity)
        for configured in (TARGET, "switch.invalid", ""):
            with patch.dict(os.environ, {**baseline, "KETTLE_ENTITY": configured}, clear=True):
                self.assertEqual(Settings.from_env().kettle_entity, configured)


class HomeAssistantShortcutReadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.ha = HomeAssistantClient("http://private.example.invalid", "ha-test-secret")
        await self.ha.close()
        self.ha._create_session = MagicMock(side_effect=AssertionError("must not recreate or retry"))
        self.response = SimpleNamespace(status=200, json=AsyncMock(return_value=kettle_state()),
            text=AsyncMock(return_value="ha-test-secret raw payload private URL"))
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=self.response)
        context.__aexit__ = AsyncMock(return_value=False)
        self.session = SimpleNamespace(closed=False, request=MagicMock(return_value=context))
        self.ha._session = self.session

    async def test_fresh_get_has_bounded_timeout_and_does_not_cache(self) -> None:
        await self.ha.get_state_once(TARGET)
        await self.ha.get_state_once(TARGET)
        self.assertEqual(self.session.request.call_count, 2)
        for call in self.session.request.call_args_list:
            self.assertEqual(call.args, ("GET", f"http://private.example.invalid/api/states/{TARGET}"))
            self.assertEqual(call.kwargs["timeout"].total, 5)
        self.ha._create_session.assert_not_called()

    async def test_http_error_never_reads_or_logs_raw_body(self) -> None:
        self.response.status = 500
        with self.assertNoLogs("app.services.home_assistant", level=logging.DEBUG):
            with self.assertRaises(HomeAssistantError) as caught:
                await self.ha.get_state_once(TARGET)
        self.assertEqual(caught.exception.status, 500)
        self.assertIsNone(caught.exception.body)
        self.assertEqual(str(caught.exception), "Home Assistant read failed")
        self.response.text.assert_not_awaited()
        self.response.json.assert_not_awaited()
        self.assertEqual(self.session.request.call_count, 1)

    async def test_transport_errors_are_sanitized_and_not_retried(self) -> None:
        for error in (aiohttp.ClientConnectionError("ha-test-secret private URL"),
                      asyncio.TimeoutError(), RuntimeError("Session is closed ha-test-secret")):
            self.session.request.reset_mock()
            self.session.request.side_effect = error
            with self.assertNoLogs("app.services.home_assistant", level=logging.DEBUG):
                with self.assertRaises(HomeAssistantError) as caught:
                    await self.ha.get_state_once(TARGET)
            self.assertEqual(str(caught.exception), "Home Assistant read failed")
            self.assertEqual(self.session.request.call_count, 1)
        self.ha._create_session.assert_not_called()

    async def test_not_found_closed_session_and_invalid_json_are_bounded(self) -> None:
        self.response.status = 404
        self.assertIsNone(await self.ha.get_state_once(TARGET))
        self.response.json.assert_not_awaited()
        self.session.closed = True
        with self.assertRaises(HomeAssistantError):
            await self.ha.get_state_once(TARGET)
        self.assertEqual(self.session.request.call_count, 1)
        self.session.closed = False
        self.response.status = 200
        self.response.json.side_effect = ValueError("ha-test-secret raw payload")
        with self.assertRaises(HomeAssistantError) as caught:
            await self.ha.get_state_once(TARGET)
        self.assertNotIn("ha-test-secret", str(caught.exception))
        self.response.json.side_effect = None
        self.response.json.return_value = []
        with self.assertRaises(HomeAssistantError):
            await self.ha.get_state_once(TARGET)
