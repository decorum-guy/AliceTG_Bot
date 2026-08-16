from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from app.planning.api.service import PlanningApiService
from app.planning.db import PlanningDatabase
from app.planning.errors import PlanningEventNotLocalOnlyError
from app.planning.events import EventService, is_native_local_only_event
from app.planning.models import MutationContext
from app.planning.repositories import PlanningRepository
from app.planning.providers.cache import ProviderCalendarCache
from app.planning.providers.contracts import CalendarWindow, ProviderTimeoutError
from app.planning.providers.icloud import ICloudCalDavProvider


NOW = "2026-08-16T12:00:00Z"
WINDOW = CalendarWindow(
    datetime(2026, 8, 16, tzinfo=timezone.utc),
    datetime(2026, 8, 23, tzinfo=timezone.utc),
)
CONTEXT = MutationContext(
    audience="operator",
    actor_id="icloud-fixture",
    actor_type="operator",
    surface="operator",
)


def _ical(
    calendar_number: int,
    *,
    include_second_event: bool = True,
    shift_second_event: bool = False,
) -> str:
    second = f"""
BEGIN:VEVENT
UID:fixture-timed-extra-{calendar_number}
DTSTART;TZID=Europe/Moscow:{'20260820T110000' if shift_second_event else '20260820T100000'}
DTEND;TZID=Europe/Moscow:{'20260820T120000' if shift_second_event else '20260820T110000'}
SUMMARY:{'changed <b>literal</b>' if shift_second_event else '<b>literal</b>'}
DESCRIPTION:private notes fixture
LOCATION:private room fixture
END:VEVENT
""" if include_second_event else ""
    return f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Alice fixture//EN
X-WR-TIMEZONE:Europe/Moscow
BEGIN:VEVENT
UID:fixture-timed-{calendar_number}
DTSTART;TZID=Europe/Moscow:20260817T100000
DTEND;TZID=Europe/Moscow:20260817T110000
SUMMARY:Timed fixture {calendar_number}
DESCRIPTION:owner private notes {calendar_number}
LOCATION:owner private location {calendar_number}
END:VEVENT
BEGIN:VEVENT
UID:fixture-day-{calendar_number}
DTSTART;VALUE=DATE:20260818
DTEND;VALUE=DATE:20260819
SUMMARY:One day
END:VEVENT
BEGIN:VEVENT
UID:fixture-multi-{calendar_number}
DTSTART;VALUE=DATE:20260819
DTEND;VALUE=DATE:20260821
SUMMARY:Multi day
END:VEVENT
BEGIN:VEVENT
UID:fixture-recurring-{calendar_number}
DTSTART;TZID=Europe/Moscow:20260817T120000
DTEND;TZID=Europe/Moscow:20260817T130000
RRULE:FREQ=DAILY;COUNT=3
SUMMARY:Recurring base
END:VEVENT
BEGIN:VEVENT
UID:fixture-recurring-{calendar_number}
RECURRENCE-ID;TZID=Europe/Moscow:20260818T120000
DTSTART;TZID=Europe/Moscow:20260818T140000
DTEND;TZID=Europe/Moscow:20260818T150000
SUMMARY:Recurring exception
END:VEVENT
{second}
END:VCALENDAR
"""


def _multistatus(*, principal: str | None = None, home: str | None = None, calendars: bool = False, icals: list[str] | None = None) -> bytes:
    if principal:
        return f'<multistatus xmlns="DAV:"><response><propstat><prop><current-user-principal><href>{principal}</href></current-user-principal></prop></propstat></response></multistatus>'.encode()
    if home:
        return f'<multistatus xmlns="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><response><propstat><prop><c:calendar-home-set><href>{home}</href></c:calendar-home-set></prop></propstat></response></multistatus>'.encode()
    if calendars:
        return b"""<multistatus xmlns="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav" xmlns:i="http://apple.com/ns/ical/">
<response><href>/home/one/</href><propstat><prop><resourcetype><c:calendar/></resourcetype><displayname>Same name</displayname><i:calendar-color>#ff0000</i:calendar-color></prop></propstat></response>
<response><href>/home/two/</href><propstat><prop><resourcetype><c:calendar/></resourcetype><displayname>Same name</displayname></prop></propstat></response>
</multistatus>"""
    data = "".join(
        f"<response><href>/event-{index}.ics</href><propstat><prop><calendar-data><![CDATA[{value}]]></calendar-data></prop></propstat></response>"
        for index, value in enumerate(icals or [], start=1)
    )
    return f'<multistatus xmlns="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">{data}</multistatus>'.encode()


class FixtureCalDavTransport:
    bootstrap_url = "https://fixture.invalid/.well-known/caldav"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.fail_next_report = False
        self.invalid_next_report = False
        self.auth_next_report = False
        self.include_second_event = True
        self.shift_second_event = False

    async def propfind(self, url: str, *, body: bytes, depth: str) -> bytes:
        self.calls.append(("PROPFIND", url, depth))
        body_text = body.decode()
        if "current-user-principal" in body_text:
            return _multistatus(principal="/principal/")
        if "calendar-home-set" in body_text:
            return _multistatus(home="/home/")
        return _multistatus(calendars=True)

    async def report(self, url: str, *, body: bytes, depth: str) -> bytes:
        self.calls.append(("REPORT", url, depth))
        if self.fail_next_report:
            self.fail_next_report = False
            raise ProviderTimeoutError("fixture timeout")
        if self.invalid_next_report:
            self.invalid_next_report = False
            return b"not xml"
        if self.auth_next_report:
            self.auth_next_report = False
            from app.planning.providers.contracts import ProviderAuthError

            raise ProviderAuthError("fixture auth failure")
        number = 1 if url.endswith("one/") else 2
        return _multistatus(
            icals=[
                _ical(
                    number,
                    include_second_event=self.include_second_event,
                    shift_second_event=self.shift_second_event,
                )
            ]
        )


class ICloudProviderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = PlanningDatabase(Path(self.temp.name) / "planning.sqlite3")
        self.transport = FixtureCalDavTransport()
        self.provider = ICloudCalDavProvider(
            transport=self.transport,
            account_name="owner@example.invalid",
            default_timezone="Europe/Moscow",
        )
        self.cache = ProviderCalendarCache(
            self.database,
            provider=self.provider,
            provider_name="icloud",
            account_id=self.provider.account_id_for("owner@example.invalid"),
            display_label="iCloud",
            enabled=True,
            configured=True,
            now_fn=lambda: NOW,
        )

    async def asyncTearDown(self) -> None:
        await self.provider.close()
        self.database.close()
        self.temp.cleanup()

    async def test_discovery_normalization_recurrence_and_transport_boundary(self) -> None:
        account = await self.provider.discover_account()
        calendars = await self.provider.list_calendars()
        events = await self.provider.fetch_events(calendars[0], WINDOW)

        self.assertEqual(account.display_label, "iCloud")
        self.assertNotIn("owner@example.invalid", account.account_id)
        self.assertEqual([calendar.display_name for calendar in calendars], ["Same name", "Same name"])
        self.assertNotEqual(calendars[0].provider_calendar_id, calendars[1].provider_calendar_id)
        self.assertEqual(len(events), 7)
        self.assertTrue(any(event.all_day and event.end_date_exclusive == "2026-08-19" for event in events))
        self.assertTrue(any(event.all_day and event.end_date_exclusive == "2026-08-21" for event in events))
        exception = next(event for event in events if event.title == "Recurring exception")
        self.assertEqual(exception.start_at_utc, "2026-08-18T11:00:00Z")
        hostile = next(event for event in events if "literal" in event.title)
        self.assertEqual(hostile.title, "<b>literal</b>")
        self.assertTrue(all(method in {"PROPFIND", "REPORT"} for method, _, _ in self.transport.calls))

    async def test_cache_combines_local_and_provider_events_and_stabilizes_identity(self) -> None:
        local = PlanningRepository(self.database).create_calendar_event(
            title="Native local",
            all_day=False,
            timezone="Europe/Moscow",
            start_at_utc="2026-08-17T08:00:00Z",
            end_at_utc="2026-08-17T09:00:00Z",
            context=CONTEXT,
        )
        first = await self.cache.refresh(WINDOW)
        self.assertEqual(first.status, "current")
        events = self.database.connection.execute(
            "SELECT id, title, provider_id, provider_calendar_id, sync_state FROM calendar_events ORDER BY id"
        ).fetchall()
        self.assertEqual(len(events), 15)
        self.assertEqual(sum(row["source"] == "calendar-provider" for row in self.database.connection.execute("SELECT source FROM calendar_events")), 14)
        imported = self.database.connection.execute(
            "SELECT * FROM calendar_events WHERE source = 'calendar-provider' ORDER BY id LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(imported)
        self.assertNotEqual(imported["sync_state"], "local_only")
        self.assertFalse(is_native_local_only_event(EventService(self.database).get(imported["id"])))
        self.assertEqual(EventService(self.database).get(local.id).title, "Native local")

        api = PlanningApiService(
            self.database,
            default_timezone="Europe/Moscow",
            provider_cache=self.cache,
        )
        listed = api.list_events(
            from_utc="2026-08-16T00:00:00Z",
            to_utc="2026-08-23T00:00:00Z",
            limit=100,
            offset=0,
            correlation_id="correlation",
        )
        self.assertEqual(len(listed["items"]), 15)
        self.assertEqual(len(listed["sources"]), 2)

        first_ids = {
            row["provider_id"]: row["id"]
            for row in self.database.connection.execute("SELECT provider_id, id FROM calendar_events WHERE source = 'calendar-provider'")
        }
        changed_before = self.database.connection.execute(
            "SELECT id, provider_id FROM calendar_events WHERE title = '<b>literal</b>' LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(changed_before)
        self.transport.shift_second_event = True
        await self.cache.refresh(WINDOW)
        changed_after = self.database.connection.execute(
            "SELECT id, title, start_at_utc FROM calendar_events WHERE provider_id = ?",
            (changed_before["provider_id"],),
        ).fetchone()
        self.assertEqual(changed_after["id"], changed_before["id"])
        self.assertEqual(changed_after["title"], "changed <b>literal</b>")
        self.assertEqual(changed_after["start_at_utc"], "2026-08-20T08:00:00Z")

        self.transport.include_second_event = False
        await self.cache.refresh(WINDOW)
        second_ids = {
            row["provider_id"]: row["id"]
            for row in self.database.connection.execute("SELECT provider_id, id FROM calendar_events WHERE source = 'calendar-provider'")
        }
        self.assertEqual(first_ids, second_ids)

        imported_id = next(iter(first_ids.values()))
        with self.assertRaises(PlanningEventNotLocalOnlyError):
            EventService(self.database).update(imported_id, expected_version=1, title="blocked", context=CONTEXT)
        with self.assertRaises(PlanningEventNotLocalOnlyError):
            EventService(self.database).delete(imported_id, expected_version=1, context=CONTEXT)

    async def test_malformed_and_auth_failures_are_sanitized_and_preserve_cache(self) -> None:
        await self.cache.refresh(WINDOW)
        self.transport.invalid_next_report = True
        malformed = await self.cache.refresh(WINDOW)
        self.assertEqual(malformed.status, "stale")
        self.assertEqual(malformed.error_code, "provider_payload_invalid")
        self.assertEqual(self.cache.health_snapshot()["providerErrorCode"], "provider_payload_invalid")

        self.transport.auth_next_report = True
        auth = await self.cache.refresh(WINDOW)
        self.assertEqual(auth.status, "stale")
        self.assertEqual(auth.error_code, "provider_authentication_failed")
        self.assertEqual(
            self.database.connection.execute(
                "SELECT COUNT(*) FROM calendar_events WHERE deleted_at IS NULL"
            ).fetchone()[0],
            14,
        )

    async def test_failure_is_stale_cache_and_successful_disappearance_tombstones_only_provider_rows(self) -> None:
        await self.cache.refresh(WINDOW)
        before_native = PlanningRepository(self.database).create_calendar_event(
            title="Native remains",
            all_day=True,
            timezone="Europe/Moscow",
            start_date="2026-08-18",
            end_date_exclusive="2026-08-19",
            context=CONTEXT,
        )
        self.transport.fail_next_report = True
        failed = await self.cache.refresh(WINDOW)
        self.assertEqual(failed.status, "stale")
        self.assertEqual(failed.error_code, "provider_timeout")
        self.assertEqual(self.database.connection.execute("SELECT COUNT(*) FROM calendar_events WHERE deleted_at IS NOT NULL").fetchone()[0], 0)
        self.assertEqual(EventService(self.database).get(before_native.id).deleted_at, None)
        self.assertEqual(self.cache.source_metadata()[1]["status"], "stale")

        self.transport.include_second_event = False
        recovered = await self.cache.refresh(WINDOW)
        self.assertEqual(recovered.status, "current")
        self.assertEqual(self.cache.source_metadata()[1]["status"], "current")
        self.assertEqual(self.database.connection.execute("SELECT COUNT(*) FROM calendar_events WHERE deleted_at IS NOT NULL").fetchone()[0], 2)
        self.assertEqual(EventService(self.database).get(before_native.id).deleted_at, None)

    async def test_read_by_id_and_redacted_source_metadata(self) -> None:
        await self.cache.refresh(WINDOW)
        api = PlanningApiService(self.database, default_timezone="Europe/Moscow", provider_cache=self.cache)
        imported = self.database.connection.execute(
            "SELECT id FROM calendar_events WHERE source = 'calendar-provider' ORDER BY id LIMIT 1"
        ).fetchone()[0]
        payload = api.get_event(event_id=imported, correlation_id="correlation")
        self.assertEqual(payload["object"]["id"], imported)
        self.assertIn("sources", payload)
        serialized = str(payload)
        self.assertNotIn("owner@example.invalid", serialized)
        self.assertNotIn("fixture private notes", serialized)
        self.assertNotIn("/home/", serialized)
        self.assertEqual(payload["sources"][1]["provider"], "icloud")
        self.assertEqual(payload["sources"][1]["status"], "current")

    async def test_disabled_and_not_configured_states_do_not_call_provider(self) -> None:
        disabled = ProviderCalendarCache(
            self.database,
            provider=self.provider,
            provider_name="icloud",
            account_id=None,
            display_label="iCloud",
            enabled=False,
            configured=False,
            now_fn=lambda: NOW,
        )
        disabled_result = await disabled.refresh(WINDOW)
        self.assertEqual(disabled_result.status, "disabled")
        not_configured = ProviderCalendarCache(
            self.database,
            provider=None,
            provider_name="icloud",
            account_id=None,
            display_label="iCloud",
            enabled=True,
            configured=False,
            now_fn=lambda: NOW,
        )
        not_configured_result = await not_configured.refresh(WINDOW)
        self.assertEqual(not_configured_result.status, "not_configured")


if __name__ == "__main__":
    unittest.main()
