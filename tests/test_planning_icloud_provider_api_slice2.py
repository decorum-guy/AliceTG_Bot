from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app.planning.api.auth import AuthenticatedPlanningContext
from app.planning.api.routes import PLANNING_PREFIX, setup_planning_routes
from app.planning.api.service import PlanningApiService
from app.planning.db import PlanningDatabase
from app.planning.providers.cache import ProviderCalendarCache
from app.planning.providers.icloud import CalDavWriteResponse, ICloudCalDavProvider
from tests.planning.test_icloud_provider import NOW, WINDOW, WriteFixtureCalDavTransport


PANEL_SECRET = "synthetic-panel-agent-secret"
HA_SECRET = "synthetic-ha-secret"
OPERATOR_SECRET = "synthetic-operator-secret"
INTERNAL_SECRET = "synthetic-existing-internal-secret"


class PlanningICloudProviderApiSlice2Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = PlanningDatabase(Path(self.temp.name) / "planning.sqlite3")
        self.transport = WriteFixtureCalDavTransport()
        self.provider = ICloudCalDavProvider(
            transport=self.transport,
            account_name="synthetic@example.invalid",
        )
        self.cache = ProviderCalendarCache(
            self.database,
            provider=self.provider,
            provider_name="icloud",
            account_id=self.provider.account_id_for("synthetic@example.invalid"),
            display_label="iCloud",
            enabled=True,
            configured=True,
            now_fn=lambda: NOW,
        )
        await self.cache.refresh(WINDOW)
        self.service = PlanningApiService(
            self.database,
            now_fn=lambda: NOW,
            provider_cache=self.cache,
            icloud_writes_enabled=True,
        )
        self.settings = SimpleNamespace(
            planning_api_enabled=True,
            internal_webhook_secret=INTERNAL_SECRET,
            planning_ha_secret=HA_SECRET,
            planning_panel_agent_secret=PANEL_SECRET,
            planning_operator_secret=OPERATOR_SECRET,
            planning_api_rate_limit_per_minute=120,
            planning_api_stale_after_seconds=300,
            planning_default_timezone="Europe/Moscow",
            planning_icloud_writes_enabled=True,
        )
        self.app = web.Application()
        self.app["settings"] = self.settings
        self.app["planning_database"] = self.database
        self.app["planning_api_service"] = self.service
        self.app["planning_icloud_cache"] = self.cache
        setup_planning_routes(self.app)
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await self.provider.close()
        self.database.close()
        self.temp.cleanup()

    @staticmethod
    def _headers(
        audience: str = "panel-agent",
        secret: str = PANEL_SECRET,
        **extra: str,
    ) -> dict[str, str]:
        return {
            "X-Internal-Secret": INTERNAL_SECRET,
            "X-Planning-Audience": audience,
            "X-Planning-Secret": secret,
            **extra,
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        audience: str = "panel-agent",
        secret: str = PANEL_SECRET,
        json_body: object | None = None,
        headers: dict[str, str] | None = None,
    ):
        request_headers = self._headers(audience, secret)
        if headers:
            request_headers.update(headers)
        kwargs: dict[str, object] = {"headers": request_headers}
        if json_body is not None:
            kwargs["json"] = json_body
        return await self.client.request(method, path, **kwargs)

    def _calendar_ids(self) -> tuple[str, str]:
        rows = self.database.connection.execute(
            "SELECT provider_calendar_id FROM provider_calendars "
            "WHERE source_id = ? ORDER BY can_write DESC, provider_calendar_id",
            (self.cache.source_id,),
        ).fetchall()
        self.assertEqual(len(rows), 2)
        return str(rows[0][0]), str(rows[1][0])

    async def _create_event(self, *, key: str, calendar_id: str, all_day: bool = False):
        body: dict[str, object] = {
            "calendar_id": calendar_id,
            "title": "API synthetic event",
            "notes": "private notes",
            "location": "private room",
            "all_day": all_day,
            "timezone": "Europe/Moscow",
        }
        if all_day:
            body.update({"start_date": "2026-08-18", "end_date_exclusive": "2026-08-20"})
        else:
            body.update({
                "start_at_utc": "2026-08-21T07:00:00Z",
                "end_at_utc": "2026-08-21T08:00:00Z",
            })
        response = await self._request(
            "POST",
            f"{PLANNING_PREFIX}/provider-events",
            headers={"Idempotency-Key": key},
            json_body=body,
        )
        return response, body

    async def test_route_auth_gate_and_safe_destination_projection(self) -> None:
        response = await self._request("GET", f"{PLANNING_PREFIX}/calendar-destinations")
        self.assertEqual(response.status, 200)
        payload = await response.json()
        self.assertEqual(payload["capabilities"]["canCreateCalendar"], True)
        self.assertEqual(len(payload["items"]), 2)
        self.assertEqual({item["label"] for item in payload["items"]}, {"Same name"})
        self.assertEqual(
            set(payload["items"][0]),
            {"id", "label", "color", "providerKind", "writeState", "canCreateEvent", "canDeleteCalendar"},
        )
        self.assertNotIn("collection_ref", json.dumps(payload))
        self.assertNotIn("etag", json.dumps(payload).lower())
        self.assertNotIn("account", json.dumps(payload).lower())
        self.assertNotIn("fixture.invalid", json.dumps(payload))

        _, candidate_id = self._calendar_ids()
        self.database.connection.execute(
            "UPDATE provider_calendars SET can_write = NULL WHERE provider_calendar_id = ?",
            (candidate_id,),
        )
        candidate_response = await self._request("GET", f"{PLANNING_PREFIX}/calendar-destinations")
        candidate = next(item for item in (await candidate_response.json())["items"] if item["id"] == candidate_id)
        self.assertEqual(candidate["writeState"], "candidate")
        self.assertTrue(candidate["canCreateEvent"])

        ha = await self._request(
            "GET",
            f"{PLANNING_PREFIX}/calendar-destinations",
            audience="ha",
            secret=HA_SECRET,
        )
        self.assertEqual(ha.status, 403)
        operator = await self._request(
            "GET",
            f"{PLANNING_PREFIX}/calendar-destinations",
            audience="operator",
            secret=OPERATOR_SECRET,
        )
        self.assertEqual(operator.status, 403)

        calendar_id, _ = self._calendar_ids()
        self.service.icloud_writes_enabled = False
        gated_destinations = await self._request("GET", f"{PLANNING_PREFIX}/calendar-destinations")
        gated_payload = await gated_destinations.json()
        self.assertFalse(gated_payload["capabilities"]["canCreateCalendar"])
        self.assertTrue(all(not item["canCreateEvent"] for item in gated_payload["items"]))
        self.assertTrue(all(not item["canDeleteCalendar"] for item in gated_payload["items"]))
        gated_candidate = next(item for item in gated_payload["items"] if item["id"] == candidate_id)
        self.assertEqual(gated_candidate["writeState"], "candidate")

        gated, _ = await self._create_event(key="gate-off", calendar_id=calendar_id)
        self.assertEqual(gated.status, 503)
        self.assertEqual((await gated.json())["error"]["code"], "provider_write_disabled")
        self.service.icloud_writes_enabled = True

        self.cache.configured = False
        unavailable_destinations = await self._request("GET", f"{PLANNING_PREFIX}/calendar-destinations")
        unavailable_payload = await unavailable_destinations.json()
        self.assertFalse(unavailable_payload["capabilities"]["canCreateCalendar"])
        self.assertTrue(all(not item["canCreateEvent"] for item in unavailable_payload["items"]))
        self.assertTrue(all(not item["canDeleteCalendar"] for item in unavailable_payload["items"]))
        unavailable_candidate = next(item for item in unavailable_payload["items"] if item["id"] == candidate_id)
        self.assertEqual(unavailable_candidate["writeState"], "candidate")
        self.cache.configured = True

        restored_destinations = await self._request("GET", f"{PLANNING_PREFIX}/calendar-destinations")
        restored_payload = await restored_destinations.json()
        self.assertTrue(restored_payload["capabilities"]["canCreateCalendar"])
        restored_candidate = next(item for item in restored_payload["items"] if item["id"] == candidate_id)
        self.assertTrue(restored_candidate["canCreateEvent"])
        self.assertTrue(restored_candidate["canDeleteCalendar"])

        invalid, _ = await self._create_event(key="bad-calendar", calendar_id="https://example.invalid/calendar")
        self.assertEqual(invalid.status, 400)
        self.assertEqual((await invalid.json())["error"]["code"], "validation_error")

        calendar_id, _ = self._calendar_ids()
        for index, field in enumerate(("url", "etag", "provider_id", "source_ref", "recurrence", "uid")):
            body = {
                "calendar_id": calendar_id,
                "title": "invalid provider input",
                "all_day": False,
                "timezone": "Europe/Moscow",
                "start_at_utc": "2026-08-23T07:00:00Z",
                "end_at_utc": "2026-08-23T08:00:00Z",
                field: "forbidden",
            }
            rejected = await self._request(
                "POST",
                f"{PLANNING_PREFIX}/provider-events",
                headers={"Idempotency-Key": f"invalid-provider-{index}"},
                json_body=body,
            )
            self.assertEqual(rejected.status, 400, field)

    async def test_provider_event_create_replay_and_all_day(self) -> None:
        calendar_id, _ = self._calendar_ids()
        first, body = await self._create_event(key="create-event", calendar_id=calendar_id)
        first_bytes = await first.read()
        self.assertEqual(first.status, 200)
        first_payload = json.loads(first_bytes)
        serialized = first_bytes.decode()
        self.assertNotIn("etag", serialized.lower())
        self.assertNotIn("resource_ref", serialized)
        self.assertNotIn("fixture.invalid", serialized)
        event = first_payload["object"]
        self.assertEqual(event["source"], "calendar-provider")
        self.assertEqual(event["version"], 1)
        self.assertEqual(event["provider_calendar_id"], calendar_id)
        self.assertEqual(first_payload["mutationCapabilities"]["canEdit"], True)
        self.assertEqual([call[0] for call in self.transport.write_calls].count("PUT_NEW"), 1)

        replay = await self._request(
            "POST",
            f"{PLANNING_PREFIX}/provider-events",
            headers={"Idempotency-Key": "create-event"},
            json_body=body,
        )
        self.assertEqual(replay.status, 200)
        self.assertEqual(first_bytes, await replay.read())
        self.assertEqual([call[0] for call in self.transport.write_calls].count("PUT_NEW"), 1)

        self.service.icloud_writes_enabled = False
        replay_after_gate_change = await self._request(
            "POST",
            f"{PLANNING_PREFIX}/provider-events",
            headers={"Idempotency-Key": "create-event"},
            json_body=body,
        )
        self.assertEqual(replay_after_gate_change.status, 200)
        self.assertEqual(first_bytes, await replay_after_gate_change.read())
        different_request = dict(body)
        different_request["title"] = "different request"
        conflict = await self._request(
            "POST",
            f"{PLANNING_PREFIX}/provider-events",
            headers={"Idempotency-Key": "create-event"},
            json_body=different_request,
        )
        self.assertEqual(conflict.status, 409)
        self.assertEqual((await conflict.json())["error"]["code"], "idempotency_conflict")
        self.service.icloud_writes_enabled = True

        all_day, _ = await self._create_event(key="create-all-day", calendar_id=calendar_id, all_day=True)
        self.assertEqual(all_day.status, 200)
        all_day_event = (await all_day.json())["object"]
        self.assertTrue(all_day_event["all_day"])
        self.assertEqual(all_day_event["start_date"], "2026-08-18")
        self.assertEqual(all_day_event["end_date_exclusive"], "2026-08-20")

    async def test_provider_event_update_uses_canonical_version_and_internal_etag(self) -> None:
        calendar_id, _ = self._calendar_ids()
        created, _ = await self._create_event(key="update-create", calendar_id=calendar_id)
        created_event = (await created.json())["object"]
        event_id = created_event["id"]
        provider_puts_before = len([call for call in self.transport.write_calls if call[0] == "PUT_MATCH"])

        update_body = {"title": "Updated title", "notes": "Updated notes", "location": "Updated room"}
        updated = await self._request(
            "PATCH",
            f"{PLANNING_PREFIX}/provider-events/{event_id}",
            headers={"Idempotency-Key": "update-event", "If-Match": "1"},
            json_body=update_body,
        )
        updated_bytes = await updated.read()
        self.assertEqual(updated.status, 200)
        updated_payload = json.loads(updated_bytes)
        updated_event = updated_payload["object"]
        self.assertEqual(updated_event["id"], event_id)
        self.assertEqual(updated_event["version"], 2)
        self.assertEqual(updated_event["title"], "Updated title")
        self.assertEqual(updated_event["notes"], "Updated notes")
        self.assertEqual(updated_event["location"], "Updated room")
        self.assertEqual(len([call for call in self.transport.write_calls if call[0] == "PUT_MATCH"]), provider_puts_before + 1)

        exact_replay = await self._request(
            "PATCH",
            f"{PLANNING_PREFIX}/provider-events/{event_id}",
            headers={"Idempotency-Key": "update-event", "If-Match": "1"},
            json_body=update_body,
        )
        self.assertEqual(exact_replay.status, 200)
        self.assertEqual(updated_bytes, await exact_replay.read())
        self.assertEqual(len([call for call in self.transport.write_calls if call[0] == "PUT_MATCH"]), provider_puts_before + 1)

        stale = await self._request(
            "PATCH",
            f"{PLANNING_PREFIX}/provider-events/{event_id}",
            headers={"Idempotency-Key": "update-stale", "If-Match": "1"},
            json_body={"title": "must not write"},
        )
        self.assertEqual(stale.status, 409)
        self.assertEqual((await stale.json())["error"]["code"], "version_conflict")
        self.assertEqual(
            self.database.connection.execute(
                "SELECT COUNT(*) FROM idempotency_keys WHERE audience = 'panel-agent' AND key = ?",
                ("update-stale",),
            ).fetchone()[0],
            0,
        )
        self.assertEqual(len([call for call in self.transport.write_calls if call[0] == "PUT_MATCH"]), provider_puts_before + 1)

        self.transport.next_status = 412
        etag_conflict = await self._request(
            "PATCH",
            f"{PLANNING_PREFIX}/provider-events/{event_id}",
            headers={"Idempotency-Key": "update-etag-conflict", "If-Match": "2"},
            json_body={"title": "provider rejects"},
        )
        self.assertEqual(etag_conflict.status, 409)
        self.assertEqual((await etag_conflict.json())["error"]["code"], "provider_etag_conflict")

    async def test_provider_event_delete_tombstones_immediately_and_old_route_stays_local_only(self) -> None:
        calendar_id, _ = self._calendar_ids()
        created, _ = await self._create_event(key="delete-create", calendar_id=calendar_id)
        event = (await created.json())["object"]
        event_id = event["id"]
        deleted = await self._request(
            "DELETE",
            f"{PLANNING_PREFIX}/provider-events/{event_id}",
            headers={"Idempotency-Key": "delete-event", "If-Match": "1"},
        )
        deleted_bytes = await deleted.read()
        self.assertEqual(deleted.status, 200)
        tombstone = json.loads(deleted_bytes)["object"]
        self.assertEqual(tombstone["id"], event_id)
        self.assertIsNotNone(tombstone["deleted_at"])
        self.assertEqual(tombstone["version"], 2)
        exact_replay = await self._request(
            "DELETE",
            f"{PLANNING_PREFIX}/provider-events/{event_id}",
            headers={"Idempotency-Key": "delete-event", "If-Match": "1"},
        )
        self.assertEqual(exact_replay.status, 200)
        self.assertEqual(deleted_bytes, await exact_replay.read())
        self.assertEqual([call[0] for call in self.transport.write_calls].count("DELETE_MATCH"), 1)
        readback = await self._request("GET", f"{PLANNING_PREFIX}/events/{event_id}")
        self.assertEqual(readback.status, 200)
        self.assertIsNotNone((await readback.json())["object"]["deleted_at"])

        old_route = await self._request(
            "DELETE",
            f"{PLANNING_PREFIX}/events/{event_id}",
            headers={"Idempotency-Key": "old-local-delete", "If-Match": "2"},
        )
        self.assertEqual(old_route.status, 409)
        self.assertEqual((await old_route.json())["error"]["code"], "event_not_local_only")

        old_patch = await self._request(
            "PATCH",
            f"{PLANNING_PREFIX}/events/{event_id}",
            headers={"Idempotency-Key": "old-local-patch", "If-Match": "2"},
            json_body={"title": "must remain local-only"},
        )
        self.assertEqual(old_patch.status, 409)
        self.assertEqual((await old_patch.json())["error"]["code"], "event_not_local_only")

    async def test_unsafe_provider_event_is_read_only_and_in_progress_claim_never_retries(self) -> None:
        calendar_id, _ = self._calendar_ids()
        unsafe_row = self.database.connection.execute(
            "SELECT ce.id, ce.version FROM calendar_events ce "
            "JOIN provider_event_cache pec ON pec.canonical_event_id = ce.id "
            "WHERE pec.write_safe = 0 AND ce.deleted_at IS NULL LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(unsafe_row)
        unsafe = await self._request(
            "PATCH",
            f"{PLANNING_PREFIX}/provider-events/{unsafe_row['id']}",
            headers={"Idempotency-Key": "unsafe-update", "If-Match": str(unsafe_row["version"])},
            json_body={"title": "must remain read-only"},
        )
        self.assertEqual(unsafe.status, 409)
        self.assertEqual((await unsafe.json())["error"]["code"], "provider_read_only")

        body = {
            "calendar_id": calendar_id,
            "title": "in progress",
            "notes": None,
            "location": None,
            "all_day": False,
            "timezone": "Europe/Moscow",
            "start_at_utc": "2026-08-22T07:00:00Z",
            "end_at_utc": "2026-08-22T08:00:00Z",
            "start_date": None,
            "end_date_exclusive": None,
        }
        auth = AuthenticatedPlanningContext(
            audience="panel-agent",
            actor_id="planning-panel-agent",
            actor_type="service",
            surface="panel-agent",
        )
        request_hash = self.service._request_hash(
            auth=auth,
            route_key="POST /provider-events",
            object_id=None,
            body=body,
            expected_version=None,
        )
        with self.database.transaction():
            claim = self.service.repository.claim_idempotency(
                audience="panel-agent",
                key="in-progress-event",
                request_hash=request_hash,
            )
        self.assertTrue(claim.is_new)
        self.service.icloud_writes_enabled = False
        before = len([call for call in self.transport.write_calls if call[0] == "PUT_NEW"])
        in_progress = await self._request(
            "POST",
            f"{PLANNING_PREFIX}/provider-events",
            headers={"Idempotency-Key": "in-progress-event"},
            json_body=body,
        )
        self.assertEqual(in_progress.status, 409)
        in_progress_payload = await in_progress.json()
        self.assertEqual(in_progress_payload["error"]["code"], "idempotency_in_progress")
        self.assertFalse(in_progress_payload["error"]["retryable"])
        self.assertEqual(len([call for call in self.transport.write_calls if call[0] == "PUT_NEW"]), before)
        self.service.icloud_writes_enabled = True

    async def test_failed_new_preflight_does_not_poison_idempotency_key(self) -> None:
        calendar_id, _ = self._calendar_ids()
        self.service.icloud_writes_enabled = False
        rejected, body = await self._create_event(key="preflight-no-claim", calendar_id=calendar_id)
        self.assertEqual(rejected.status, 503)
        self.assertEqual(
            self.database.connection.execute(
                "SELECT COUNT(*) FROM idempotency_keys WHERE audience = 'panel-agent' AND key = ?",
                ("preflight-no-claim",),
            ).fetchone()[0],
            0,
        )
        self.service.icloud_writes_enabled = True
        recovered, _ = await self._create_event(key="preflight-no-claim", calendar_id=calendar_id)
        self.assertEqual(recovered.status, 200)
        self.assertEqual((await recovered.json())["object"]["title"], body["title"])

    async def test_uncertain_provider_outcome_is_stable_and_not_replayed(self) -> None:
        calendar_id, _ = self._calendar_ids()
        self.transport.readback_override = CalDavWriteResponse(200, None, b"broken")
        response, _ = await self._create_event(key="uncertain-event", calendar_id=calendar_id)
        self.assertEqual(response.status, 503)
        payload = await response.json()
        self.assertEqual(payload["error"]["code"], "provider_mutation_uncertain")
        self.assertFalse(payload["error"]["retryable"])
        self.assertEqual(payload["error"]["details"]["mutationState"], "uncertain")
        self.assertNotIn("fixture.invalid", json.dumps(payload))
        puts = len([call for call in self.transport.write_calls if call[0] == "PUT_NEW"])
        self.transport.readback_override = None
        replay, _ = await self._create_event(key="uncertain-event", calendar_id=calendar_id)
        self.assertEqual(replay.status, 503)
        self.assertEqual((await replay.json())["error"]["code"], "provider_mutation_uncertain")
        self.assertEqual(len([call for call in self.transport.write_calls if call[0] == "PUT_NEW"]), puts)

    async def test_calendar_create_delete_reconciles_destinations_and_rejects_unsafe_fields(self) -> None:
        create = await self._request(
            "POST",
            f"{PLANNING_PREFIX}/provider-calendars",
            headers={"Idempotency-Key": "create-calendar"},
            json_body={"display_name": "New synthetic calendar", "color": "#123456"},
        )
        self.assertEqual(create.status, 200)
        destination = (await create.json())["destination"]
        calendar_id = destination["id"]
        self.assertTrue(calendar_id.startswith("icloud_calendar_"))
        destinations = await self._request("GET", f"{PLANNING_PREFIX}/calendar-destinations")
        self.assertIn(calendar_id, {item["id"] for item in (await destinations.json())["items"]})

        delete = await self._request(
            "DELETE",
            f"{PLANNING_PREFIX}/provider-calendars/{calendar_id}",
            headers={"Idempotency-Key": "delete-calendar"},
        )
        self.assertEqual(delete.status, 200)
        delete_response_bytes = await delete.read()
        self.assertTrue(json.loads(delete_response_bytes)["deleted"])
        delete_bytes = await self._request(
            "DELETE",
            f"{PLANNING_PREFIX}/provider-calendars/{calendar_id}",
            headers={"Idempotency-Key": "delete-calendar"},
        )
        self.assertEqual(delete_bytes.status, 200)
        self.assertEqual((await delete_bytes.read()), delete_response_bytes)
        self.assertEqual([call[0] for call in self.transport.write_calls].count("DELETE_CALENDAR"), 1)
        after = await self._request("GET", f"{PLANNING_PREFIX}/calendar-destinations")
        self.assertNotIn(calendar_id, {item["id"] for item in (await after.json())["items"]})

        unsafe = await self._request(
            "POST",
            f"{PLANNING_PREFIX}/provider-calendars",
            headers={"Idempotency-Key": "unsafe-calendar"},
            json_body={"display_name": "bad", "url": "https://example.invalid"},
        )
        self.assertEqual(unsafe.status, 400)
        self.assertEqual((await unsafe.json())["error"]["code"], "validation_error")

    async def test_provider_not_configured_is_bounded(self) -> None:
        self.cache.configured = False
        calendar_id, _ = self._calendar_ids()
        response, _ = await self._create_event(key="not-configured", calendar_id=calendar_id)
        self.assertEqual(response.status, 503)
        self.assertEqual((await response.json())["error"]["code"], "provider_not_configured")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
