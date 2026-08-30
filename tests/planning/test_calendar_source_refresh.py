from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app.planning import PlanningDatabase
from app.planning.api.routes import setup_planning_routes
from app.planning.providers.cache import (
    MAX_BACKWARDS_CLOCK_SKEW_SECONDS,
    ProviderCalendarCache,
)
from app.planning.providers.contracts import (
    CalendarWindow,
    ExternalCalendar,
    ExternalCalendarEvent,
    ExternalProviderAccount,
    ProviderFailureCode,
    ProviderFetchError,
    ProviderTimeoutError,
)
from app.planning.providers.sync import ICloudCalendarRefreshLoop, provider_stale_after_seconds


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
        self.events: list[ExternalCalendarEvent] = []
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
        return list(self.events)


def calendar(calendar_id: str, name: str, color: str) -> ExternalCalendar:
    return ExternalCalendar(
        provider_calendar_id=calendar_id,
        display_name=name,
        color=color,
        enabled=True,
        fetch_ref=f"server-only-ref-{calendar_id}",
    )


def provider_event(calendar_id: str) -> ExternalCalendarEvent:
    return ExternalCalendarEvent(
        provider_calendar_id=calendar_id,
        provider_event_id="event-1",
        recurrence_instance_key="2026-08-17",
        title="Provider event",
        notes=None,
        location=None,
        all_day=True,
        timezone="Europe/Moscow",
        start_at_utc=None,
        end_at_utc=None,
        start_date="2026-08-17",
        end_date_exclusive="2026-08-18",
    )


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> str:
        return self.value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


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
        self.assertEqual(failed.status, "current")
        self.assertEqual(failed.error_code, "provider_timeout")
        failed_metadata = self.cache.source_metadata()[1]
        self.assertEqual(failed_metadata["status"], "current")
        self.assertEqual(failed_metadata["calendars"][0]["calendarId"], "stable-a")
        self.assertEqual(failed_metadata["calendars"][0]["displayName"], "Team")

        await asyncio.gather(self.cache.refresh(WINDOW), self.cache.refresh(WINDOW))
        self.assertEqual(self.provider.max_active_discoveries, 1)

    async def test_fixed_latest_failure_code_is_safe_and_success_clears_it(self) -> None:
        self.provider.calendars = [calendar("stable-a", "Team", "#112233")]
        await self.cache.refresh(WINDOW)

        self.provider.fail_next = ProviderFetchError(ProviderFailureCode.DNS_FAILED)
        failed = await self.cache.refresh(WINDOW)
        self.assertEqual(failed.status, "current")
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

    async def test_age_based_failure_state_preserves_fresh_data_then_stales_and_recovers(self) -> None:
        clock = MutableClock(datetime(2026, 8, 27, 9, 0, tzinfo=timezone.utc))
        cache = ProviderCalendarCache(
            self.database,
            provider=self.provider,
            provider_name="icloud",
            account_id="opaque-account",
            display_label="iCloud",
            enabled=True,
            configured=True,
            now_fn=clock.now,
            stale_after_seconds=600,
        )
        self.provider.calendars = [calendar("stable-a", "Team", "#112233")]
        self.provider.events = [provider_event("stable-a")]
        first = await cache.refresh(WINDOW)
        self.assertEqual(first.status, "current")
        first_success = first.last_successful_sync_at

        clock.advance(300)
        self.provider.fail_next = ProviderFetchError(ProviderFailureCode.DNS_FAILED)
        transient = await cache.refresh(WINDOW)
        self.assertEqual(transient.status, "current")
        self.assertEqual(transient.error_code, ProviderFailureCode.DNS_FAILED.value)
        self.assertEqual(transient.last_successful_sync_at, first_success)
        source = cache.source_metadata()[1]
        self.assertEqual(source["status"], "current")
        self.assertEqual(source["observedAt"], clock.now())
        self.assertEqual(source["errorCode"], ProviderFailureCode.DNS_FAILED.value)
        self.assertEqual(source["calendars"][0]["status"], "current")
        self.assertEqual(source["calendars"][0]["errorCode"], ProviderFailureCode.DNS_FAILED.value)
        self.assertEqual(
            self.database.connection.execute("SELECT sync_state FROM calendar_events").fetchone()[0],
            "synced",
        )

        for code in (ProviderFailureCode.AUTHENTICATION_FAILED, ProviderFailureCode.RATE_LIMITED):
            self.provider.fail_next = ProviderFetchError(code)
            repeated = await cache.refresh(WINDOW)
            self.assertEqual(repeated.status, "current")
            self.assertEqual(repeated.error_code, code.value)

        clock.advance(300)
        self.provider.fail_next = ProviderFetchError(ProviderFailureCode.RATE_LIMITED)
        stale = await cache.refresh(WINDOW)
        self.assertEqual(stale.status, "stale")
        self.assertEqual(stale.error_code, ProviderFailureCode.RATE_LIMITED.value)
        self.assertEqual(stale.last_successful_sync_at, first_success)
        stale_source = cache.source_metadata()[1]
        self.assertEqual(stale_source["status"], "stale")
        self.assertEqual(stale_source["calendars"][0]["status"], "stale")
        self.assertEqual(
            self.database.connection.execute("SELECT sync_state FROM calendar_events").fetchone()[0],
            "stale",
        )

        clock.advance(1)
        recovered = await cache.refresh(WINDOW)
        self.assertEqual(recovered.status, "current")
        self.assertIsNone(recovered.error_code)
        self.assertEqual(cache.source_metadata()[1]["calendars"][0]["status"], "current")
        self.assertIsNone(cache.source_metadata()[1]["errorCode"])
        self.assertEqual(
            self.database.connection.execute("SELECT sync_state FROM calendar_events").fetchone()[0],
            "synced",
        )

    async def test_no_cache_and_malformed_last_success_fail_safely(self) -> None:
        clock = MutableClock(datetime(2026, 8, 27, 9, 0, tzinfo=timezone.utc))
        cache = ProviderCalendarCache(
            self.database,
            provider=self.provider,
            provider_name="icloud",
            account_id="opaque-account",
            display_label="iCloud",
            enabled=True,
            configured=True,
            now_fn=clock.now,
            stale_after_seconds=600,
        )
        self.provider.fail_next = ProviderFetchError(ProviderFailureCode.DNS_FAILED)
        self.assertEqual((await cache.refresh(WINDOW)).status, "error")

        self.provider.calendars = [calendar("stable-a", "Team", "#112233")]
        await cache.refresh(WINDOW)
        self.database.connection.execute(
            "UPDATE provider_sources SET last_successful_sync_at = 'not-a-timestamp' WHERE source_id = ?",
            (cache.source_id,),
        )
        self.provider.fail_next = ProviderFetchError(ProviderFailureCode.DNS_FAILED)
        self.assertEqual((await cache.refresh(WINDOW)).status, "stale")

        await cache.refresh(WINDOW)
        self.database.connection.execute(
            "UPDATE provider_sources SET last_successful_sync_at = NULL WHERE source_id = ?",
            (cache.source_id,),
        )
        self.provider.fail_next = ProviderFetchError(ProviderFailureCode.DNS_FAILED)
        self.assertEqual((await cache.refresh(WINDOW)).status, "stale")

    async def test_backwards_clock_skew_is_bounded_and_malformed_observed_fails_closed(self) -> None:
        clock = MutableClock(datetime(2026, 8, 27, 9, 0, tzinfo=timezone.utc))
        cache = ProviderCalendarCache(
            self.database,
            provider=self.provider,
            provider_name="icloud",
            account_id="opaque-account",
            display_label="iCloud",
            enabled=True,
            configured=True,
            now_fn=clock.now,
            stale_after_seconds=600,
        )
        self.provider.calendars = [calendar("stable-a", "Team", "#112233")]
        await cache.refresh(WINDOW)

        def set_last_success(seconds_ahead: int) -> None:
            value = (clock.value + timedelta(seconds=seconds_ahead)).isoformat(timespec="seconds").replace(
                "+00:00", "Z"
            )
            self.database.connection.execute(
                "UPDATE provider_sources SET last_successful_sync_at = ? WHERE source_id = ?",
                (value, cache.source_id),
            )

        self.assertEqual(MAX_BACKWARDS_CLOCK_SKEW_SECONDS, 60)
        for seconds_ahead, expected in ((1, "current"), (60, "current"), (61, "stale"), (3600, "stale")):
            with self.subTest(seconds_ahead=seconds_ahead):
                set_last_success(seconds_ahead)
                self.provider.fail_next = ProviderFetchError(ProviderFailureCode.DNS_FAILED)
                self.assertEqual((await cache.refresh(WINDOW)).status, expected)

        malformed_observed_cache = ProviderCalendarCache(
            self.database,
            provider=self.provider,
            provider_name="icloud",
            account_id="opaque-account",
            display_label="iCloud",
            enabled=True,
            configured=True,
            now_fn=lambda: "not-a-timestamp",
            stale_after_seconds=600,
        )
        self.provider.fail_next = ProviderFetchError(ProviderFailureCode.DNS_FAILED)
        self.assertEqual((await malformed_observed_cache.refresh(WINDOW)).status, "stale")

    async def test_disappeared_calendar_is_not_resurrected_by_failure_propagation(self) -> None:
        clock = MutableClock(datetime(2026, 8, 27, 9, 0, tzinfo=timezone.utc))
        cache = ProviderCalendarCache(
            self.database,
            provider=self.provider,
            provider_name="icloud",
            account_id="opaque-account",
            display_label="iCloud",
            enabled=True,
            configured=True,
            now_fn=clock.now,
            stale_after_seconds=600,
        )
        calendar_a = calendar("stable-a", "A", "#112233")
        calendar_b = calendar("stable-b", "B", "#445566")
        self.provider.calendars = [calendar_a, calendar_b]
        await cache.refresh(WINDOW)
        clock.advance(1)
        self.provider.calendars = [calendar_a]
        await cache.refresh(WINDOW)

        def rows() -> dict[str, object]:
            return {
                str(row["provider_calendar_id"]): row
                for row in self.database.connection.execute(
                    "SELECT * FROM provider_calendars WHERE source_id = ?",
                    (cache.source_id,),
                ).fetchall()
            }

        disappeared = rows()["stable-b"]
        self.assertEqual(disappeared["status"], "disabled")
        self.assertEqual(disappeared["enabled"], 0)
        self.assertEqual(disappeared["last_error_code"], "provider_calendar_disappeared")
        disappeared_observed_at = disappeared["observed_at"]
        disappeared_last_successful_sync_at = disappeared["last_successful_sync_at"]

        clock.advance(300)
        self.provider.fail_next = ProviderFetchError(ProviderFailureCode.DNS_FAILED)
        transient = await cache.refresh(WINDOW)
        self.assertEqual(transient.status, "current")
        fresh_rows = rows()
        self.assertEqual(fresh_rows["stable-a"]["status"], "current")
        self.assertEqual(fresh_rows["stable-a"]["last_error_code"], ProviderFailureCode.DNS_FAILED.value)
        self.assertEqual(fresh_rows["stable-b"]["status"], "disabled")
        self.assertEqual(fresh_rows["stable-b"]["last_error_code"], "provider_calendar_disappeared")
        self.assertEqual(fresh_rows["stable-b"]["observed_at"], disappeared_observed_at)
        self.assertEqual(
            fresh_rows["stable-b"]["last_successful_sync_at"], disappeared_last_successful_sync_at
        )

        clock.advance(300)
        self.provider.fail_next = ProviderFetchError(ProviderFailureCode.RATE_LIMITED)
        stale = await cache.refresh(WINDOW)
        self.assertEqual(stale.status, "stale")
        stale_rows = rows()
        self.assertEqual(stale_rows["stable-a"]["status"], "stale")
        self.assertEqual(stale_rows["stable-a"]["last_error_code"], ProviderFailureCode.RATE_LIMITED.value)
        self.assertEqual(stale_rows["stable-b"]["status"], "disabled")
        self.assertEqual(stale_rows["stable-b"]["last_error_code"], "provider_calendar_disappeared")
        self.assertEqual(
            stale_rows["stable-b"]["last_successful_sync_at"], disappeared_last_successful_sync_at
        )

        clock.advance(1)
        self.provider.calendars = [calendar_a, calendar_b]
        await cache.refresh(WINDOW)
        restored = rows()["stable-b"]
        self.assertEqual(restored["enabled"], 1)
        self.assertEqual(restored["status"], "current")
        self.assertIsNone(restored["last_error_code"])

    async def test_restart_uses_persisted_age_not_an_in_memory_failure_counter(self) -> None:
        clock = MutableClock(datetime(2026, 8, 27, 9, 0, tzinfo=timezone.utc))
        self.provider.calendars = [calendar("stable-a", "Team", "#112233")]
        first_cache = ProviderCalendarCache(
            self.database,
            provider=self.provider,
            provider_name="icloud",
            account_id="opaque-account",
            display_label="iCloud",
            enabled=True,
            configured=True,
            now_fn=clock.now,
            stale_after_seconds=600,
        )
        await first_cache.refresh(WINDOW)
        clock.advance(300)
        self.provider.fail_next = ProviderFetchError(ProviderFailureCode.DNS_FAILED)
        self.assertEqual((await first_cache.refresh(WINDOW)).status, "current")

        restarted_cache = ProviderCalendarCache(
            self.database,
            provider=self.provider,
            provider_name="icloud",
            account_id="opaque-account",
            display_label="iCloud",
            enabled=True,
            configured=True,
            now_fn=clock.now,
            stale_after_seconds=600,
        )
        self.assertEqual(restarted_cache.source_metadata()[1]["status"], "current")
        self.assertEqual(
            restarted_cache.source_metadata()[1]["errorCode"],
            ProviderFailureCode.DNS_FAILED.value,
        )
        self.provider.fail_next = ProviderFetchError(ProviderFailureCode.AUTHENTICATION_FAILED)
        self.assertEqual((await restarted_cache.refresh(WINDOW)).status, "current")
        clock.advance(300)
        self.provider.fail_next = ProviderFetchError(ProviderFailureCode.RATE_LIMITED)
        self.assertEqual((await restarted_cache.refresh(WINDOW)).status, "stale")

    def test_stale_threshold_policy(self) -> None:
        self.assertEqual(provider_stale_after_seconds(60), 600)
        self.assertEqual(provider_stale_after_seconds(300), 600)
        self.assertEqual(provider_stale_after_seconds(600), 1200)
        self.assertEqual(provider_stale_after_seconds(3600), 7200)

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
