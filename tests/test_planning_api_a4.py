from __future__ import annotations

import asyncio
import io
import json
import logging
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app.planning import AuditWriter, MutationContext, PlanningDatabase, PlanningRepository
from app.planning.api.auth import InProcessRateLimiter
from app.planning.api.routes import PLANNING_PREFIX, setup_planning_routes
from app.planning.api.service import PlanningApiService
from app.web.internal_routes import setup_internal_routes


PANEL_SECRET = "synthetic-panel-agent-secret"
HA_SECRET = "synthetic-ha-secret"
OPERATOR_SECRET = "synthetic-operator-secret"
INTERNAL_SECRET = "synthetic-existing-internal-secret"
NOW = "2026-08-11T08:00:00Z"
_MISSING = object()


class PlanningApiA4Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "planning.sqlite3"
        self.database = PlanningDatabase(self.db_path)
        self.service = PlanningApiService(self.database, now_fn=lambda: NOW)
        self.settings = SimpleNamespace(
            planning_api_enabled=True,
            internal_webhook_secret=INTERNAL_SECRET,
            planning_ha_secret=HA_SECRET,
            planning_panel_agent_secret=PANEL_SECRET,
            planning_operator_secret=OPERATOR_SECRET,
            planning_api_rate_limit_per_minute=120,
            planning_api_stale_after_seconds=300,
            planning_default_timezone="Europe/Moscow",
        )
        self.app = web.Application()
        self.app["settings"] = self.settings
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

    async def _request(
        self,
        method: str,
        path: str,
        *,
        audience: str = "panel-agent",
        secret: str = PANEL_SECRET,
        headers: dict[str, str] | None = None,
        json_body: object = _MISSING,
        data: bytes | None = None,
    ):
        request_headers = self._headers(audience, secret)
        if headers:
            request_headers.update(headers)
        kwargs: dict[str, object] = {"headers": request_headers}
        if json_body is not _MISSING:
            kwargs["json"] = json_body
        if data is not None:
            kwargs["data"] = data
        return await self.client.request(method, path, **kwargs)

    async def _create_task(
        self,
        *,
        key: str,
        title: str = "Synthetic task",
        due_date: str | None = None,
        due_time: str | None = None,
        timezone: str | None = None,
        priority: str = "normal",
        project_id: str | None = None,
    ) -> tuple[object, dict]:
        body: dict[str, object] = {"title": title, "priority": priority}
        if due_date is not None:
            body["due_date"] = due_date
        if due_time is not None:
            body["due_time"] = due_time
        if timezone is not None:
            body["timezone"] = timezone
        if project_id is not None:
            body["project_id"] = project_id
        response = await self._request(
            "POST",
            f"{PLANNING_PREFIX}/tasks",
            headers={"Idempotency-Key": key},
            json_body=body,
        )
        payload = await response.json()
        return response, payload

    async def _create_reminder(self, *, key: str, title: str = "Synthetic reminder") -> tuple[object, dict]:
        response = await self._request(
            "POST",
            f"{PLANNING_PREFIX}/reminders",
            headers={"Idempotency-Key": key},
            json_body={
                "title": title,
                "due_at_utc": "2026-08-14T07:30:00Z",
                "timezone": "Europe/Moscow",
            },
        )
        return response, await response.json()

    async def _create_event(self, *, key: str, body: dict[str, object]) -> tuple[object, dict]:
        response = await self._request(
            "POST",
            f"{PLANNING_PREFIX}/events",
            headers={"Idempotency-Key": key},
            json_body=body,
        )
        return response, await response.json()

    def _planning_state_snapshot(self) -> dict[str, list[tuple[object, ...]]]:
        tables = (
            "calendar_events",
            "audit_events",
            "idempotency_keys",
            "outbox",
            "provider_mappings",
            "sync_cursors",
            "sync_conflicts",
        )
        return {
            table: [
                tuple(row)
                for row in self.database.connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            ]
            for table in tables
        }

    def _seed_external_event(self):
        return self.service.repository.create_calendar_event(
            title="Provider-owned event",
            all_day=False,
            timezone="Europe/Moscow",
            start_at_utc="2026-08-15T09:00:00Z",
            end_at_utc="2026-08-15T10:00:00Z",
            sync_state="synced",
            provider_id="provider-event-1",
            provider_calendar_id="provider-calendar-1",
            source_ref="provider:event:1",
            context=MutationContext(
                audience="operator",
                actor_id="provider-fixture",
                actor_type="operator",
                surface="operator",
            ),
        )

    async def test_authentication_and_audience_matrix(self) -> None:
        missing = await self.client.get(f"{PLANNING_PREFIX}/status")
        self.assertEqual(missing.status, 401)
        self.assertEqual((await missing.json())["error"]["code"], "authentication_failed")

        missing_existing = await self.client.get(
            f"{PLANNING_PREFIX}/status",
            headers={"X-Planning-Audience": "panel-agent", "X-Planning-Secret": PANEL_SECRET},
        )
        self.assertEqual(missing_existing.status, 401)

        wrong = await self._request(
            "GET",
            f"{PLANNING_PREFIX}/status",
            secret="wrong-secret",
        )
        self.assertEqual(wrong.status, 401)

        panel = await self._request("GET", f"{PLANNING_PREFIX}/status")
        self.assertEqual(panel.status, 200)
        self.assertEqual((await panel.json())["kind"], "status")

        ha_status = await self._request("GET", f"{PLANNING_PREFIX}/status", audience="ha", secret=HA_SECRET)
        self.assertEqual(ha_status.status, 200)
        ha_read = await self._request(
            "GET",
            f"{PLANNING_PREFIX}/tasks?view=today",
            audience="ha",
            secret=HA_SECRET,
        )
        self.assertEqual(ha_read.status, 200)
        ha_write = await self._request(
            "POST",
            f"{PLANNING_PREFIX}/tasks",
            audience="ha",
            secret=HA_SECRET,
            headers={"Idempotency-Key": "ha-write-denied"},
            json_body={"title": "must not write", "priority": "normal"},
        )
        self.assertEqual(ha_write.status, 403)
        self.assertEqual((await ha_write.json())["error"]["code"], "audience_forbidden")

        selector_mismatch = await self._request(
            "GET",
            f"{PLANNING_PREFIX}/status",
            audience="ha",
            secret=PANEL_SECRET,
        )
        self.assertEqual(selector_mismatch.status, 401)

        wrong_existing = await self._request(
            "GET",
            f"{PLANNING_PREFIX}/status",
            headers={"X-Internal-Secret": "wrong-existing-secret"},
        )
        self.assertEqual(wrong_existing.status, 401)

        operator = await self._request(
            "GET",
            f"{PLANNING_PREFIX}/status",
            audience="operator",
            secret=OPERATOR_SECRET,
        )
        self.assertEqual(operator.status, 200)

    async def test_body_audience_injection_and_secrets_are_not_exposed(self) -> None:
        injected = await self._request(
            "POST",
            f"{PLANNING_PREFIX}/tasks",
            headers={"Idempotency-Key": "body-audience-injection"},
            json_body={"title": "injection", "priority": "normal", "audience": "panel-agent"},
        )
        self.assertEqual(injected.status, 400)

        response = await self._request("GET", f"{PLANNING_PREFIX}/status")
        body = await response.text()
        self.assertNotIn(PANEL_SECRET, body)
        self.assertNotIn(HA_SECRET, body)
        self.assertNotIn("secret", body.lower())

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger = logging.getLogger("app.planning.api.routes")
        logger.addHandler(handler)
        try:
            failed = await self._request(
                "GET",
                f"{PLANNING_PREFIX}/status",
                secret=PANEL_SECRET + "-wrong",
            )
            self.assertEqual(failed.status, 401)
        finally:
            logger.removeHandler(handler)
        self.assertNotIn(PANEL_SECRET, stream.getvalue())

    async def test_strict_server_owned_request_hash_and_execution_fields(self) -> None:
        cases = [
            {"title": "unknown", "priority": "normal", "unexpected": True},
            {"title": "server", "priority": "normal", "id": str(uuid.uuid4())},
            {"title": "server", "priority": "normal", "version": 9},
            {"title": "server", "priority": "normal", "source": "panel-agent"},
            {"title": "server", "priority": "normal", "source_ref": "client"},
            {"title": "server", "priority": "normal", "request_hash": "sha256:" + "a" * 64},
            {"title": "unsafe", "priority": "normal", "service": "light.turn_on"},
            {"title": "unsafe", "priority": "normal", "entity": "light.example"},
            {"title": "unsafe", "priority": "normal", "shell": "echo unsafe"},
            {"title": "unsafe", "priority": "normal", "command": "echo unsafe"},
            {"title": "unsafe", "priority": "normal", "url": "https://example.invalid"},
            {"title": "unsafe", "priority": "normal", "path": "/tmp/unsafe"},
        ]
        for index, body in enumerate(cases):
            response = await self._request(
                "POST",
                f"{PLANNING_PREFIX}/tasks",
                headers={"Idempotency-Key": f"strict-{index}"},
                json_body=body,
            )
            with self.subTest(index=index):
                self.assertEqual(response.status, 400)

        request_hash_header = await self._request(
            "POST",
            f"{PLANNING_PREFIX}/tasks",
            headers={"Idempotency-Key": "request-hash-header", "request_hash": "sha256:" + "a" * 64},
            json_body={"title": "hash", "priority": "normal"},
        )
        self.assertEqual(request_hash_header.status, 400)

        body_too_large = await self._request(
            "POST",
            f"{PLANNING_PREFIX}/tasks",
            headers={"Idempotency-Key": "body-too-large"},
            data=json.dumps({"title": "x" * 70_000, "priority": "normal"}).encode("utf-8"),
        )
        self.assertEqual(body_too_large.status, 413)
        self.assertEqual((await body_too_large.json())["error"]["code"], "body_too_large")

    async def test_missing_key_malformed_json_and_unknown_route(self) -> None:
        missing = await self._request(
            "POST",
            f"{PLANNING_PREFIX}/tasks",
            json_body={"title": "missing key", "priority": "normal"},
        )
        self.assertEqual(missing.status, 400)
        self.assertEqual((await missing.json())["error"]["code"], "missing_idempotency_key")

        malformed = await self._request(
            "POST",
            f"{PLANNING_PREFIX}/tasks",
            headers={"Idempotency-Key": "malformed-json"},
            data=b"{not-json",
        )
        self.assertEqual(malformed.status, 400)
        self.assertEqual((await malformed.json())["error"]["code"], "malformed_json")

        for suffix in ("alice/interpret", "objects", "execute", "proxy", "action", "tool", "service"):
            absent = await self._request("POST", f"{PLANNING_PREFIX}/{suffix}")
            self.assertEqual(absent.status, 404)
            self.assertEqual((await absent.json())["kind"], "error")

        for method, path in (
            ("PUT", f"{PLANNING_PREFIX}/status"),
            ("POST", f"{PLANNING_PREFIX}/projects"),
            ("PUT", f"{PLANNING_PREFIX}/tasks"),
        ):
            unsupported = await self._request(method, path)
            self.assertEqual(unsupported.status, 404)
            unsupported_payload = await unsupported.json()
            self.assertEqual(unsupported_payload["kind"], "error")

    async def test_task_idempotency_exact_replay_conflict_and_audience_scope(self) -> None:
        first_response, first = await self._create_task(key="task-replay")
        first_bytes = await first_response.read()
        replay_response = await self._request(
            "POST",
            f"{PLANNING_PREFIX}/tasks",
            headers={"Idempotency-Key": "task-replay"},
            json_body={"title": "Synthetic task", "priority": "normal"},
        )
        replay_bytes = await replay_response.read()
        self.assertEqual(first_response.status, 200)
        self.assertEqual(replay_response.status, 200)
        self.assertEqual(first_bytes, replay_bytes)
        replay = json.loads(replay_bytes)
        self.assertEqual(first["correlation_id"], replay["correlation_id"])
        self.assertEqual(first["object"], replay["object"])

        conflict = await self._request(
            "POST",
            f"{PLANNING_PREFIX}/tasks",
            headers={"Idempotency-Key": "task-replay"},
            json_body={"title": "different body", "priority": "normal"},
        )
        self.assertEqual(conflict.status, 409)
        self.assertEqual((await conflict.json())["error"]["code"], "idempotency_conflict")

        scoped = await self._request(
            "POST",
            f"{PLANNING_PREFIX}/tasks",
            audience="operator",
            secret=OPERATOR_SECRET,
            headers={"Idempotency-Key": "task-replay"},
            json_body={"title": "operator scoped", "priority": "low"},
        )
        self.assertEqual(scoped.status, 200)
        self.assertNotEqual(scoped.headers.get("X-Correlation-ID"), first["correlation_id"])

    async def test_same_key_different_action_or_object_conflicts(self) -> None:
        _, created = await self._create_task(key="action-conflict")
        action = await self._request(
            "POST",
            f"{PLANNING_PREFIX}/tasks/{created['object']['id']}/complete",
            headers={"Idempotency-Key": "action-conflict", "If-Match": "1"},
            json_body={},
        )
        self.assertEqual(action.status, 409)
        self.assertEqual((await action.json())["error"]["code"], "idempotency_conflict")

        _, other = await self._create_task(key="other-object")
        first_object = await self._request(
            "POST",
            f"{PLANNING_PREFIX}/tasks/{created['object']['id']}/complete",
            headers={"Idempotency-Key": "object-key", "If-Match": "1"},
            json_body={},
        )
        self.assertEqual(first_object.status, 200)
        other_object = await self._request(
            "POST",
            f"{PLANNING_PREFIX}/tasks/{other['object']['id']}/complete",
            headers={"Idempotency-Key": "object-key", "If-Match": "1"},
            json_body={},
        )
        self.assertEqual(other_object.status, 409)
        self.assertEqual((await other_object.json())["error"]["code"], "idempotency_conflict")

    async def test_concurrent_duplicate_requests_mutate_once(self) -> None:
        async def send():
            return await self._request(
                "POST",
                f"{PLANNING_PREFIX}/tasks",
                headers={"Idempotency-Key": "concurrent-task"},
                json_body={"title": "one canonical task", "priority": "normal"},
            )

        first, second = await asyncio.gather(send(), send())
        first_bytes = await first.read()
        second_bytes = await second.read()
        self.assertEqual(first.status, 200)
        self.assertEqual(second.status, 200)
        self.assertEqual(first_bytes, second_bytes)
        self.assertEqual(self.database.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 1)
        self.assertEqual(
            self.database.connection.execute(
                "SELECT COUNT(*) FROM idempotency_keys WHERE audience = 'panel-agent'"
            ).fetchone()[0],
            1,
        )

    async def test_rollback_removes_domain_audit_and_idempotency(self) -> None:
        failing = PlanningRepository(self.database, AuditWriter(self.database, fail=True))
        self.service.repository = failing
        response, payload = await self._create_task(key="audit-failure")
        self.assertEqual(response.status, 500)
        self.assertEqual(payload["error"]["code"], "internal_error")
        self.assertEqual(self.database.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)
        self.assertEqual(self.database.connection.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0], 0)
        self.assertEqual(
            self.database.connection.execute(
                "SELECT COUNT(*) FROM idempotency_keys WHERE key = 'audit-failure'"
            ).fetchone()[0],
            0,
        )

    async def test_failure_after_domain_mutation_before_response_store_rolls_back(self) -> None:
        original = self.service.repository.store_idempotency_response

        def fail_after_domain(*args, **kwargs):
            raise RuntimeError("synthetic response-store failure")

        with patch.object(self.service.repository, "store_idempotency_response", side_effect=fail_after_domain):
            response, payload = await self._create_task(key="response-store-failure")
        self.assertEqual(response.status, 500)
        self.assertEqual(payload["error"]["code"], "internal_error")
        self.assertEqual(self.database.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)
        self.assertEqual(
            self.database.connection.execute(
                "SELECT COUNT(*) FROM idempotency_keys WHERE key = 'response-store-failure'"
            ).fetchone()[0],
            0,
        )
        self.service.repository.store_idempotency_response = original

    async def test_successful_mutation_audit_and_response_commit_together(self) -> None:
        response, payload = await self._create_task(key="atomic-success")
        self.assertEqual(response.status, 200)
        task_id = payload["object"]["id"]
        self.assertEqual(
            self.database.connection.execute("SELECT COUNT(*) FROM tasks WHERE id = ?", (task_id,)).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.database.connection.execute("SELECT COUNT(*) FROM audit_events WHERE object_id = ?", (task_id,)).fetchone()[0],
            1,
        )
        row = self.database.connection.execute(
            "SELECT response_json, correlation_id FROM idempotency_keys WHERE audience = 'panel-agent' AND key = 'atomic-success'"
        ).fetchone()
        self.assertEqual(row["response_json"], await response.text())
        self.assertEqual(row["correlation_id"], payload["correlation_id"])

    async def test_expected_version_is_required_and_stale_versions_are_safe(self) -> None:
        _, created = await self._create_task(key="version-task")
        task_id = created["object"]["id"]
        missing_patch = await self._request(
            "PATCH",
            f"{PLANNING_PREFIX}/tasks/{task_id}",
            headers={"Idempotency-Key": "missing-patch-version"},
            json_body={"title": "updated"},
        )
        self.assertEqual(missing_patch.status, 400)
        self.assertEqual((await missing_patch.json())["error"]["code"], "invalid_if_match")

        missing_action = await self._request(
            "POST",
            f"{PLANNING_PREFIX}/tasks/{task_id}/complete",
            headers={"Idempotency-Key": "missing-action-version"},
            json_body={},
        )
        self.assertEqual(missing_action.status, 400)

        updated = await self._request(
            "PATCH",
            f"{PLANNING_PREFIX}/tasks/{task_id}",
            headers={"Idempotency-Key": "valid-patch", "If-Match": "1"},
            json_body={"title": "updated"},
        )
        updated_payload = await updated.json()
        self.assertEqual(updated.status, 200)
        self.assertEqual(updated_payload["object"]["version"], 2)

        stale = await self._request(
            "PATCH",
            f"{PLANNING_PREFIX}/tasks/{task_id}",
            headers={"Idempotency-Key": "stale-patch", "If-Match": "1"},
            json_body={"title": "stale"},
        )
        stale_payload = await stale.json()
        self.assertEqual(stale.status, 409)
        self.assertEqual(stale_payload["error"]["code"], "version_conflict")
        self.assertEqual(stale_payload["error"]["details"]["expected_version"], 1)
        self.assertEqual(stale_payload["error"]["details"]["actual_version"], 2)
        self.assertNotIn("updated", json.dumps(stale_payload))

    async def test_reminder_lifecycle_delivery_independence_and_tombstone(self) -> None:
        _, created = await self._create_reminder(key="reminder-create")
        reminder = created["object"]
        self.assertEqual(reminder["status"], "pending")
        self.assertEqual(reminder["delivery_state"], "not_due")
        self.assertEqual(reminder["source"], "panel-agent")

        listed = await self._request("GET", f"{PLANNING_PREFIX}/reminders?state=pending")
        listed_payload = await listed.json()
        self.assertEqual(listed.status, 200)
        self.assertEqual([item["id"] for item in listed_payload["items"]], [reminder["id"]])

        edited = await self._request(
            "PATCH",
            f"{PLANNING_PREFIX}/reminders/{reminder['id']}",
            headers={"Idempotency-Key": "reminder-edit", "If-Match": "1"},
            json_body={"title": "Edited reminder"},
        )
        edited_payload = await edited.json()
        self.assertEqual(edited.status, 200)
        self.assertEqual(edited_payload["object"]["version"], 2)

        delivered = self.service.repository.update_reminder(
            reminder["id"],
            expected_version=2,
            context=MutationContext(
                audience="operator",
                actor_id="synthetic-test",
                actor_type="service",
                surface="system",
            ),
            delivery_state="delivered",
        )
        self.assertEqual(delivered.status, "pending")
        delivered_list = await self._request("GET", f"{PLANNING_PREFIX}/reminders?state=pending")
        delivered_payload = await delivered_list.json()
        self.assertEqual(delivered_payload["items"][0]["delivery_state"], "delivered")
        self.assertEqual(delivered_payload["items"][0]["status"], "pending")

        cancelled = await self._request(
            "POST",
            f"{PLANNING_PREFIX}/reminders/{reminder['id']}/cancel",
            headers={"Idempotency-Key": "reminder-cancel", "If-Match": "3"},
            json_body={},
        )
        cancelled_payload = await cancelled.json()
        self.assertEqual(cancelled.status, 200)
        self.assertEqual(cancelled_payload["object"]["status"], "cancelled")
        self.assertIsNotNone(cancelled_payload["object"]["deleted_at"])
        self.assertEqual(self.database.connection.execute("SELECT COUNT(*) FROM reminders").fetchone()[0], 1)
        after_cancel = await self._request("GET", f"{PLANNING_PREFIX}/reminders?state=cancelled")
        self.assertEqual([item["id"] for item in (await after_cancel.json())["items"]], [reminder["id"]])

    async def test_reminder_complete_requires_version_and_returns_canonical_object(self) -> None:
        _, created = await self._create_reminder(key="reminder-complete-create")
        reminder_id = created["object"]["id"]
        complete = await self._request(
            "POST",
            f"{PLANNING_PREFIX}/reminders/{reminder_id}/complete",
            headers={"Idempotency-Key": "reminder-complete", "If-Match": "1"},
            json_body={},
        )
        payload = await complete.json()
        self.assertEqual(complete.status, 200)
        self.assertEqual(payload["object"]["status"], "completed")
        self.assertEqual(payload["object"]["version"], 2)
        self.assertIsNotNone(payload["object"]["completed_at"])

    async def test_task_date_only_timed_views_complete_and_archive(self) -> None:
        _, today = await self._create_task(key="today-task", title="today", due_date="2026-08-11", priority="high")
        _, overdue = await self._create_task(key="overdue-task", title="overdue", due_date="2026-08-10", priority="normal")
        _, upcoming = await self._create_task(
            key="upcoming-task",
            title="upcoming",
            due_date="2026-08-12",
            due_time="09:30",
            timezone="Europe/Moscow",
            priority="low",
        )
        self.assertIsNone(today["object"]["due_time"])
        self.assertIsNone(today["object"]["timezone"])
        self.assertEqual(upcoming["object"]["due_time"], "09:30")
        self.assertEqual(upcoming["object"]["timezone"], "Europe/Moscow")

        for view, expected in (("today", ["today"]), ("overdue", ["overdue"]), ("upcoming", ["upcoming"])):
            response = await self._request("GET", f"{PLANNING_PREFIX}/tasks?view={view}")
            payload = await response.json()
            with self.subTest(view=view):
                self.assertEqual(response.status, 200)
                self.assertEqual([item["title"] for item in payload["items"]], expected)

        completed = await self._request(
            "POST",
            f"{PLANNING_PREFIX}/tasks/{today['object']['id']}/complete",
            headers={"Idempotency-Key": "today-complete", "If-Match": "1"},
            json_body={},
        )
        self.assertEqual((await completed.json())["object"]["status"], "completed")
        archived = await self._request(
            "DELETE",
            f"{PLANNING_PREFIX}/tasks/{overdue['object']['id']}",
            headers={"Idempotency-Key": "overdue-archive", "If-Match": "1"},
        )
        archived_payload = await archived.json()
        self.assertEqual(archived.status, 200)
        self.assertEqual(archived_payload["object"]["status"], "archived")
        self.assertIsNotNone(archived_payload["object"]["deleted_at"])
        self.assertEqual(self.database.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 3)

    async def test_undated_task_view_is_canonical_bounded_and_read_only(self) -> None:
        context = MutationContext(
            audience="operator",
            actor_id="synthetic-undated-test",
            actor_type="operator",
            surface="operator",
        )
        selected_project = self.service.repository.create_project(name="Selected undated", context=context)
        other_project = self.service.repository.create_project(name="Other undated", context=context)
        selected = self.service.repository.create_task(
            title="selected undated",
            project_id=selected_project.id,
            context=context,
        )
        selected_second = self.service.repository.create_task(
            title="selected undated second",
            project_id=selected_project.id,
            context=context,
        )
        different_project = self.service.repository.create_task(
            title="different project undated",
            project_id=other_project.id,
            context=context,
        )
        date_only = self.service.repository.create_task(
            title="date-only excluded",
            due_date="2026-08-11",
            context=context,
        )
        timed = self.service.repository.create_task(
            title="timed excluded",
            due_date="2026-08-11",
            due_time="09:30",
            timezone="Europe/Moscow",
            context=context,
        )
        completed = self.service.repository.create_task(title="completed excluded", context=context)
        self.service.repository.complete_task(completed.id, expected_version=1, context=context)
        archived = self.service.repository.create_task(title="archived excluded", context=context)
        self.service.repository.archive_task(archived.id, expected_version=1, context=context)
        tombstoned = self.service.repository.create_task(title="tombstoned excluded", context=context)
        self.database.connection.execute(
            "UPDATE tasks SET deleted_at = ? WHERE id = ?",
            (NOW, tombstoned.id),
        )

        def state_snapshot() -> dict[str, list[tuple[object, ...]]]:
            return {
                table: [
                    tuple(row)
                    for row in self.database.connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
                ]
                for table in ("tasks", "projects", "audit_events", "idempotency_keys", "outbox")
            }

        before_read = state_snapshot()
        response = await self._request("GET", f"{PLANNING_PREFIX}/tasks?view=undated")
        payload = await response.json()
        self.assertEqual(response.status, 200)
        self.assertEqual(payload["schemaVersion"], "planning.v1")
        self.assertEqual(payload["kind"], "list")
        self.assertEqual(payload["domain"], "task")
        returned_ids = [item["id"] for item in payload["items"]]
        self.assertEqual(
            returned_ids,
            [task.id for task in sorted((selected, selected_second, different_project), key=lambda item: (item.created_at, item.id))],
        )
        selected_payload = next(item for item in payload["items"] if item["id"] == selected.id)
        self.assertEqual(selected_payload, selected.to_dict())
        self.assertIsNone(selected_payload["due_date"])
        self.assertIsNone(selected_payload["due_time"])
        self.assertIsNone(selected_payload["timezone"])
        for excluded in (date_only, timed, completed, archived, tombstoned):
            self.assertNotIn(excluded.id, returned_ids)

        filtered = await self._request(
            "GET",
            f"{PLANNING_PREFIX}/tasks?view=undated&project_id={selected_project.id}",
        )
        filtered_payload = await filtered.json()
        self.assertEqual(filtered.status, 200)
        self.assertEqual(
            [item["id"] for item in filtered_payload["items"]],
            [
                task.id
                for task in sorted((selected, selected_second), key=lambda item: (item.created_at, item.id))
            ],
        )
        self.assertNotIn(different_project.id, [item["id"] for item in filtered_payload["items"]])

        first_page = await self._request("GET", f"{PLANNING_PREFIX}/tasks?view=undated&limit=1")
        first_page_payload = await first_page.json()
        self.assertEqual(first_page.status, 200)
        self.assertEqual(first_page_payload["pagination"], {
            "count": 1,
            "has_more": True,
            "limit": 1,
            "next_offset": 1,
            "offset": 0,
        })

        for audience, secret in (("ha", HA_SECRET), ("panel-agent", PANEL_SECRET), ("operator", OPERATOR_SECRET)):
            permitted = await self._request(
                "GET",
                f"{PLANNING_PREFIX}/tasks?view=undated",
                audience=audience,
                secret=secret,
            )
            self.assertEqual(permitted.status, 200, audience)
        unauthorized = await self.client.get(f"{PLANNING_PREFIX}/tasks?view=undated")
        self.assertEqual(unauthorized.status, 401)
        wrong_secret = await self._request(
            "GET",
            f"{PLANNING_PREFIX}/tasks?view=undated",
            secret="wrong-secret",
        )
        self.assertEqual(wrong_secret.status, 401)

        self.assertEqual(state_snapshot(), before_read)

    async def test_undated_task_query_allowlist_and_bounds_remain_strict(self) -> None:
        cases = (
            "view=unknown",
            "view=undated&view=undated",
            "view=undated&project_id=not-a-uuid",
            "view=undated&limit=101",
            "view=undated&offset=10001",
        )
        for query in cases:
            response = await self._request("GET", f"{PLANNING_PREFIX}/tasks?{query}")
            payload = await response.json()
            with self.subTest(query=query):
                self.assertEqual(response.status, 400)
                self.assertEqual(payload["kind"], "error")
                self.assertEqual(payload["error"]["code"], "validation_error")
                self.assertNotIn(str(self.db_path), json.dumps(payload))
                self.assertNotIn(PANEL_SECRET, json.dumps(payload))

    async def test_task_read_by_id_is_bounded_and_audience_scoped(self) -> None:
        _, created = await self._create_task(key="read-by-id-open", title="Read by id")
        task_id = created["object"]["id"]
        before = {
            table: self.database.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("tasks", "audit_events", "idempotency_keys", "outbox")
        }

        with patch.object(self.service.task_service, "get", wraps=self.service.task_service.get) as get_task:
            panel = await self._request("GET", f"{PLANNING_PREFIX}/tasks/{task_id}")
        panel_payload = await panel.json()

        self.assertEqual(panel.status, 200)
        self.assertEqual(panel.headers["Cache-Control"], "no-store")
        self.assertEqual(panel_payload["schemaVersion"], "planning.v1")
        self.assertEqual(panel_payload["kind"], "object")
        self.assertEqual(panel_payload["domain"], "task")
        self.assertEqual(panel_payload["object"], created["object"])
        self.assertEqual(panel_payload["object"]["version"], 1)
        self.assertEqual(get_task.call_args.args, (task_id,))

        operator = await self._request(
            "GET",
            f"{PLANNING_PREFIX}/tasks/{task_id}",
            audience="operator",
            secret=OPERATOR_SECRET,
        )
        operator_payload = await operator.json()
        self.assertEqual(operator.status, 200)
        self.assertEqual(operator_payload["object"], created["object"])

        ha = await self._request(
            "GET",
            f"{PLANNING_PREFIX}/tasks/{task_id}",
            audience="ha",
            secret=HA_SECRET,
        )
        self.assertEqual(ha.status, 403)
        self.assertEqual((await ha.json())["error"]["code"], "audience_forbidden")

        with_query = await self._request("GET", f"{PLANNING_PREFIX}/tasks/{task_id}?view=today")
        self.assertEqual(with_query.status, 400)
        self.assertEqual((await with_query.json())["error"]["code"], "validation_error")

        after = {
            table: self.database.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("tasks", "audit_events", "idempotency_keys", "outbox")
        }
        self.assertEqual(after, before)

    async def test_task_read_by_id_preserves_terminal_and_temporal_shapes(self) -> None:
        _, undated = await self._create_task(key="read-undated", title="Undated")
        _, date_only = await self._create_task(
            key="read-date-only",
            title="Date only",
            due_date="2026-08-14",
        )
        _, timed = await self._create_task(
            key="read-timed",
            title="Timed",
            due_date="2026-08-14",
            due_time="09:30",
            timezone="Europe/Moscow",
        )

        completed = await self._request(
            "POST",
            f"{PLANNING_PREFIX}/tasks/{date_only['object']['id']}/complete",
            headers={"Idempotency-Key": "read-complete", "If-Match": "1"},
            json_body={},
        )
        archived = await self._request(
            "DELETE",
            f"{PLANNING_PREFIX}/tasks/{timed['object']['id']}",
            headers={"Idempotency-Key": "read-archive", "If-Match": "1"},
        )
        self.assertEqual(completed.status, 200)
        self.assertEqual(archived.status, 200)

        for label, task, expected_status in (
            ("undated", undated, "open"),
            ("date-only", date_only, "completed"),
            ("timed", timed, "archived"),
        ):
            response = await self._request("GET", f"{PLANNING_PREFIX}/tasks/{task['object']['id']}")
            payload = await response.json()
            with self.subTest(task=label):
                self.assertEqual(response.status, 200)
                self.assertEqual(payload["object"]["status"], expected_status)
                self.assertEqual(payload["object"]["version"], 1 if label == "undated" else 2)
                self.assertEqual(payload["object"]["source"], "panel-agent")

        undated_response = await self._request("GET", f"{PLANNING_PREFIX}/tasks/{undated['object']['id']}")
        undated_payload = await undated_response.json()
        self.assertIsNone(undated_payload["object"]["due_date"])
        self.assertIsNone(undated_payload["object"]["due_time"])
        self.assertIsNone(undated_payload["object"]["timezone"])

        date_only_response = await self._request("GET", f"{PLANNING_PREFIX}/tasks/{date_only['object']['id']}")
        date_only_payload = await date_only_response.json()
        self.assertEqual(date_only_payload["object"]["due_date"], "2026-08-14")
        self.assertIsNone(date_only_payload["object"]["due_time"])
        self.assertIsNone(date_only_payload["object"]["timezone"])
        self.assertIsNotNone(date_only_payload["object"]["completed_at"])

        timed_response = await self._request("GET", f"{PLANNING_PREFIX}/tasks/{timed['object']['id']}")
        timed_payload = await timed_response.json()
        self.assertEqual(timed_payload["object"]["due_date"], "2026-08-14")
        self.assertEqual(timed_payload["object"]["due_time"], "09:30")
        self.assertEqual(timed_payload["object"]["timezone"], "Europe/Moscow")
        self.assertIsNotNone(timed_payload["object"]["archived_at"])
        self.assertIsNotNone(timed_payload["object"]["deleted_at"])

    async def test_task_read_by_id_not_found_invalid_and_unmatched_paths_are_bounded(self) -> None:
        missing_id = str(uuid.uuid4())
        missing = await self._request("GET", f"{PLANNING_PREFIX}/tasks/{missing_id}")
        missing_payload = await missing.json()
        self.assertEqual(missing.status, 404)
        self.assertEqual(missing_payload["error"]["code"], "not_found")
        self.assertNotIn(missing_id, json.dumps(missing_payload))

        malformed = await self._request("GET", f"{PLANNING_PREFIX}/tasks/not-a-uuid")
        self.assertEqual(malformed.status, 400)
        self.assertEqual((await malformed.json())["error"]["code"], "validation_error")

        unmatched = await self._request("GET", f"{PLANNING_PREFIX}/tasks/{missing_id}/extra")
        self.assertEqual(unmatched.status, 404)
        self.assertEqual((await unmatched.json())["error"]["code"], "route_not_found")

        listed = await self._request("GET", f"{PLANNING_PREFIX}/tasks?view=today")
        self.assertEqual(listed.status, 200)

    async def test_event_read_by_id_is_canonical_read_only_and_audience_scoped(self) -> None:
        _, timed = await self._create_event(
            key="read-event-timed",
            body={
                "title": "Read timed",
                "notes": "Timed notes",
                "location": "Timed location",
                "all_day": False,
                "timezone": "Europe/Moscow",
                "start_at_utc": "2026-08-14T09:00:00Z",
                "end_at_utc": "2026-08-14T10:00:00Z",
            },
        )
        _, all_day = await self._create_event(
            key="read-event-all-day",
            body={
                "title": "Read all day",
                "all_day": True,
                "timezone": "Europe/Moscow",
                "start_date": "2026-08-14",
                "end_date_exclusive": "2026-08-16",
            },
        )
        _, deleted = await self._create_event(
            key="read-event-deleted",
            body={
                "title": "Read deleted",
                "all_day": True,
                "timezone": "Europe/Moscow",
                "start_date": "2026-08-17",
                "end_date_exclusive": "2026-08-18",
            },
        )
        deleted_response = await self._request(
            "DELETE",
            f"{PLANNING_PREFIX}/events/{deleted['object']['id']}",
            headers={"Idempotency-Key": "read-event-delete", "If-Match": "1"},
        )
        deleted_payload = await deleted_response.json()
        self.assertEqual(deleted_response.status, 200)
        self.assertEqual(deleted_payload["object"]["version"], 2)
        external_event = self._seed_external_event()
        before_reads = self._planning_state_snapshot()

        with patch.object(self.service.event_service, "get", wraps=self.service.event_service.get) as get_event:
            panel = await self._request("GET", f"{PLANNING_PREFIX}/events/{timed['object']['id']}")
        panel_payload = await panel.json()
        self.assertEqual(panel.status, 200)
        self.assertEqual(panel.headers["Cache-Control"], "no-store")
        self.assertEqual(panel_payload["schemaVersion"], "planning.v1")
        self.assertEqual(panel_payload["kind"], "object")
        self.assertEqual(panel_payload["domain"], "calendar_event")
        self.assertEqual(panel_payload["object"], timed["object"])
        self.assertEqual(get_event.call_args.args, (timed["object"]["id"],))
        self.assertEqual(
            {
                key: panel_payload["object"][key]
                for key in ("all_day", "timezone", "start_at_utc", "end_at_utc", "start_date", "end_date_exclusive")
            },
            {
                "all_day": False,
                "timezone": "Europe/Moscow",
                "start_at_utc": "2026-08-14T09:00:00Z",
                "end_at_utc": "2026-08-14T10:00:00Z",
                "start_date": None,
                "end_date_exclusive": None,
            },
        )

        operator = await self._request(
            "GET",
            f"{PLANNING_PREFIX}/events/{all_day['object']['id']}",
            audience="operator",
            secret=OPERATOR_SECRET,
        )
        operator_payload = await operator.json()
        self.assertEqual(operator.status, 200)
        self.assertEqual(operator_payload["object"], all_day["object"])
        self.assertEqual(
            {
                key: operator_payload["object"][key]
                for key in ("all_day", "timezone", "start_at_utc", "end_at_utc", "start_date", "end_date_exclusive")
            },
            {
                "all_day": True,
                "timezone": "Europe/Moscow",
                "start_at_utc": None,
                "end_at_utc": None,
                "start_date": "2026-08-14",
                "end_date_exclusive": "2026-08-16",
            },
        )

        deleted_read = await self._request("GET", f"{PLANNING_PREFIX}/events/{deleted['object']['id']}")
        deleted_read_payload = await deleted_read.json()
        self.assertEqual(deleted_read.status, 200)
        self.assertEqual(deleted_read_payload["object"], deleted_payload["object"])
        self.assertIsNotNone(deleted_read_payload["object"]["deleted_at"])
        self.assertEqual(deleted_read_payload["object"]["version"], 2)

        external_id = external_event.id
        external_read = await self._request("GET", f"{PLANNING_PREFIX}/events/{external_id}")
        external_payload = await external_read.json()
        self.assertEqual(external_read.status, 200)
        self.assertEqual(external_payload["object"]["id"], external_id)
        self.assertEqual(external_payload["object"]["sync_state"], "synced")
        self.assertEqual(external_payload["object"]["provider_id"], "provider-event-1")
        self.assertEqual(external_payload["object"]["provider_calendar_id"], "provider-calendar-1")
        self.assertEqual(external_payload["object"]["version"], 1)

        ha = await self._request(
            "GET",
            f"{PLANNING_PREFIX}/events/{timed['object']['id']}",
            audience="ha",
            secret=HA_SECRET,
        )
        self.assertEqual(ha.status, 403)
        self.assertEqual((await ha.json())["error"]["code"], "audience_forbidden")

        with_query = await self._request("GET", f"{PLANNING_PREFIX}/events/{timed['object']['id']}?view=today")
        self.assertEqual(with_query.status, 400)
        self.assertEqual((await with_query.json())["error"]["code"], "validation_error")

        missing_id = str(uuid.uuid4())
        missing = await self._request("GET", f"{PLANNING_PREFIX}/events/{missing_id}")
        self.assertEqual(missing.status, 404)
        self.assertEqual((await missing.json())["error"]["code"], "not_found")
        malformed = await self._request("GET", f"{PLANNING_PREFIX}/events/not-a-uuid")
        self.assertEqual(malformed.status, 400)
        self.assertEqual((await malformed.json())["error"]["code"], "validation_error")
        self.assertEqual(self._planning_state_snapshot(), before_reads)

    async def test_event_mutations_are_local_only_and_fail_closed_without_writes(self) -> None:
        _, local = await self._create_event(
            key="local-event-safety",
            body={
                "title": "Local event",
                "all_day": False,
                "timezone": "Europe/Moscow",
                "start_at_utc": "2026-08-15T11:00:00Z",
                "end_at_utc": "2026-08-15T12:00:00Z",
            },
        )
        self.assertEqual(
            (local["object"]["sync_state"], local["object"]["provider_id"], local["object"]["provider_calendar_id"]),
            ("local_only", None, None),
        )
        patched = await self._request(
            "PATCH",
            f"{PLANNING_PREFIX}/events/{local['object']['id']}",
            headers={"Idempotency-Key": "local-event-patch", "If-Match": "1"},
            json_body={"title": "Patched local event"},
        )
        patched_payload = await patched.json()
        self.assertEqual(patched.status, 200)
        self.assertEqual(patched_payload["object"]["title"], "Patched local event")
        self.assertEqual(patched_payload["object"]["version"], 2)
        self.assertEqual(patched_payload["object"]["provider_id"], None)
        self.assertEqual(patched_payload["object"]["provider_calendar_id"], None)
        deleted = await self._request(
            "DELETE",
            f"{PLANNING_PREFIX}/events/{local['object']['id']}",
            headers={"Idempotency-Key": "local-event-delete", "If-Match": "2"},
        )
        deleted_payload = await deleted.json()
        self.assertEqual(deleted.status, 200)
        self.assertIsNotNone(deleted_payload["object"]["deleted_at"])
        self.assertEqual(deleted_payload["object"]["version"], 3)

        external = self._seed_external_event()
        before_rejection = self._planning_state_snapshot()
        rejected_patch = await self._request(
            "PATCH",
            f"{PLANNING_PREFIX}/events/{external.id}",
            headers={"Idempotency-Key": "external-event-patch", "If-Match": "1"},
            json_body={"title": "must not change"},
        )
        rejected_patch_payload = await rejected_patch.json()
        self.assertEqual(rejected_patch.status, 409)
        self.assertEqual(rejected_patch_payload["error"]["code"], "event_not_local_only")
        after_patch_rejection = self.service.repository.get_calendar_event(external.id)
        self.assertEqual(after_patch_rejection.to_dict(), external.to_dict())
        self.assertEqual(self._planning_state_snapshot(), before_rejection)

        rejected_delete = await self._request(
            "DELETE",
            f"{PLANNING_PREFIX}/events/{external.id}",
            headers={"Idempotency-Key": "external-event-delete", "If-Match": "1"},
        )
        rejected_delete_payload = await rejected_delete.json()
        self.assertEqual(rejected_delete.status, 409)
        self.assertEqual(rejected_delete_payload["error"]["code"], "event_not_local_only")
        after_delete_rejection = self.service.repository.get_calendar_event(external.id)
        self.assertEqual(after_delete_rejection.deleted_at, None)
        self.assertEqual(after_delete_rejection.version, 1)
        self.assertEqual(after_delete_rejection.provider_id, "provider-event-1")
        self.assertEqual(after_delete_rejection.provider_calendar_id, "provider-calendar-1")
        self.assertEqual(self._planning_state_snapshot(), before_rejection)

    async def test_event_delete_exact_replay_after_tombstone(self) -> None:
        _, local = await self._create_event(
            key="exact-delete-replay-event",
            body={
                "title": "Exact delete replay",
                "all_day": True,
                "timezone": "Europe/Moscow",
                "start_date": "2026-08-19",
                "end_date_exclusive": "2026-08-20",
            },
        )
        request_headers = {"Idempotency-Key": "exact-delete-replay", "If-Match": "1"}
        first = await self._request(
            "DELETE",
            f"{PLANNING_PREFIX}/events/{local['object']['id']}",
            headers=request_headers,
        )
        first_raw = await first.text()
        first_payload = json.loads(first_raw)
        self.assertEqual(first.status, 200)
        canonical_after_first = self.service.repository.get_calendar_event(local["object"]["id"])
        state_after_first = self._planning_state_snapshot()
        self.assertEqual(first_payload["object"], canonical_after_first.to_dict())
        self.assertEqual(canonical_after_first.version, 2)
        self.assertIsNotNone(canonical_after_first.deleted_at)

        with patch.object(self.service.event_service, "delete", wraps=self.service.event_service.delete) as delete:
            replay = await self._request(
                "DELETE",
                f"{PLANNING_PREFIX}/events/{local['object']['id']}",
                headers=request_headers,
            )
        replay_raw = await replay.text()
        replay_payload = json.loads(replay_raw)
        self.assertEqual(replay.status, first.status)
        self.assertEqual(replay_raw, first_raw)
        self.assertEqual(replay_payload, first_payload)
        self.assertEqual(replay_payload["correlation_id"], first_payload["correlation_id"])
        self.assertFalse(delete.called)
        self.assertEqual(self.service.repository.get_calendar_event(local["object"]["id"]).to_dict(), first_payload["object"])
        self.assertEqual(self._planning_state_snapshot(), state_after_first)
        self.assertEqual(
            self.database.connection.execute(
                "SELECT COUNT(*) FROM audit_events WHERE object_id = ? AND action = 'delete'",
                (local["object"]["id"],),
            ).fetchone()[0],
            1,
        )

    async def test_event_patch_exact_replay_after_mutation(self) -> None:
        _, local = await self._create_event(
            key="exact-patch-replay-event",
            body={
                "title": "Exact patch replay",
                "all_day": False,
                "timezone": "Europe/Moscow",
                "start_at_utc": "2026-08-19T09:00:00Z",
                "end_at_utc": "2026-08-19T10:00:00Z",
            },
        )
        request_headers = {"Idempotency-Key": "exact-patch-replay", "If-Match": "1"}
        request_body = {"title": "Patched exactly once"}
        first = await self._request(
            "PATCH",
            f"{PLANNING_PREFIX}/events/{local['object']['id']}",
            headers=request_headers,
            json_body=request_body,
        )
        first_raw = await first.text()
        first_payload = json.loads(first_raw)
        self.assertEqual(first.status, 200)
        canonical_after_first = self.service.repository.get_calendar_event(local["object"]["id"])
        state_after_first = self._planning_state_snapshot()
        self.assertEqual(first_payload["object"], canonical_after_first.to_dict())
        self.assertEqual(canonical_after_first.version, 2)
        self.assertEqual(canonical_after_first.title, "Patched exactly once")

        with patch.object(self.service.event_service, "update", wraps=self.service.event_service.update) as update:
            replay = await self._request(
                "PATCH",
                f"{PLANNING_PREFIX}/events/{local['object']['id']}",
                headers=request_headers,
                json_body=request_body,
            )
        replay_raw = await replay.text()
        replay_payload = json.loads(replay_raw)
        self.assertEqual(replay.status, first.status)
        self.assertEqual(replay_raw, first_raw)
        self.assertEqual(replay_payload, first_payload)
        self.assertEqual(replay_payload["correlation_id"], first_payload["correlation_id"])
        self.assertFalse(update.called)
        self.assertEqual(self.service.repository.get_calendar_event(local["object"]["id"]).to_dict(), first_payload["object"])
        self.assertEqual(self._planning_state_snapshot(), state_after_first)
        self.assertEqual(
            self.database.connection.execute(
                "SELECT COUNT(*) FROM audit_events WHERE object_id = ? AND action = 'update'",
                (local["object"]["id"],),
            ).fetchone()[0],
            1,
        )

    async def test_event_replay_returns_history_after_ownership_changes_but_new_key_is_rejected(self) -> None:
        _, local = await self._create_event(
            key="ownership-change-replay-event",
            body={
                "title": "Ownership change replay",
                "all_day": True,
                "timezone": "Europe/Moscow",
                "start_date": "2026-08-21",
                "end_date_exclusive": "2026-08-22",
            },
        )
        request_headers = {"Idempotency-Key": "ownership-change-replay", "If-Match": "1"}
        request_body = {"title": "Historical local response"}
        first = await self._request(
            "PATCH",
            f"{PLANNING_PREFIX}/events/{local['object']['id']}",
            headers=request_headers,
            json_body=request_body,
        )
        first_raw = await first.text()
        first_payload = json.loads(first_raw)
        self.assertEqual(first.status, 200)
        with self.database.transaction():
            self.database.connection.execute(
                """
                UPDATE calendar_events
                SET sync_state = 'synced', provider_id = ?, provider_calendar_id = ?
                WHERE id = ?
                """,
                ("provider-after-first-write", "calendar-after-first-write", local["object"]["id"]),
            )
        current_provider_event = self.service.repository.get_calendar_event(local["object"]["id"])
        before_replay = self._planning_state_snapshot()
        replay = await self._request(
            "PATCH",
            f"{PLANNING_PREFIX}/events/{local['object']['id']}",
            headers=request_headers,
            json_body=request_body,
        )
        replay_raw = await replay.text()
        self.assertEqual(replay.status, 200)
        self.assertEqual(replay_raw, first_raw)
        self.assertEqual(json.loads(replay_raw), first_payload)
        self.assertEqual(self.service.repository.get_calendar_event(local["object"]["id"]).to_dict(), current_provider_event.to_dict())
        self.assertEqual(self._planning_state_snapshot(), before_replay)

        rejected = await self._request(
            "PATCH",
            f"{PLANNING_PREFIX}/events/{local['object']['id']}",
            headers={"Idempotency-Key": "ownership-change-new-mutation", "If-Match": "2"},
            json_body={"title": "must not change provider event"},
        )
        rejected_payload = await rejected.json()
        self.assertEqual(rejected.status, 409)
        self.assertEqual(rejected_payload["error"]["code"], "event_not_local_only")
        self.assertEqual(self.service.repository.get_calendar_event(local["object"]["id"]).to_dict(), current_provider_event.to_dict())
        self.assertEqual(self._planning_state_snapshot(), before_replay)

    async def test_projects_are_read_only_and_project_filter_is_bounded(self) -> None:
        project = self.service.repository.create_project(
            name="Synthetic project",
            context=MutationContext(
                audience="operator",
                actor_id="synthetic-test",
                actor_type="operator",
                surface="operator",
            ),
        )
        projects = await self._request("GET", f"{PLANNING_PREFIX}/projects?limit=1")
        project_payload = await projects.json()
        self.assertEqual(projects.status, 200)
        self.assertEqual(project_payload["pagination"]["limit"], 1)
        self.assertEqual(project_payload["items"][0]["id"], project.id)

        _, task = await self._create_task(key="project-task", project_id=project.id, due_date="2026-08-11")
        filtered = await self._request(
            "GET",
            f"{PLANNING_PREFIX}/tasks?view=today&project_id={project.id}",
        )
        self.assertEqual([item["id"] for item in (await filtered.json())["items"]], [task["object"]["id"]])
        create_project = await self._request(
            "POST",
            f"{PLANNING_PREFIX}/projects",
            headers={"Idempotency-Key": "project-write"},
            json_body={"name": "not an allowed route"},
        )
        self.assertEqual(create_project.status, 404)

    async def test_event_timed_all_day_exclusive_range_and_delete(self) -> None:
        timed = await self._request(
            "POST",
            f"{PLANNING_PREFIX}/events",
            headers={"Idempotency-Key": "timed-event"},
            json_body={
                "title": "Timed event",
                "all_day": False,
                "timezone": "Europe/Moscow",
                "start_at_utc": "2026-08-12T09:00:00Z",
                "end_at_utc": "2026-08-12T10:00:00Z",
            },
        )
        timed_payload = await timed.json()
        self.assertEqual(timed.status, 200)
        self.assertFalse(timed_payload["object"]["all_day"])

        all_day = await self._request(
            "POST",
            f"{PLANNING_PREFIX}/events",
            headers={"Idempotency-Key": "all-day-event"},
            json_body={
                "title": "All day event",
                "all_day": True,
                "timezone": "Europe/Moscow",
                "start_date": "2026-08-14",
                "end_date_exclusive": "2026-08-16",
            },
        )
        all_day_payload = await all_day.json()
        self.assertEqual(all_day.status, 200)
        self.assertTrue(all_day_payload["object"]["all_day"])
        self.assertEqual(all_day_payload["object"]["end_date_exclusive"], "2026-08-16")

        ranged = await self._request(
            "GET",
            f"{PLANNING_PREFIX}/events?from=2026-08-12T00:00:00Z&to=2026-08-13T00:00:00Z",
        )
        ranged_payload = await ranged.json()
        self.assertEqual(ranged.status, 200)
        self.assertEqual([item["title"] for item in ranged_payload["items"]], ["Timed event"])

        deleted = await self._request(
            "DELETE",
            f"{PLANNING_PREFIX}/events/{timed_payload['object']['id']}",
            headers={"Idempotency-Key": "delete-event", "If-Match": "1"},
        )
        deleted_payload = await deleted.json()
        self.assertEqual(deleted.status, 200)
        self.assertIsNotNone(deleted_payload["object"]["deleted_at"])
        self.assertEqual(self.database.connection.execute("SELECT COUNT(*) FROM calendar_events").fetchone()[0], 2)

    async def test_event_invalid_shapes_recurrence_and_provider_boundary(self) -> None:
        mixed = await self._request(
            "POST",
            f"{PLANNING_PREFIX}/events",
            headers={"Idempotency-Key": "mixed-event"},
            json_body={
                "title": "Mixed",
                "all_day": True,
                "timezone": "Europe/Moscow",
                "start_date": "2026-08-14",
                "end_date_exclusive": "2026-08-15",
                "start_at_utc": "2026-08-14T09:00:00Z",
                "end_at_utc": "2026-08-14T10:00:00Z",
            },
        )
        self.assertEqual(mixed.status, 400)

        recurrence = await self._request(
            "POST",
            f"{PLANNING_PREFIX}/events",
            headers={"Idempotency-Key": "recurrence-event"},
            json_body={
                "title": "Recurring",
                "all_day": True,
                "timezone": "Europe/Moscow",
                "start_date": "2026-08-14",
                "end_date_exclusive": "2026-08-15",
                "recurrence_rule": "FREQ=DAILY",
            },
        )
        self.assertEqual(recurrence.status, 400)

        provider_fields = await self._request(
            "POST",
            f"{PLANNING_PREFIX}/events",
            headers={"Idempotency-Key": "provider-fields"},
            json_body={
                "title": "Provider injection",
                "all_day": True,
                "timezone": "Europe/Moscow",
                "start_date": "2026-08-14",
                "end_date_exclusive": "2026-08-15",
                "provider_id": "remote-provider",
            },
        )
        self.assertEqual(provider_fields.status, 400)
        self.assertNotIn("/internal/planning/v1/execute", {resource.canonical for resource in self.app.router.resources()})

    async def test_freshness_pagination_error_and_uuid4_envelopes(self) -> None:
        for index in range(2):
            await self._create_task(
                key=f"pagination-{index}",
                title=f"task-{index}",
                due_date="2026-08-11",
            )
        listed = await self._request("GET", f"{PLANNING_PREFIX}/tasks?view=today&limit=1")
        payload = await listed.json()
        self.assertEqual(payload["schemaVersion"], "planning.v1")
        self.assertEqual(payload["kind"], "list")
        self.assertEqual(payload["sourceStatus"], "current")
        self.assertEqual(payload["lastSyncedAt"], NOW)
        self.assertEqual(payload["staleAfter"], "2026-08-11T08:05:00Z")
        self.assertTrue(payload["pagination"]["has_more"])
        self.assertEqual(payload["pagination"]["next_offset"], 1)
        self.assertEqual(uuid.UUID(payload["correlation_id"]).version, 4)

        _, created = await self._create_task(key="envelope-object")
        self.assertEqual(created["schemaVersion"], "planning.v1")
        self.assertEqual(uuid.UUID(created["correlation_id"]).version, 4)
        error = await self._request("GET", f"{PLANNING_PREFIX}/events?from=not-a-time&to=also-not-a-time")
        error_payload = await error.json()
        self.assertEqual(error.status, 400)
        self.assertEqual(error_payload["kind"], "error")
        self.assertEqual(error_payload["http_status"], 400)
        self.assertEqual(uuid.UUID(error_payload["correlation_id"]).version, 4)

    async def test_status_is_content_free_and_audience_capability_scoped(self) -> None:
        response = await self._request("GET", f"{PLANNING_PREFIX}/status")
        payload = await response.json()
        serialized = json.dumps(payload)
        self.assertEqual(payload["storageStatus"], "available")
        self.assertEqual(payload["capabilities"]["tasks"], ["read", "create", "update", "complete", "archive"])
        for forbidden in ("Synthetic", "title", "notes", "Telegram", "/", "secret", "token"):
            self.assertNotIn(forbidden, serialized)

        ha = await self._request("GET", f"{PLANNING_PREFIX}/status", audience="ha", secret=HA_SECRET)
        ha_payload = await ha.json()
        self.assertEqual(ha_payload["capabilities"]["tasks"], ["read"])

    async def test_range_timezone_time_and_query_limits(self) -> None:
        too_large = await self._request(
            "GET",
            f"{PLANNING_PREFIX}/events?from=2026-01-01T00:00:00Z&to=2027-01-03T00:00:00Z",
        )
        self.assertEqual(too_large.status, 413)
        self.assertEqual((await too_large.json())["error"]["code"], "range_too_large")

        bad_timezone = await self._request(
            "POST",
            f"{PLANNING_PREFIX}/reminders",
            headers={"Idempotency-Key": "bad-timezone"},
            json_body={
                "title": "bad timezone",
                "due_at_utc": "2026-08-14T07:30:00Z",
                "timezone": "Mars/Phobos",
            },
        )
        self.assertEqual(bad_timezone.status, 400)

        bad_time = await self._request(
            "POST",
            f"{PLANNING_PREFIX}/tasks",
            headers={"Idempotency-Key": "bad-time"},
            json_body={
                "title": "bad time",
                "priority": "normal",
                "due_date": "2026-08-14",
                "due_time": "25:61",
                "timezone": "Europe/Moscow",
            },
        )
        self.assertEqual(bad_time.status, 400)

        unknown_query = await self._request("GET", f"{PLANNING_PREFIX}/tasks?view=today&command=echo")
        self.assertEqual(unknown_query.status, 400)

        huge_limit = await self._request("GET", f"{PLANNING_PREFIX}/tasks?view=today&limit=101")
        self.assertEqual(huge_limit.status, 400)

    async def test_rate_limit_is_deterministic_and_per_process(self) -> None:
        self.app["planning_authenticator"].rate_limiter = InProcessRateLimiter(max_requests=2)
        first = await self._request("GET", f"{PLANNING_PREFIX}/status")
        second = await self._request("GET", f"{PLANNING_PREFIX}/status")
        third = await self._request("GET", f"{PLANNING_PREFIX}/status")
        self.assertEqual((first.status, second.status), (200, 200))
        self.assertEqual(third.status, 429)
        self.assertEqual((await third.json())["error"]["code"], "rate_limited")

    async def test_feature_gate_is_off_without_explicit_planning_api_enablement(self) -> None:
        app = web.Application()
        app["settings"] = SimpleNamespace()
        setup_internal_routes(app)
        paths = {resource.canonical for resource in app.router.resources()}
        self.assertNotIn(f"{PLANNING_PREFIX}/status", paths)


class PlanningApiConfigTests(unittest.TestCase):
    def test_enabled_api_requires_independent_audience_secrets(self) -> None:
        from unittest.mock import patch

        required = {
            "TELEGRAM_BOT_TOKEN": "synthetic",
            "TELEGRAM_WEBHOOK_SECRET": "synthetic",
            "TELEGRAM_ALLOWED_USER_IDS": "1",
            "TELEGRAM_ADMIN_CHAT_ID": "1",
            "HA_LONG_LIVED_TOKEN": "synthetic",
            "INTERNAL_WEBHOOK_SECRET": "synthetic",
            "PLANNING_API_ENABLED": "true",
            "PLANNING_HA_SECRET": "ha",
        }
        with patch.dict("os.environ", required, clear=True):
            from app.config import Settings

            with self.assertRaises(RuntimeError):
                Settings.from_env()
