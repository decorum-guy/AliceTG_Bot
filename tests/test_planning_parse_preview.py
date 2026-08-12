from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
import urllib.request
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app.planning import MutationContext, PlanningDatabase
from app.planning.alice import AliceInterpretationService
from app.planning.api.routes import PLANNING_PREFIX, setup_planning_routes
from app.planning.api.service import PlanningApiService


PANEL_SECRET = "synthetic-preview-panel-secret"
HA_SECRET = "synthetic-preview-ha-secret"
OPERATOR_SECRET = "synthetic-preview-operator-secret"
INTERNAL_SECRET = "synthetic-preview-internal-secret"
REFERENCE = "2026-08-12T09:00:00Z"
NOW = REFERENCE
SNAPSHOT_TABLES = (
    "reminders",
    "tasks",
    "calendar_events",
    "projects",
    "outbox",
    "audit_events",
    "idempotency_keys",
)


class PlanningParsePreviewHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "planning.sqlite3"
        self.database = PlanningDatabase(self.db_path)
        self.service = PlanningApiService(self.database, now_fn=lambda: NOW)
        self.app = web.Application()
        self.app["settings"] = SimpleNamespace(
            planning_api_enabled=True,
            internal_webhook_secret=INTERNAL_SECRET,
            planning_ha_secret=HA_SECRET,
            planning_panel_agent_secret=PANEL_SECRET,
            planning_operator_secret=OPERATOR_SECRET,
            planning_api_rate_limit_per_minute=120,
            planning_api_stale_after_seconds=300,
            planning_default_timezone="Europe/Moscow",
        )
        self.app["planning_database"] = self.database
        self.app["planning_api_service"] = self.service
        setup_planning_routes(self.app)
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        self.database.close()
        self.temp.cleanup()

    @staticmethod
    def _headers(audience: str = "panel-agent", secret: str = PANEL_SECRET) -> dict[str, str]:
        return {
            "X-Internal-Secret": INTERNAL_SECRET,
            "X-Planning-Audience": audience,
            "X-Planning-Secret": secret,
        }

    def _body(self, **overrides: object) -> dict[str, object]:
        body: dict[str, object] = {
            "text": "завтра в 15:00 напомни позвонить врачу",
            "reference_time_utc": REFERENCE,
            "timezone": "Europe/Moscow",
            "locale": "ru-RU",
        }
        body.update(overrides)
        return body

    async def _post(
        self,
        body: object,
        *,
        audience: str = "panel-agent",
        secret: str = PANEL_SECRET,
        auth: bool = True,
    ):
        return await self.client.post(
            f"{PLANNING_PREFIX}/parse",
            headers=self._headers(audience, secret) if auth else {},
            json=body,
        )

    def _snapshot(self) -> dict[str, list[tuple[object, ...]]]:
        return {
            table: [
                tuple(row)
                for row in self.database.connection.execute(
                    f"SELECT * FROM {table} ORDER BY rowid"
                ).fetchall()
            ]
            for table in SNAPSHOT_TABLES
        }

    def _seed_existing_planning_state(self) -> None:
        context = MutationContext(
            audience="panel-agent",
            actor_id="preview-fixture",
            actor_type="service",
            surface="panel-agent",
            correlation_id="2b7d8c9e-5f21-4a63-8c7d-1e9b5a2f4c60",
        ).validate()
        repository = self.service.repository
        project = repository.create_project(name="Existing project", context=context)
        repository.create_reminder(
            title="Existing reminder",
            due_at_utc="2026-08-14T07:30:00Z",
            timezone="Europe/Moscow",
            context=context,
        )
        repository.create_task(
            title="Existing task",
            due_date="2026-08-14",
            priority="normal",
            project_id=project.id,
            context=context,
        )
        repository.create_calendar_event(
            title="Existing event",
            all_day=False,
            timezone="Europe/Moscow",
            start_at_utc="2026-08-15T10:00:00Z",
            end_at_utc="2026-08-15T11:00:00Z",
            context=context,
        )

    async def test_panel_agent_auth_succeeds_and_response_is_strict_preview_envelope(self) -> None:
        response = await self._post(self._body())
        payload = await response.json()

        self.assertEqual(response.status, 200)
        self.assertEqual(
            set(payload),
            {
                "schemaVersion",
                "kind",
                "candidate",
                "confidence",
                "ambiguities",
                "requires_confirmation",
                "normalized_text",
                "error_code",
                "correlation_id",
            },
        )
        self.assertEqual(payload["schemaVersion"], "planning.v1")
        self.assertEqual(payload["kind"], "parse_preview")
        self.assertEqual(payload["confidence"], "high")
        self.assertFalse(payload["requires_confirmation"])
        self.assertEqual(payload["ambiguities"], [])
        self.assertIsNone(payload["error_code"])
        self.assertEqual(uuid.UUID(payload["correlation_id"]).version, 4)
        self.assertEqual(payload["candidate"]["domain"], "reminder")
        self.assertEqual(payload["candidate"]["operation"], "create")
        self.assertIsInstance(payload["candidate"]["fields"]["due_at_utc"], str)
        self.assertIsInstance(payload["candidate"]["fields"]["timezone"], str)
        self.assertIsInstance(payload["candidate"]["normalized_paraphrase"], str)
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    async def test_wrong_audience_and_secret_fail_closed(self) -> None:
        wrong_secret = await self._post(self._body(), secret="wrong-secret")
        wrong_audience = await self._post(self._body(), audience="ha", secret=HA_SECRET)
        operator = await self._post(self._body(), audience="operator", secret=OPERATOR_SECRET)

        self.assertEqual(wrong_secret.status, 401)
        self.assertEqual((await wrong_secret.json())["error"]["code"], "authentication_failed")
        self.assertEqual(wrong_audience.status, 403)
        self.assertEqual((await wrong_audience.json())["error"]["code"], "audience_forbidden")
        self.assertEqual(operator.status, 403)
        self.assertEqual((await operator.json())["error"]["code"], "audience_forbidden")

    async def test_missing_auth_fails(self) -> None:
        response = await self._post(self._body(), auth=False)
        self.assertEqual(response.status, 401)
        self.assertEqual((await response.json())["error"]["code"], "authentication_failed")

    async def test_unknown_and_structural_request_fields_are_rejected(self) -> None:
        cases = (
            {"service": "light.turn_on"},
            {"url": "https://example.com"},
            {"path": "/etc/passwd"},
            {"command": "rm -rf /"},
            {"actor": "browser"},
            {"source": "panel-agent"},
            {"audience": "panel-agent"},
            {"parser_module": "app.planning.parser"},
        )
        for index, extra in enumerate(cases):
            response = await self._post(self._body(**extra))
            with self.subTest(index=index, field=next(iter(extra))):
                self.assertEqual(response.status, 400)
                self.assertEqual((await response.json())["error"]["code"], "validation_error")

    async def test_duplicate_json_key_is_rejected(self) -> None:
        raw = (
            b'{"text":"first","text":"second",'
            b'"reference_time_utc":"2026-08-12T09:00:00Z",'
            b'"timezone":"Europe/Moscow"}'
        )
        response = await self.client.post(
            f"{PLANNING_PREFIX}/parse",
            headers={**self._headers(), "Content-Type": "application/json"},
            data=raw,
        )
        self.assertEqual(response.status, 400)
        self.assertEqual((await response.json())["error"]["code"], "malformed_json")

    async def test_malformed_reference_timestamp_is_rejected(self) -> None:
        for value in (
            "2026-08-12T09:00:00+03:00",
            "2026-08-12T09:00:00",
            "not-a-timestamp",
        ):
            response = await self._post(self._body(reference_time_utc=value))
            with self.subTest(value=value):
                self.assertEqual(response.status, 400)
                self.assertEqual((await response.json())["error"]["code"], "validation_error")

    async def test_invalid_iana_timezone_is_rejected(self) -> None:
        response = await self._post(self._body(timezone="Mars/Phobos"))
        self.assertEqual(response.status, 400)
        self.assertEqual((await response.json())["error"]["code"], "validation_error")

    async def test_text_and_locale_bounds_are_rejected(self) -> None:
        oversized = await self._post(self._body(text="x" * 2001))
        invalid_locale = await self._post(self._body(locale="en-US"))
        oversized_locale = await self._post(self._body(locale="x" * 17))

        self.assertEqual(oversized.status, 400)
        self.assertEqual(invalid_locale.status, 400)
        self.assertEqual(oversized_locale.status, 400)
        for response in (oversized, invalid_locale, oversized_locale):
            self.assertEqual((await response.json())["error"]["code"], "validation_error")

    async def test_high_confidence_reminder_candidate(self) -> None:
        response = await self._post(
            self._body(text="завтра в 15:00 напомни позвонить врачу")
        )
        payload = await response.json()

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["confidence"], "high")
        self.assertFalse(payload["requires_confirmation"])
        self.assertEqual(payload["candidate"]["domain"], "reminder")
        self.assertEqual(payload["candidate"]["operation"], "create")
        self.assertEqual(payload["candidate"]["fields"]["title"], "позвонить врачу")
        self.assertEqual(payload["candidate"]["fields"]["due_at_utc"], "2026-08-13T12:00:00Z")

    async def test_high_confidence_task_candidate(self) -> None:
        response = await self._post(self._body(text="завтра нужно купить молоко"))
        payload = await response.json()

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["candidate"]["domain"], "task")
        self.assertEqual(payload["candidate"]["operation"], "create")
        self.assertEqual(payload["candidate"]["fields"]["title"], "купить молоко")
        self.assertEqual(payload["candidate"]["fields"]["due_date"], "2026-08-13")
        self.assertIsNone(payload["candidate"]["fields"]["due_time"])
        self.assertIsNone(payload["candidate"]["fields"]["timezone"])
        self.assertEqual(payload["candidate"]["fields"]["priority"], "none")

    async def test_high_confidence_calendar_event_candidate(self) -> None:
        response = await self._post(
            self._body(text="завтра с 17:00 до 19:00 встреча с Егором")
        )
        payload = await response.json()

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["candidate"]["domain"], "calendar_event")
        self.assertEqual(payload["candidate"]["operation"], "create")
        self.assertFalse(payload["candidate"]["fields"]["all_day"])
        self.assertEqual(payload["candidate"]["fields"]["start_at_utc"], "2026-08-13T14:00:00Z")
        self.assertEqual(payload["candidate"]["fields"]["end_at_utc"], "2026-08-13T16:00:00Z")
        self.assertEqual(payload["candidate"]["fields"]["sync_state"], "local_only")

    async def test_ambiguities_are_preserved_without_silent_resolution(self) -> None:
        cases = (
            ("завтра вечером встреча с командой", "time"),
            ("на следующей неделе нужно встретиться", "date"),
            ("завтра с пяти до семи встреча с командой", "time"),
        )
        for phrase, field in cases:
            response = await self._post(self._body(text=phrase))
            payload = await response.json()
            with self.subTest(phrase=phrase):
                self.assertEqual(response.status, 200)
                self.assertIsNone(payload["candidate"])
                self.assertEqual(payload["confidence"], "medium")
                self.assertTrue(payload["requires_confirmation"])
                self.assertEqual(payload["ambiguities"][0]["field"], field)
                self.assertEqual(payload["error_code"], None)

    async def test_start_only_event_candidate_preserves_confirmation_semantics(self) -> None:
        response = await self._post(
            self._body(text="завтра в пять вечера встреча с командой")
        )
        payload = await response.json()

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["candidate"]["domain"], "calendar_event")
        self.assertEqual(payload["candidate"]["operation"], "create")
        self.assertEqual(payload["confidence"], "high")
        self.assertTrue(payload["requires_confirmation"])
        self.assertEqual(payload["ambiguities"][0]["field"], "end_time")
        self.assertEqual(payload["candidate"]["fields"]["proposed_end_local"], "18:00")

    async def test_parser_error_result_is_returned_without_internal_exception_details(self) -> None:
        response = await self._post(
            self._body(
                text="29 марта 2026 в 02:30 напомни проверить",
                reference_time_utc="2026-03-28T12:00:00Z",
                timezone="Europe/Berlin",
            )
        )
        payload = await response.json()

        self.assertEqual(response.status, 200)
        self.assertIsNone(payload["candidate"])
        self.assertEqual(payload["confidence"], "low")
        self.assertFalse(payload["requires_confirmation"])
        self.assertEqual(payload["ambiguities"], [])
        self.assertEqual(payload["error_code"], "nonexistent_local_time")
        self.assertNotIn("error_message", payload)

    async def test_query_candidate_is_returned_but_never_executed(self) -> None:
        self._seed_existing_planning_state()
        before = self._snapshot()
        response = await self._post(self._body(text="какие у меня напоминания"))
        payload = await response.json()

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["candidate"]["domain"], "reminder")
        self.assertEqual(payload["candidate"]["operation"], "query")
        self.assertEqual(payload["candidate"]["fields"], {"query": "active_reminders"})
        self.assertEqual(self._snapshot(), before)

    async def test_same_input_is_deterministically_equivalent(self) -> None:
        first = await self._post(self._body())
        second = await self._post(self._body())
        first_payload = await first.json()
        second_payload = await second.json()

        self.assertEqual(first.status, 200)
        self.assertEqual(second.status, 200)
        first_payload.pop("correlation_id")
        second_payload.pop("correlation_id")
        self.assertEqual(first_payload, second_payload)
        self.assertEqual(self._snapshot()["idempotency_keys"], [])

    async def test_high_confidence_create_changes_no_durable_planning_state(self) -> None:
        self._seed_existing_planning_state()
        before = self._snapshot()
        response = await self._post(
            self._body(text="завтра в 15:00 напомни позвонить врачу")
        )
        payload = await response.json()
        after = self._snapshot()

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["candidate"]["operation"], "create")
        self.assertEqual(payload["confidence"], "high")
        self.assertEqual(after, before)
        for table in SNAPSHOT_TABLES:
            self.assertEqual(after[table], before[table], table)
        self.assertEqual(after["outbox"], [])
        self.assertEqual(after["idempotency_keys"], before["idempotency_keys"])

    async def test_preview_does_not_require_or_write_idempotency_audit_or_outbox(self) -> None:
        before = self._snapshot()
        response = await self._post(self._body())
        after = self._snapshot()

        self.assertEqual(response.status, 200)
        self.assertEqual(after, before)
        self.assertEqual(after["idempotency_keys"], [])
        self.assertEqual(after["audit_events"], [])
        self.assertEqual(after["outbox"], [])

    async def test_hostile_text_is_parser_input_only(self) -> None:
        hostile_phrases = (
            "завтра в 15:00 напомни light.turn_on",
            "завтра в 15:00 напомни /etc/passwd",
            "завтра в 15:00 напомни https://example.com",
            "завтра в 15:00 напомни rm -rf /",
            "завтра в 15:00 напомни shell command",
            "завтра в 15:00 напомни homeassistant service entity",
        )
        before = self._snapshot()
        with patch.object(subprocess, "run") as subprocess_run, patch.object(
            urllib.request, "urlopen"
        ) as urlopen:
            for phrase in hostile_phrases:
                response = await self._post(self._body(text=phrase))
                payload = await response.json()
                with self.subTest(phrase=phrase):
                    self.assertEqual(response.status, 200)
                    self.assertEqual(payload["candidate"]["domain"], "reminder")
                    self.assertNotIn("service", payload["candidate"]["fields"])
                    self.assertNotIn("entity_id", payload["candidate"]["fields"])
                    self.assertNotIn("command", payload["candidate"]["fields"])
                    self.assertNotIn("url", payload["candidate"]["fields"])
        subprocess_run.assert_not_called()
        urlopen.assert_not_called()
        self.assertEqual(self._snapshot(), before)

    async def test_response_contains_no_credentials_or_private_server_metadata(self) -> None:
        response = await self._post(self._body())
        serialized = await response.text()

        for private_value in (
            PANEL_SECRET,
            HA_SECRET,
            OPERATOR_SECRET,
            INTERNAL_SECRET,
            str(self.db_path),
            "source_ref",
            "provider_payload",
            "database_path",
            "error_message",
            "actor",
        ):
            self.assertNotIn(private_value, serialized)

    async def test_preview_does_not_invoke_alice_interpretation_service(self) -> None:
        self.assertNotIn("planning_alice_service", self.app)
        with patch.object(
            AliceInterpretationService,
            "interpret",
            side_effect=AssertionError("Alice interpretation must not run for preview"),
        ) as interpret:
            response = await self._post(self._body())

        self.assertEqual(response.status, 200)
        interpret.assert_not_called()


class PlanningParsePreviewRegistrationTests(unittest.TestCase):
    def test_route_is_not_registered_when_existing_planning_api_gate_is_off(self) -> None:
        from app.web.internal_routes import setup_internal_routes

        app = web.Application()
        app["settings"] = SimpleNamespace(
            planning_api_enabled=False,
            planning_alice_interpret_enabled=False,
        )
        setup_internal_routes(app)
        paths = {resource.canonical for resource in app.router.resources()}
        self.assertNotIn(f"{PLANNING_PREFIX}/parse", paths)


if __name__ == "__main__":
    unittest.main()
