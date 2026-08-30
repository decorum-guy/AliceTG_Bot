from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app.planning import PlanningDatabase
from app.planning.api.routes import setup_planning_routes
from app.planning.providers.cache import ProviderCalendarCache
from app.planning.providers.contracts import (
    CalendarWindow,
    ExternalCalendar,
    ExternalProviderAccount,
    ProviderFailureCode,
    ProviderFetchError,
    ProviderTimeoutError,
)
from app.planning.providers.sync import ICloudCalendarRefreshLoop


NOW = "2026-08-27T09:00:00Z"
WINDOW = CalendarWindow(
    start=datetime(2026, 8, 1, tzinfo=timezone.utc),
    end=datetime(2026, 9, 1, tzinfo=timezone.utc),
)
INTERNAL_SECRET = "synthetic-internal-secret"
PANEL_SECRET = "synthetic-panel-agent-secret"
HA_SECRET = "synthetic-ha-secret"


class DiscoveryProvider:
    def __init__(self) -> None:
        self.calendars: list[ExternalCalendar] = []
        self.fail_next: Exception | None = None
        self.discover_calls = 0
        self.list_calls = 0
        self.fetch_calls = 0
        self.active_discoveries = 0
        self.max_active_discoveries = 0

    async def discover_account(self) -> ExternalProviderAccount:
        self.discover_calls += 1
        self.active_discoveries += 1
        self.max_active_discoveries = max(self.max_active_discoveries, self.active_discoveries)
        try:
            await asyncio.sleep(0)
            if self.fail_next is not None:
                error = self.fail_next
                self.fail_next = None
                raise error
            return ExternalProviderAccount("icloud", "opaque-account", "iCloud")
        finally:
            self.active_discoveries -= 1

    async def list_calendars(self) -> list[ExternalCalendar]:
        self.list_calls += 1
        return list(self.calendars)

    async def fetch_events(self, calendar: ExternalCalendar, window: CalendarWindow):
        del calendar, window
        self.fetch_calls += 1
        return []


def calendar(calendar_id: str, name: str, color: str) -> ExternalCalendar:
    return ExternalCalendar(
        provider_calendar_id=calendar_id,
        display_name=name,
        color=color,
        enabled=True,
        fetch_ref=f"server-only-ref-{calendar_id}",
    )


class CalendarSourceRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = PlanningDatabase(Path(self.temp.name) / "planning.sqlite3")
        self.provider = DiscoveryProvider()
        self.cache = ProviderCalendarCache(
            self.database,
            provider=self.provider,
            provider_name="icloud",
            account_id="opaque-account",
            display_label="iCloud",
            enabled=True,
            configured=True,
            now_fn=lambda: NOW,
        )

    async def asyncTearDown(self) -> None:
        self.database.close()
        self.temp.cleanup()

    async def test_refresh_discovers_and_reconciles_calendar_identity_not_display_fields(self) -> None:
        self.provider.calendars = [
            calendar("stable-a", "Team", "#112233"),
            calendar("stable-b", "Team", "#445566"),
        ]
        first = await self.cache.refresh(WINDOW)
        self.assertEqual(first.status, "current")
        self.assertEqual(self.provider.discover_calls, 1)
        self.assertEqual(self.provider.list_calls, 1)

        self.provider.calendars = [
            calendar("stable-a", "Renamed Team", "#112233"),
            calendar("stable-c", "Team", "#778899"),
        ]
        second = await self.cache.refresh(WINDOW)
        self.assertEqual(second.status, "current")
        sources = self.cache.source_metadata()
        calendars = {item["calendarId"]: item for item in sources[1]["calendars"]}
        self.assertEqual(calendars["stable-a"]["displayName"], "Renamed Team")
        self.assertEqual(calendars["stable-a"]["color"], "#112233")
        self.assertEqual(calendars["stable-b"]["status"], "disabled")
        self.assertFalse(calendars["stable-b"]["enabled"])
        self.assertEqual(calendars["stable-c"]["displayName"], "Team")
        self.assertEqual(calendars["stable-c"]["color"], "#778899")
        self.assertEqual(len({item["calendarId"] for item in sources[1]["calendars"]}), 3)

    async def test_failure_preserves_confirmed_metadata_and_refreshes_are_serialized(self) -> None:
        self.provider.calendars = [calendar("stable-a", "Team", "#112233")]
        await self.cache.refresh(WINDOW)
        self.provider.fail_next = ProviderTimeoutError()
        failed = await self.cache.refresh(WINDOW)
        self.assertEqual(failed.status, "stale")
        self.assertEqual(failed.error_code, "provider_timeout")
        failed_metadata = self.cache.source_metadata()[1]
        self.assertEqual(failed_metadata["status"], "stale")
        self.assertEqual(failed_metadata["calendars"][0]["calendarId"], "stable-a")
        self.assertEqual(failed_metadata["calendars"][0]["displayName"], "Team")

        await asyncio.gather(self.cache.refresh(WINDOW), self.cache.refresh(WINDOW))
        self.assertEqual(self.provider.max_active_discoveries, 1)

    async def test_fixed_latest_failure_code_is_safe_and_success_clears_it(self) -> None:
        self.provider.calendars = [calendar("stable-a", "Team", "#112233")]
        await self.cache.refresh(WINDOW)

        self.provider.fail_next = ProviderFetchError(ProviderFailureCode.DNS_FAILED)
        failed = await self.cache.refresh(WINDOW)
        self.assertEqual(failed.status, "stale")  # Existing cache/freshness behavior is unchanged.
        self.assertEqual(failed.error_code, ProviderFailureCode.DNS_FAILED.value)
        failed_source = self.cache.source_metadata()[1]
        self.assertEqual(failed_source["errorCode"], ProviderFailureCode.DNS_FAILED.value)
        self.assertEqual(failed_source["calendars"][0]["errorCode"], ProviderFailureCode.DNS_FAILED.value)
        self.assertEqual(self.cache.health_snapshot()["providerErrorCode"], ProviderFailureCode.DNS_FAILED.value)

        recovered = await self.cache.refresh(WINDOW)
        self.assertEqual(recovered.status, "current")
        self.assertIsNone(recovered.error_code)
        recovered_source = self.cache.source_metadata()[1]
        self.assertIsNone(recovered_source["errorCode"])
        self.assertIsNone(recovered_source["calendars"][0]["errorCode"])
        self.assertIsNone(self.cache.health_snapshot()["providerErrorCode"])

    async def test_status_serialization_never_uses_private_exception_detail_as_a_category(self) -> None:
        self.provider.calendars = [calendar("stable-a", "Team", "#112233")]
        await self.cache.refresh(WINDOW)
        private_values = (
            "owner@example.invalid",
            "FAKE_SECRET_ICLOUD_159A",
            "https://calendar.example.invalid/private/path",
            "PRIVATE_EVENT_TITLE_159A",
            "RAW_SOCKET_DETAIL_159A",
        )
        self.provider.fail_next = ProviderFetchError(" ".join(private_values))
        failed = await self.cache.refresh(WINDOW)
        self.assertEqual(failed.error_code, ProviderFailureCode.FETCH_FAILED.value)
        serialized = json.dumps(
            {
                "result": failed.error_code,
                "source": self.cache.source_metadata(),
                "health": self.cache.health_snapshot(),
            },
            sort_keys=True,
        )
        for private_value in private_values:
            self.assertNotIn(private_value, serialized)

    async def test_authenticated_route_runs_real_discovery_returns_safe_result_and_rejects_ha(self) -> None:
        self.provider.calendars = [calendar("stable-a", "Team", "#112233")]
        app = web.Application()
        app["settings"] = SimpleNamespace(
            internal_webhook_secret=INTERNAL_SECRET,
            planning_ha_secret=HA_SECRET,
            planning_panel_agent_secret=PANEL_SECRET,
            planning_operator_secret="synthetic-operator-secret",
            planning_api_rate_limit_per_minute=120,
            planning_api_stale_after_seconds=300,
            planning_default_timezone="Europe/Moscow",
        )
        app["planning_database"] = self.database
        app["planning_icloud_cache"] = self.cache
        app["planning_icloud_refresh_loop"] = ICloudCalendarRefreshLoop(self.cache, interval_seconds=60)
        setup_planning_routes(app)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            response = await client.post(
                "/internal/planning/v1/calendar-sources/refresh",
                json={},
                headers={
                    "X-Internal-Secret": INTERNAL_SECRET,
                    "X-Planning-Audience": "panel-agent",
                    "X-Planning-Secret": PANEL_SECRET,
                },
            )
            self.assertEqual(response.status, 200)
            payload = await response.json()
            self.assertEqual(payload["result"], "success")
            self.assertEqual(payload["calendarsSeen"], 1)
            self.assertEqual(set(payload), {
                "schemaVersion", "kind", "result", "status", "observedAt",
                "lastSuccessfulSyncAt", "calendarsSeen", "eventsSeen", "errorCode",
                "correlation_id",
            })
            serialized = str(payload)
            self.assertNotIn("opaque-account", serialized)
            self.assertNotIn("server-only-ref-stable-a", serialized)
            self.assertEqual(self.provider.discover_calls, 1)

            ha_response = await client.post(
                "/internal/planning/v1/calendar-sources/refresh",
                json={},
                headers={
                    "X-Internal-Secret": INTERNAL_SECRET,
                    "X-Planning-Audience": "ha",
                    "X-Planning-Secret": HA_SECRET,
                },
            )
            self.assertEqual(ha_response.status, 403)
            self.assertEqual(self.provider.discover_calls, 1)
        finally:
            await client.close()


if __name__ == "__main__":
    unittest.main()
