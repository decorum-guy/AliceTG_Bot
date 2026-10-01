from __future__ import annotations

import asyncio
import json
import logging
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.services.home_assistant import HomeAssistantDispatchUncertain, HomeAssistantError
from app.services.kettle_shortcut import KettleShortcutError, KettleShortcutService
from app.web.internal_routes import _kettle_shortcut_body
import test_kettle_shortcut as helpers


class KettleStopShortcutTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = helpers.KettleShortcutTests.asyncSetUp
    asyncTearDown = helpers.KettleShortcutTests.asyncTearDown

    async def stop(self):
        return await self.client.post("/shortcut/kettle", json={"action": "stop"}, headers=self.auth)

    async def assert_failure(self, response, code, status):
        self.assertEqual(response.status, status)
        self.assertEqual(await response.json(), {
            "ok": False, "error": code, "message": "Не удалось остановить чайник",
        })

    def active(self):
        self.ha.current = helpers.kettle_state("on", "on", 80)

    async def test_exact_stop_and_already_stopped_have_zero_mutations(self):
        for target in (None, 80, 100):
            self.ha.current = helpers.kettle_state(target=target)
            response = await self.stop()
            self.assertEqual(response.status, 200)
            self.assertEqual(await response.json(), {
                "ok": True, "action": "stop", "status": "already_stopped",
                "message": "Чайник уже выключен",
            })
            self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(self.ha.calls, [])
        self.assertEqual(self.ha.reads, [helpers.TARGET] * 3)

    async def test_stop_auth_boundary_precedes_reads_and_mutations(self):
        for token, status in ((None, 401), ("Basic x", 401), ("Bearer wrong", 403),
                              ("Bearer control-test-token", 403), ("Bearer неправильный", 403)):
            response = await self.client.post("/shortcut/kettle", json={"action": "stop"},
                headers={"Authorization": token} if token else {})
            self.assertEqual(response.status, status)
            self.assertEqual((await response.json())["error"], "unauthorized")
        self.settings.shortcuts_secret_token = ""
        self.assertEqual((await self.stop()).status, 503)
        self.assertEqual(self.ha.reads, [])
        self.assertEqual(self.ha.calls, [])

    async def test_stop_rejects_extra_keys_and_all_injections(self):
        for key in ("extra", "entity", "entity_id", "entityId", "service", "domain", "mode",
                    "operation_mode", "temperature", "payload", "command", "requestId"):
            for value in ("injected", None):
                with self.subTest(key=key, value=value):
                    response = await self.client.post("/shortcut/kettle",
                        json={"action": "stop", key: value}, headers=self.auth)
                    self.assertEqual(response.status, 400)
                    self.assertEqual((await response.json())["error"], "invalid_action")
        self.assertEqual(self.ha.reads, [])
        self.assertEqual(self.ha.calls, [])

    async def test_stop_parser_rejects_duplicates_unknown_actions_and_oversize(self):
        for raw in ('{"action":"stop","action":"stop"}', '{"action":"boil","action":"stop"}',
                    '{"action":"stop","action":"boil"}', '{"action":"STOP"}',
                    '{"action":"turn_off"}', '{"action":"off"}', '{"action":["stop"]}',
                    '{"action":{"stop":true}}', '{"action":"stop"} trailing',
                    '{"action":"stop"}' + ' ' * 513):
            response = await self.client.post("/shortcut/kettle", data=raw,
                headers={**self.auth, "Content-Type": "application/json"})
            self.assertEqual(response.status, 400)
            self.assertEqual((await response.json())["error"], "invalid_action")
        for content_type in ("text/plain", "text/json", "application/x-www-form-urlencoded"):
            response = await self.client.post("/shortcut/kettle", data='{"action":"stop"}',
                headers={**self.auth, "Content-Type": content_type})
            self.assertEqual(response.status, 400)
        self.assertEqual(self.ha.reads, [])
        self.assertEqual(self.ha.calls, [])

    async def test_stop_chunked_body_is_fully_validated(self):
        async def body(extra=False):
            yield b'{"action":"stop"'
            await asyncio.sleep(0)
            yield b',"operation_mode":"off"}' if extra else b'}'
        response = await self.client.post("/shortcut/kettle", data=body(True),
            headers={**self.auth, "Content-Type": "application/json"})
        self.assertEqual(response.status, 400)
        response = await self.client.post("/shortcut/kettle", data=body(),
            headers={**self.auth, "Content-Type": "application/json; charset=utf-8"})
        self.assertEqual(response.status, 200)
        self.assertEqual(self.ha.calls, [])

    async def test_parser_preserves_read_limit_timeout_and_bounded_action(self):
        for action in ("boil", "stop"):
            raw = json.dumps({"action": action}).encode()
            read = AsyncMock(side_effect=asyncio.IncompleteReadError(raw, 513))
            request = SimpleNamespace(content_type="application/json", content=SimpleNamespace(readexactly=read))
            self.assertEqual(await _kettle_shortcut_body(request), action)
            read.assert_awaited_once_with(513)
        read.side_effect = asyncio.TimeoutError()
        self.assertIsNone(await _kettle_shortcut_body(request))

    async def test_invalid_stop_target_fails_before_ha(self):
        for target in (None, 100, "", "switch.kettle", "water_heater.", "water_heater.kettle/other",
                       "water_heater.Kettle", "water_heater.kettle\n", "water_heater.чайник"):
            self.settings.kettle_entity = target
            await self.assert_failure(await self.stop(), "kettle_invalid_target", 503)
        self.assertEqual(self.ha.reads, [])
        self.assertEqual(self.ha.calls, [])

    async def test_active_stop_sends_one_fixed_mutation_and_needs_no_target(self):
        for target in (None, 0, 80, 100, "absent"):
            with self.subTest(target=target):
                self.ha.reset()
                before = helpers.kettle_state("on", "on", 80)
                after = helpers.kettle_state(target=target, updated=helpers.AFTER_ON)
                if target == "absent":
                    after["attributes"].pop("temperature")
                self.ha.scripted = [before, after]
                response = await self.stop()
                self.assertEqual(response.status, 200)
                self.assertEqual(await response.json(), {
                    "ok": True, "action": "stop", "status": "stopped", "message": "Чайник остановлен",
                })
                self.assertEqual(self.ha.calls, [("water_heater", "set_operation_mode",
                    {"entity_id": helpers.TARGET, "operation_mode": "off"})])
                self.assertEqual(self.ha.events, [("read", helpers.TARGET),
                    ("command", "set_operation_mode"), ("read", helpers.TARGET)])

    async def test_wrong_state_or_mode_cannot_confirm_stop(self):
        for state, mode in (("off", "on"), ("on", "off"), ("on", "on"), ("off", "green_tea")):
            with self.subTest(state=state, mode=mode):
                self.ha.reset()
                self.clock.value = 0
                self.ha.scripted = [helpers.kettle_state("on", "on"),
                    helpers.kettle_state(state, mode, updated=helpers.AFTER_ON)]
                await self.assert_failure(await self.stop(), "ha_verification_timeout", 504)
                self.assertEqual(self.clock.value, 5.0)
                self.assertEqual(len(self.ha.calls), 1)

    async def test_same_older_missing_naive_malformed_stop_timestamp_cannot_confirm(self):
        for updated in (helpers.BEFORE, "2026-09-30T18:15:53Z", None, "bad",
                        "2026-09-30T18:15:56", "9999-12-31T23:59:59-12:00"):
            with self.subTest(updated=updated):
                self.ha.reset()
                self.clock.value = 0
                after = helpers.kettle_state(updated=updated)
                if updated is None:
                    after.pop("last_updated")
                self.ha.scripted = [helpers.kettle_state("on", "on"), after]
                await self.assert_failure(await self.stop(), "ha_verification_timeout", 504)
                self.assertEqual(self.clock.value, 5.0)
                self.assertEqual(len(self.ha.calls), 1)

    async def test_invalid_precommand_stop_timestamp_never_mutates(self):
        for updated in (None, "bad", "2026-09-30T18:15:54"):
            self.ha.current = helpers.kettle_state("on", "on", updated=updated)
            await self.assert_failure(await self.stop(), "kettle_invalid_state", 503)
        self.assertEqual(self.ha.calls, [])

    async def test_stop_uses_only_ha_clock_domain_with_later_process_wall_clock(self):
        self.ha.scripted = [helpers.kettle_state("on", "on"),
            helpers.kettle_state(updated=helpers.AFTER_ON)]
        service = KettleShortcutService(self.ha, self.settings, clock=self.clock, sleep=self.clock.sleep,
            utcnow=lambda: helpers.STARTED.replace(year=2027))
        self.assertEqual(await service.stop(), "stopped")
        self.assertEqual(len(self.ha.calls), 1)

    async def test_delayed_stop_propagation_uses_bounded_polling_without_retry(self):
        self.ha.scripted = [helpers.kettle_state("on", "on"), helpers.kettle_state("on", "on"),
            helpers.kettle_state(updated=helpers.BEFORE), helpers.kettle_state(updated=helpers.AFTER_ON)]
        self.assertEqual((await (await self.stop()).json())["status"], "stopped")
        self.assertEqual(self.clock.sleeps, [0.25, 0.25])
        self.assertEqual(len(self.ha.calls), 1)

    async def test_stop_command_failure_uncertainty_and_timeout_are_not_retried(self):
        for error, code in ((HomeAssistantError("private-upstream-body"), "kettle_command_failed"),
                           (HomeAssistantDispatchUncertain("private-upstream-body"), "kettle_dispatch_uncertain"),
                           (asyncio.TimeoutError(), "kettle_dispatch_uncertain"),
                           (RuntimeError("private-upstream-body"), "kettle_command_failed")):
            self.ha.reset()
            self.active()
            self.ha.failures["set_operation_mode"] = error
            await self.assert_failure(await self.stop(), code, 502)
            self.assertEqual(len(self.ha.calls), 1)

    async def test_stop_errors_keep_private_upstream_details_out_of_responses_and_logs(self):
        private = "ha-test-secret http://private.invalid/api water_heater.private_entity raw-payload"
        for stage in ("initial_read", "set_operation_mode", "verification_read"):
            self.ha.reset()
            self.active()
            error = HomeAssistantError(private, status=500, body=private)
            if stage == "initial_read":
                self.ha.scripted = [error]
            elif stage == "verification_read":
                self.ha.scripted = [self.ha.current, error]
            else:
                self.ha.failures[stage] = error
            with self.assertLogs("app", level=logging.INFO) as logs:
                response = await self.stop()
            await self.assert_failure(response,
                "kettle_command_failed" if stage == "set_operation_mode" else "home_assistant_unavailable",
                502 if stage == "set_operation_mode" else 503)
            output = "\n".join(logs.output) + await response.text()
            for value in (private, "ha-test-secret", "http://private.invalid", "water_heater.private_entity",
                          "shortcut-test-token", "control-test-token", "Traceback"):
                self.assertNotIn(value, output)
            self.assertTrue(all(record.exc_info is None for record in logs.records))
            self.assertLessEqual(len(self.ha.calls), 1)

    async def test_unknown_or_unavailable_stop_readback_never_claims_success(self):
        for state in (None, {}, helpers.kettle_state("unknown"), helpers.kettle_state("unavailable")):
            self.ha.reset()
            self.ha.scripted = [helpers.kettle_state("on", "on"), state]
            await self.assert_failure(await self.stop(), "kettle_invalid_state", 503)
            self.assertEqual(len(self.ha.calls), 1)

    async def test_blocked_stop_verification_is_cancelled_at_deadline(self):
        self.active()
        calls = 0
        cancelled = False
        original = self.ha.get_state_once

        async def read(entity):
            nonlocal calls, cancelled
            calls += 1
            if calls == 2:
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled = True
            return await original(entity)

        self.ha.get_state_once = read
        service = KettleShortcutService(self.ha, self.settings, verification_timeout=0.03)
        with self.assertRaises(KettleShortcutError) as failure:
            await service.stop()
        self.assertEqual(failure.exception.code, "ha_verification_timeout")
        self.assertTrue(cancelled)
        self.assertEqual(len(self.ha.calls), 1)
