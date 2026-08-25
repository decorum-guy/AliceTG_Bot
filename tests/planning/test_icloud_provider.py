from __future__ import annotations

import tempfile
import re
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from app.planning.api.service import PlanningApiService
from app.planning.db import PlanningDatabase
from app.planning.errors import PlanningEventNotLocalOnlyError
from app.planning.events import EventService, is_native_local_only_event
from app.planning.models import MutationContext
from app.planning.repositories import PlanningRepository
from app.planning.providers.cache import ProviderCalendarCache
from app.planning.providers.contracts import (
    CalendarWindow,
    ProviderAuthError,
    ProviderFetchError,
    ProviderPayloadError,
    ProviderTimeoutError,
)
from app.planning.providers.icloud import (
    ICloudCalDavProvider,
    _CALDAV,
    _DAV,
    _calendar_multiget_body,
    _calendar_query_body,
    _propfind_body,
)


NOW = "2026-08-16T12:00:00Z"
W1 = CalendarWindow(
    datetime(2026, 8, 16, tzinfo=timezone.utc),
    datetime(2026, 8, 23, tzinfo=timezone.utc),
)
W2 = CalendarWindow(
    datetime(2026, 8, 17, tzinfo=timezone.utc),
    datetime(2026, 8, 24, tzinfo=timezone.utc),
)
WINDOW = W1
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


def _resource_icals(
    calendar_number: int,
    *,
    include_second_event: bool = True,
    shift_second_event: bool = False,
    move_second_event_outside: bool = False,
    dense_only: bool = False,
) -> list[str]:
    def wrap(body: str) -> str:
        return f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Alice fixture//EN
X-WR-TIMEZONE:Europe/Moscow
{body}
END:VCALENDAR
"""

    if dense_only:
        return [
            wrap(
                f"""BEGIN:VEVENT
UID:fixture-dense-{calendar_number}
DTSTART;TZID=Europe/Moscow:20260817T100000
DTEND;TZID=Europe/Moscow:20260817T100001
RRULE:FREQ=SECONDLY;COUNT=100000000
SUMMARY:Dense fixture
END:VEVENT"""
            )
        ]
    resources = [
        wrap(
            f"""BEGIN:VEVENT
UID:fixture-timed-{calendar_number}
DTSTART;TZID=Europe/Moscow:20260817T100000
DTEND;TZID=Europe/Moscow:20260817T110000
SUMMARY:Timed fixture {calendar_number}
DESCRIPTION:owner private notes {calendar_number}
LOCATION:owner private location {calendar_number}
END:VEVENT"""
        ),
        wrap(
            """BEGIN:VEVENT
UID:fixture-day-{calendar_number}
DTSTART;VALUE=DATE:20260818
DTEND;VALUE=DATE:20260819
SUMMARY:One day
END:VEVENT""".replace("{calendar_number}", str(calendar_number))
        ),
        wrap(
            """BEGIN:VEVENT
UID:fixture-multi-{calendar_number}
DTSTART;VALUE=DATE:20260819
DTEND;VALUE=DATE:20260821
SUMMARY:Multi day
END:VEVENT""".replace("{calendar_number}", str(calendar_number))
        ),
        wrap(
            f"""BEGIN:VEVENT
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
END:VEVENT"""
        ),
    ]
    if include_second_event:
        start = "20260920T100000" if move_second_event_outside else (
            "20260820T110000" if shift_second_event else "20260820T100000"
        )
        end = "20260920T110000" if move_second_event_outside else (
            "20260820T120000" if shift_second_event else "20260820T110000"
        )
        title = "changed <b>literal</b>" if shift_second_event else "<b>literal</b>"
        resources.append(
            wrap(
                f"""BEGIN:VEVENT
UID:fixture-timed-extra-{calendar_number}
DTSTART;TZID=Europe/Moscow:{start}
DTEND;TZID=Europe/Moscow:{end}
SUMMARY:{title}
DESCRIPTION:private notes fixture
LOCATION:private room fixture
END:VEVENT"""
            )
        )
    return resources


def _multistatus(
    *,
    principal: str | None = None,
    home: str | None = None,
    calendars: bool = False,
    include_second_calendar: bool = True,
    icals: list[str] | None = None,
    resource_hrefs: list[str] | None = None,
    missing_hrefs: set[str] | None = None,
    resource_payloads: dict[str, str] | None = None,
    direct_statuses: dict[str, int] | None = None,
    no_data_hrefs: set[str] | None = None,
    outer_href: str | None = None,
    discovery_property: str | None = None,
    discovery_status: str | None = None,
    unauthenticated: bool = False,
) -> bytes:
    if principal:
        property_value = (
            "<current-user-principal><unauthenticated/></current-user-principal>"
            if unauthenticated
            else f"<current-user-principal><href>{principal}</href></current-user-principal>"
        )
        return (
            f'<multistatus xmlns="DAV:"><response><href>{outer_href or "/wrong-resource/"}</href>'
            f"<propstat><prop>{property_value}</prop>"
            f"{f'<status>{discovery_status}</status>' if discovery_status else ''}"
            f"</propstat></response></multistatus>"
        ).encode()
    if home:
        property_value = f"<c:calendar-home-set><href>{home}</href></c:calendar-home-set>"
        return (
            f'<multistatus xmlns="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
            f'<response><href>{outer_href or "/wrong-principal-resource/"}</href>'
            f"<propstat><prop>{property_value}</prop>"
            f"{f'<status>{discovery_status}</status>' if discovery_status else ''}"
            f"</propstat></response></multistatus>"
        ).encode()
    if discovery_property:
        namespace = ' xmlns:c="urn:ietf:params:xml:ns:caldav"' if discovery_property == "home" else ""
        property_xml = (
            "<c:calendar-home-set/>" if discovery_property == "home" else "<current-user-principal/>"
        )
        return (
            f'<multistatus xmlns="DAV:"{namespace}><response><href>/outer-only/</href>'
            f"<propstat><prop>{property_xml}</prop>"
            f"{f'<status>{discovery_status}</status>' if discovery_status else ''}"
            f"</propstat></response></multistatus>"
        ).encode()
    if calendars:
        second = b"""<response><href>/home/two/</href><propstat><prop><resourcetype><c:calendar/></resourcetype><displayname>Same name</displayname></prop></propstat></response>""" if include_second_calendar else b""
        return b"""<multistatus xmlns="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav" xmlns:i="http://apple.com/ns/ical/">
<response><href>/home/one/</href><propstat><prop><resourcetype><c:calendar/></resourcetype><displayname>Same name</displayname><i:calendar-color>#ff0000</i:calendar-color></prop></propstat></response>
""" + second + b"</multistatus>"
    refs = resource_hrefs or [f"event-{index}.ics" for index, _ in enumerate(icals or [], start=1)]
    missing = missing_hrefs or set()
    payloads = resource_payloads or {}
    direct_statuses = direct_statuses or {}
    no_data = no_data_hrefs or set()
    reasons = {200: "OK", 403: "Forbidden", 404: "Not Found", 410: "Gone"}

    def status_line(status_code: int) -> str:
        return f"HTTP/1.1 {status_code} {reasons.get(status_code, 'Status')}"

    responses: list[str] = []
    for index, href in enumerate(refs, start=1):
        if href in missing:
            responses.append(
                f'<response><href>{href}</href><status>{status_line(direct_statuses.get(href, 404))}</status></response>'
            )
            continue
        if href in no_data:
            responses.append(
                f'<response><href>{href}</href><status>{status_line(direct_statuses.get(href, 200))}</status></response>'
            )
            continue
        payload = payloads.get(href)
        if payload is None:
            try:
                payload = (icals or [])[index - 1]
            except IndexError as exc:
                raise AssertionError(f"missing fixture payload for {href}") from exc
        direct_status = (
            f'<status>{status_line(direct_statuses[href])}</status>'
            if href in direct_statuses
            else ""
        )
        responses.append(
            f'<response><href>{href}</href>{direct_status}<propstat><prop><getetag>"etag-{index}"</getetag>'
            f'<c:calendar-data><![CDATA[{payload}]]></c:calendar-data></prop>'
            "<status>HTTP/1.1 200 OK</status></propstat></response>"
        )
    data = "".join(responses)
    return f'<multistatus xmlns="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">{data}</multistatus>'.encode()


class FixtureCalDavTransport:
    bootstrap_url = "https://fixture.invalid/.well-known/caldav"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.bodies: list[str] = []
        self.fail_next_report = False
        self.invalid_next_report = False
        self.auth_next_report = False
        self.include_second_event = True
        self.shift_second_event = False
        self.move_second_event_outside = False
        self.delete_second_event = False
        self.dense_only = False
        self.include_second_calendar = True
        self.discovery_variant = "normal"
        self.multiget_mode = "normal"
        self.empty_calendar_numbers: set[int] = set()
        self.report_payload: bytes | None = None

    async def propfind(self, url: str, *, body: bytes, depth: str) -> bytes:
        self.calls.append(("PROPFIND", url, depth))
        body_text = body.decode()
        self.bodies.append(body_text)
        if "current-user-principal" in body_text:
            if self.discovery_variant == "principal_missing":
                return _multistatus(discovery_property="principal")
            if self.discovery_variant == "principal_unauthenticated":
                return _multistatus(principal="/principal/", unauthenticated=True)
            if self.discovery_variant == "principal_non_2xx":
                return _multistatus(
                    principal="/principal/",
                    discovery_status="HTTP/1.1 404 Not Found",
                )
            return _multistatus(principal="/principal/", outer_href="/outer-principal-resource/")
        if "calendar-home-set" in body_text:
            if self.discovery_variant == "home_missing":
                return _multistatus(discovery_property="home")
            if self.discovery_variant == "home_non_2xx":
                return _multistatus(
                    home="/home/",
                    discovery_status="HTTP/1.1 403 Forbidden",
                )
            return _multistatus(home="/home/", outer_href="/outer-home-resource/")
        return _multistatus(calendars=True, include_second_calendar=self.include_second_calendar)

    async def report(self, url: str, *, body: bytes, depth: str) -> bytes:
        self.calls.append(("REPORT", url, depth))
        body_text = body.decode()
        self.bodies.append(body_text)
        if self.fail_next_report:
            self.fail_next_report = False
            raise ProviderTimeoutError("fixture timeout")
        if self.invalid_next_report:
            self.invalid_next_report = False
            return b"not xml"
        if self.auth_next_report:
            self.auth_next_report = False
            raise ProviderAuthError("fixture auth failure")
        number = 1 if url.endswith("one/") else 2
        if self.report_payload is not None and "calendar-multiget" not in body_text:
            return self.report_payload
        resource_icals = _resource_icals(
            number,
            include_second_event=self.include_second_event,
            shift_second_event=self.shift_second_event,
            move_second_event_outside=self.move_second_event_outside,
            dense_only=self.dense_only,
        )
        if "calendar-multiget" in body_text:
            if self.multiget_mode == "empty":
                return _multistatus(resource_hrefs=[])
            requested = re.findall(r"<d:href>(.*?)</d:href>", body_text)
            hrefs = [href for href in requested]
            if self.multiget_mode == "omitted":
                hrefs = hrefs[:-1]
            elif self.multiget_mode == "duplicate":
                hrefs = [hrefs[0], hrefs[0]]
            payloads: dict[str, str] = {}
            missing: set[str] = set()
            for href in hrefs:
                if (self.delete_second_event or not self.include_second_event) and href.endswith("event-5.ics"):
                    missing.add(href)
                    continue
                try:
                    index = int(href.rsplit("event-", 1)[1].split(".ics", 1)[0]) - 1
                    payloads[href] = resource_icals[index]
                except (IndexError, ValueError) as exc:
                    raise AssertionError(f"unexpected multiget href: {href}") from exc
            direct_statuses: dict[str, int] = {}
            no_data_hrefs: set[str] = set()
            if requested and self.multiget_mode in {
                "direct_404",
                "direct_410",
                "direct_403",
                "direct_200_no_data",
                "contradictory",
            }:
                target = requested[0]
                direct_statuses[target] = {
                    "direct_404": 404,
                    "direct_410": 410,
                    "direct_403": 403,
                    "direct_200_no_data": 200,
                    "contradictory": 404,
                }[self.multiget_mode]
                if self.multiget_mode in {"direct_404", "direct_410"}:
                    payloads.pop(target, None)
                    missing.add(target)
                elif self.multiget_mode in {"direct_403", "direct_200_no_data"}:
                    payloads.pop(target, None)
                    no_data_hrefs.add(target)
            return _multistatus(
                resource_hrefs=hrefs,
                resource_payloads=payloads,
                missing_hrefs=missing,
                direct_statuses=direct_statuses,
                no_data_hrefs=no_data_hrefs,
            )
        if number in self.empty_calendar_numbers:
            return _multistatus(resource_hrefs=[])
        resource_hrefs = [f"event-{index}.ics" for index in range(1, len(resource_icals) + 1)]
        if self.delete_second_event or self.move_second_event_outside:
            resource_hrefs = resource_hrefs[:-1]
            resource_icals = resource_icals[:-1]
        return _multistatus(
            icals=resource_icals,
            resource_hrefs=resource_hrefs,
        )


class ICloudXmlBuilderTests(unittest.TestCase):
    @staticmethod
    def _expanded(namespace: str, local_name: str) -> str:
        return f"{{{namespace}}}{local_name}"

    def _assert_propfind_root(self, body: bytes) -> ET.Element:
        root = ET.fromstring(body)
        self.assertEqual(root.tag, self._expanded(_DAV, "propfind"))
        prop = root.find(self._expanded(_DAV, "prop"))
        self.assertIsNotNone(prop)
        return prop

    def test_principal_propfind_body_is_dav_bound(self) -> None:
        prop = self._assert_propfind_root(
            _propfind_body("<d:current-user-principal/>", {"d": _DAV})
        )
        self.assertEqual(
            [child.tag for child in prop],
            [self._expanded(_DAV, "current-user-principal")],
        )

    def test_calendar_home_body_is_valid_without_explicit_d_namespace(self) -> None:
        prop = self._assert_propfind_root(
            _propfind_body("<x:calendar-home-set/>", {"x": _CALDAV})
        )
        self.assertEqual(
            [child.tag for child in prop],
            [self._expanded(_CALDAV, "calendar-home-set")],
        )

    def test_calendar_list_body_is_valid_and_binds_dav_and_apple_namespaces(self) -> None:
        prop = self._assert_propfind_root(
            _propfind_body(
                "<d:resourcetype/><d:displayname/><x:calendar-color/>",
                {"d": _DAV, "x": "http://apple.com/ns/ical/"},
            )
        )
        self.assertEqual(
            [child.tag for child in prop],
            [
                self._expanded(_DAV, "resourcetype"),
                self._expanded(_DAV, "displayname"),
                self._expanded("http://apple.com/ns/ical/", "calendar-color"),
            ],
        )

    def test_correct_explicit_d_namespace_is_not_duplicated(self) -> None:
        body = _propfind_body(
            "<x:calendar-home-set/>",
            {"d": _DAV, "x": _CALDAV},
        )
        self.assertEqual(body.count(b'xmlns:d="DAV:"'), 1)
        self._assert_propfind_root(body)

    def test_wrong_explicit_d_namespace_fails_deterministically(self) -> None:
        with self.assertRaisesRegex(ValueError, "PROPFIND DAV namespace binding is invalid"):
            _propfind_body("<d:current-user-principal/>", {"d": "urn:wrong"})

    def test_other_read_only_xml_builders_bind_every_prefix(self) -> None:
        ET.fromstring(_calendar_query_body(W1))
        ET.fromstring(_calendar_multiget_body(["https://fixture.invalid/event.ics"]))


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

        discovery_bodies = self.transport.bodies[:3]
        self.assertEqual(len(discovery_bodies), 3)
        for body in discovery_bodies:
            root = ET.fromstring(body)
            self.assertEqual(root.tag, "{DAV:}propfind")
            self.assertIsNotNone(root.find("{DAV:}prop"))
        home_prop = ET.fromstring(discovery_bodies[1]).find("{DAV:}prop")
        self.assertIsNotNone(home_prop)
        self.assertEqual(home_prop[0].tag, "{urn:ietf:params:xml:ns:caldav}calendar-home-set")

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
        self.assertEqual(self.transport.calls[1][1], "https://fixture.invalid/principal/")
        self.assertEqual(self.transport.calls[2][1], "https://fixture.invalid/home/")
        self.assertTrue(all(method in {"PROPFIND", "REPORT"} for method, _, _ in self.transport.calls))

    async def test_property_scoped_discovery_rejects_unsafe_or_unsuccessful_results(self) -> None:
        self.transport.calls.clear()
        self.transport.discovery_variant = "principal_missing"
        with self.assertRaises(ProviderPayloadError):
            await self.provider.discover_account()
        self.assertEqual(len(self.transport.calls), 1)

        self.transport.calls.clear()
        self.transport.discovery_variant = "principal_unauthenticated"
        with self.assertRaises(ProviderAuthError):
            await self.provider.discover_account()
        self.assertEqual(len(self.transport.calls), 1)

        self.transport.calls.clear()
        self.transport.discovery_variant = "principal_non_2xx"
        with self.assertRaises(ProviderPayloadError):
            await self.provider.discover_account()
        self.assertEqual(len(self.transport.calls), 1)

        self.transport.calls.clear()
        self.transport.discovery_variant = "home_missing"
        with self.assertRaises(ProviderPayloadError):
            await self.provider.discover_account()
        self.assertEqual([method for method, _, _ in self.transport.calls], ["PROPFIND", "PROPFIND"])

        self.transport.calls.clear()
        self.transport.discovery_variant = "home_non_2xx"
        with self.assertRaises(ProviderPayloadError):
            await self.provider.discover_account()
        self.assertEqual([method for method, _, _ in self.transport.calls], ["PROPFIND", "PROPFIND"])

    async def test_empty_calendar_query_accepts_only_dav_multistatus(self) -> None:
        calendars = await self.provider.list_calendars()
        self.transport.report_payload = b'<d:multistatus xmlns:d="DAV:" />'
        self.assertEqual(await self.provider.fetch_events(calendars[0], W1), [])

        self.transport.report_payload = b'<d:multistatus xmlns:d="DAV:">\n  \n</d:multistatus>'
        self.assertEqual(await self.provider.fetch_events(calendars[0], W1), [])

        invalid_payloads = (
            b'<d:collection xmlns:d="DAV:" />',
            b'<d:multistatus xmlns:d="urn:wrong" />',
            b'<html />',
            b"not xml",
        )
        for payload in invalid_payloads:
            self.transport.report_payload = payload
            with self.assertRaises(ProviderPayloadError) as raised:
                await self.provider.fetch_events(calendars[0], W1)
            self.assertEqual(raised.exception.code, "provider_xml_invalid")

    async def test_nonempty_calendar_query_still_requires_successful_calendar_data(self) -> None:
        calendars = await self.provider.list_calendars()
        self.transport.report_payload = _multistatus(
            resource_hrefs=["event-1.ics"],
            no_data_hrefs={"event-1.ics"},
        )
        with self.assertRaises(ProviderPayloadError) as raised:
            await self.provider.fetch_events(calendars[0], W1)
        self.assertEqual(raised.exception.code, "provider_calendar_data_missing")

    async def test_empty_multiget_is_verification_incomplete(self) -> None:
        await self.cache.refresh(W1)
        row = self.database.connection.execute(
            "SELECT resource_ref, provider_calendar_id FROM provider_event_cache WHERE resource_ref IS NOT NULL LIMIT 1"
        ).fetchone()
        calendar = next(
            calendar
            for calendar in await self.provider.list_calendars()
            if calendar.provider_calendar_id == row["provider_calendar_id"]
        )
        self.transport.multiget_mode = "empty"
        with self.assertRaises(ProviderPayloadError) as raised:
            await self.provider.verify_resources(calendar, [row["resource_ref"]], W2)
        self.assertEqual(raised.exception.code, "provider_resource_verification_incomplete")

    async def test_mixed_empty_and_populated_calendars_refresh_current(self) -> None:
        self.transport.empty_calendar_numbers = {1}
        result = await self.cache.refresh(W1)
        self.assertEqual(result.status, "current")
        self.assertEqual(result.calendars_seen, 2)
        self.assertEqual(result.events_seen, 7)
        self.assertEqual(
            self.database.connection.execute(
                "SELECT COUNT(*) FROM calendar_events WHERE source = 'calendar-provider' AND deleted_at IS NULL"
            ).fetchone()[0],
            7,
        )
        self.assertEqual(self.cache.source_metadata()[1]["status"], "current")

    async def test_all_empty_calendars_refresh_current(self) -> None:
        self.transport.empty_calendar_numbers = {1, 2}
        result = await self.cache.refresh(W1)
        self.assertEqual(result.status, "current")
        self.assertEqual(result.events_seen, 0)
        self.assertEqual(
            self.database.connection.execute(
                "SELECT COUNT(*) FROM calendar_events WHERE source = 'calendar-provider'"
            ).fetchone()[0],
            0,
        )
        source = self.cache.source_metadata()[1]
        self.assertEqual(source["status"], "current")
        self.assertTrue(all(calendar["status"] == "current" for calendar in source["calendars"]))

    async def test_empty_query_with_present_multiget_does_not_tombstone_cached_event(self) -> None:
        await self.cache.refresh(W1)
        native = PlanningRepository(self.database).create_calendar_event(
            title="Native remains",
            all_day=False,
            timezone="Europe/Moscow",
            start_at_utc="2026-08-17T08:00:00Z",
            end_at_utc="2026-08-17T09:00:00Z",
            context=CONTEXT,
        )
        self.transport.empty_calendar_numbers = {1, 2}
        result = await self.cache.refresh(W2)
        self.assertEqual(result.status, "current")
        self.assertEqual(result.tombstones_created, 0)
        self.assertEqual(
            self.database.connection.execute(
                "SELECT COUNT(*) FROM calendar_events WHERE source = 'calendar-provider' AND deleted_at IS NOT NULL"
            ).fetchone()[0],
            0,
        )
        self.assertIsNone(EventService(self.database).get(native.id).deleted_at)
        self.assertEqual(
            self.database.connection.execute(
                "SELECT COUNT(*) FROM calendar_events WHERE source = 'calendar-provider' AND sync_state = 'synced'"
            ).fetchone()[0],
            14,
        )

    async def test_empty_query_with_incomplete_multiget_creates_zero_tombstones(self) -> None:
        await self.cache.refresh(W1)
        self.transport.empty_calendar_numbers = {1, 2}
        self.transport.multiget_mode = "empty"
        result = await self.cache.refresh(W2)
        self.assertEqual(result.status, "stale")
        self.assertEqual(result.tombstones_created, 0)
        self.assertEqual(
            self.database.connection.execute(
                "SELECT COUNT(*) FROM calendar_events WHERE source = 'calendar-provider' AND deleted_at IS NOT NULL"
            ).fetchone()[0],
            0,
        )

    async def test_multiget_direct_response_statuses_fail_closed(self) -> None:
        await self.cache.refresh(W1)
        row = self.database.connection.execute(
            "SELECT resource_ref, provider_calendar_id FROM provider_event_cache WHERE resource_ref IS NOT NULL LIMIT 1"
        ).fetchone()
        calendar = next(calendar for calendar in await self.provider.list_calendars() if calendar.provider_calendar_id == row["provider_calendar_id"])
        resource_ref = row["resource_ref"]

        self.transport.multiget_mode = "direct_404"
        missing_404 = await self.provider.verify_resources(calendar, [resource_ref], W2)
        self.assertEqual([(item.resource_ref, item.status) for item in missing_404], [(resource_ref, "missing")])

        self.transport.multiget_mode = "direct_410"
        missing_410 = await self.provider.verify_resources(calendar, [resource_ref], W2)
        self.assertEqual([(item.resource_ref, item.status) for item in missing_410], [(resource_ref, "missing")])

        self.transport.multiget_mode = "direct_403"
        with self.assertRaises(ProviderFetchError) as forbidden:
            await self.provider.verify_resources(calendar, [resource_ref], W2)
        self.assertEqual(forbidden.exception.code, "provider_resource_verification_failed")

        self.transport.multiget_mode = "direct_200_no_data"
        with self.assertRaises(ProviderPayloadError) as incomplete:
            await self.provider.verify_resources(calendar, [resource_ref], W2)
        self.assertEqual(incomplete.exception.code, "provider_resource_verification_incomplete")

        self.transport.multiget_mode = "normal"
        present = await self.provider.verify_resources(calendar, [resource_ref], W2)
        self.assertEqual(present[0].status, "present")
        self.assertTrue(present[0].events)

        self.transport.multiget_mode = "contradictory"
        with self.assertRaises(ProviderPayloadError) as contradictory:
            await self.provider.verify_resources(calendar, [resource_ref], W2)
        self.assertEqual(contradictory.exception.code, "provider_resource_status_conflict")

        self.transport.multiget_mode = "omitted"
        with self.assertRaises(ProviderPayloadError) as omitted:
            await self.provider.verify_resources(calendar, [resource_ref], W2)
        self.assertEqual(omitted.exception.code, "provider_resource_verification_incomplete")

        self.transport.multiget_mode = "duplicate"
        with self.assertRaises(ProviderPayloadError) as duplicate:
            await self.provider.verify_resources(calendar, [resource_ref], W2)
        self.assertEqual(duplicate.exception.code, "provider_resource_verification_duplicate")

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
        self.assertEqual(malformed.error_code, "provider_xml_invalid")
        self.assertEqual(self.cache.health_snapshot()["providerErrorCode"], "provider_xml_invalid")

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
        await self.cache.refresh(W1)
        before_native = PlanningRepository(self.database).create_calendar_event(
            title="Native remains",
            all_day=True,
            timezone="Europe/Moscow",
            start_date="2026-08-18",
            end_date_exclusive="2026-08-19",
            context=CONTEXT,
        )
        self.transport.fail_next_report = True
        failed = await self.cache.refresh(W2)
        self.assertEqual(failed.status, "stale")
        self.assertEqual(failed.error_code, "provider_timeout")
        self.assertEqual(self.database.connection.execute("SELECT COUNT(*) FROM calendar_events WHERE deleted_at IS NOT NULL").fetchone()[0], 0)
        self.assertEqual(EventService(self.database).get(before_native.id).deleted_at, None)
        self.assertEqual(self.cache.source_metadata()[1]["status"], "stale")

        self.transport.include_second_event = False
        recovered = await self.cache.refresh(W2)
        self.assertEqual(recovered.status, "current")
        self.assertEqual(self.cache.source_metadata()[1]["status"], "current")
        self.assertEqual(recovered.tombstones_created, 0)
        self.assertEqual(recovered.deletions_deferred, 2)
        self.assertEqual(self.database.connection.execute("SELECT COUNT(*) FROM calendar_events WHERE deleted_at IS NOT NULL").fetchone()[0], 0)
        self.assertEqual(EventService(self.database).get(before_native.id).deleted_at, None)

    async def test_transient_missing_resource_is_deferred_and_range_stays_stable(self) -> None:
        await self.cache.refresh(W1)
        api = PlanningApiService(
            self.database,
            default_timezone="Europe/Moscow",
            provider_cache=self.cache,
        )
        before = api.list_events(
            from_utc="2026-08-16T00:00:00Z",
            to_utc="2026-08-23T00:00:00Z",
            limit=100,
            offset=0,
            correlation_id="correlation",
        )
        self.transport.include_second_event = False
        first_missing = await self.cache.refresh(W2)
        during = api.list_events(
            from_utc="2026-08-16T00:00:00Z",
            to_utc="2026-08-23T00:00:00Z",
            limit=100,
            offset=0,
            correlation_id="correlation",
        )
        self.assertEqual(first_missing.status, "current")
        self.assertEqual(first_missing.missing_candidates_seen, 2)
        self.assertEqual(first_missing.tombstones_created, 0)
        self.assertEqual(first_missing.deletions_deferred, 2)
        self.assertEqual(len(during["items"]), len(before["items"]))
        self.transport.include_second_event = True
        recovered = await self.cache.refresh(W2)
        after = api.list_events(
            from_utc="2026-08-16T00:00:00Z",
            to_utc="2026-08-23T00:00:00Z",
            limit=100,
            offset=0,
            correlation_id="correlation",
        )
        self.assertEqual(recovered.tombstones_created, 0)
        self.assertEqual(recovered.deletions_deferred, 0)
        self.assertEqual(len(after["items"]), len(before["items"]))
        self.assertEqual(
            {item["id"] for item in after["items"]},
            {item["id"] for item in before["items"]},
        )

    async def test_same_refresh_fetched_event_wins_over_contradictory_missing_verification(self) -> None:
        await self.cache.refresh(W1)
        self.transport.multiget_mode = "direct_404"
        contradictory = await self.cache.refresh(W2)
        self.assertEqual(contradictory.status, "current")
        self.assertGreater(contradictory.missing_candidates_seen, 0)
        self.assertEqual(contradictory.tombstones_created, 0)
        self.assertEqual(contradictory.deletions_deferred, 0)
        self.assertEqual(
            self.database.connection.execute(
                "SELECT COUNT(*) FROM calendar_events WHERE source = 'calendar-provider' AND deleted_at IS NULL"
            ).fetchone()[0],
            14,
        )

    async def test_provider_failure_resets_pending_deletion_evidence(self) -> None:
        await self.cache.refresh(W1)
        self.transport.include_second_event = False
        first_missing = await self.cache.refresh(W2)
        self.assertEqual(first_missing.deletions_deferred, 2)
        self.transport.fail_next_report = True
        failed = await self.cache.refresh(W2)
        self.assertEqual(failed.status, "stale")
        self.assertEqual(failed.tombstones_created, 0)
        self.transport.include_second_event = False
        after_failure = await self.cache.refresh(W2)
        self.assertEqual(after_failure.tombstones_created, 0)
        self.assertEqual(after_failure.deletions_deferred, 2)
        self.assertEqual(
            self.database.connection.execute(
                "SELECT COUNT(*) FROM calendar_events WHERE source = 'calendar-provider' AND deleted_at IS NOT NULL"
            ).fetchone()[0],
            0,
        )

    async def test_pending_deletion_confirmation_survives_restart(self) -> None:
        restart_path = Path(self.temp.name) / "restart.sqlite3"
        first_database = PlanningDatabase(restart_path)
        first_transport = FixtureCalDavTransport()
        first_provider = ICloudCalDavProvider(
            transport=first_transport,
            account_name="owner@example.invalid",
            default_timezone="Europe/Moscow",
        )
        first_cache = ProviderCalendarCache(
            first_database,
            provider=first_provider,
            provider_name="icloud",
            account_id=first_provider.account_id_for("owner@example.invalid"),
            display_label="iCloud",
            enabled=True,
            configured=True,
            now_fn=lambda: NOW,
        )
        try:
            await first_cache.refresh(W1)
            first_transport.include_second_event = False
            deferred = await first_cache.refresh(W2)
            self.assertEqual(deferred.tombstones_created, 0)
        finally:
            await first_provider.close()
            first_database.close()

        second_database = PlanningDatabase(restart_path)
        second_transport = FixtureCalDavTransport()
        second_transport.include_second_event = False
        second_provider = ICloudCalDavProvider(
            transport=second_transport,
            account_name="owner@example.invalid",
            default_timezone="Europe/Moscow",
        )
        second_cache = ProviderCalendarCache(
            second_database,
            provider=second_provider,
            provider_name="icloud",
            account_id=second_provider.account_id_for("owner@example.invalid"),
            display_label="iCloud",
            enabled=True,
            configured=True,
            now_fn=lambda: NOW,
        )
        try:
            confirmed = await second_cache.refresh(W2)
            self.assertEqual(confirmed.tombstones_created, 2)
            self.assertEqual(
                second_database.connection.execute(
                    "SELECT COUNT(*) FROM calendar_events WHERE deleted_at IS NOT NULL"
                ).fetchone()[0],
                2,
            )
        finally:
            await second_provider.close()
            second_database.close()

    async def test_shifted_windows_verify_stable_identity_and_true_remote_deletion(self) -> None:
        await self.cache.refresh(W1)
        initial_provider_ids = {
            row["id"]
            for row in self.database.connection.execute(
                "SELECT id FROM calendar_events WHERE source = 'calendar-provider'"
            ).fetchall()
        }
        before = self.database.connection.execute(
            "SELECT id, provider_id FROM calendar_events WHERE title = '<b>literal</b>' ORDER BY provider_calendar_id"
        ).fetchall()
        self.assertEqual(len(before), 2)

        await self.cache.refresh(W2)
        after_shift = self.database.connection.execute(
            "SELECT id, provider_id FROM calendar_events WHERE title = '<b>literal</b>' ORDER BY provider_calendar_id"
        ).fetchall()
        self.assertEqual([(row["id"], row["provider_id"]) for row in before], [(row["id"], row["provider_id"]) for row in after_shift])

        self.transport.shift_second_event = True
        await self.cache.refresh(W2)
        changed = self.database.connection.execute(
            "SELECT id, title, start_at_utc FROM calendar_events WHERE title = 'changed <b>literal</b>' ORDER BY provider_calendar_id"
        ).fetchall()
        self.assertEqual([row["id"] for row in changed], [row["id"] for row in before])
        self.assertTrue(all(row["start_at_utc"] == "2026-08-20T08:00:00Z" for row in changed))

        self.transport.delete_second_event = True
        deferred = await self.cache.refresh(W2)
        self.assertTrue(any("calendar-multiget" in body for body in self.transport.bodies))
        self.assertEqual(deferred.tombstones_created, 0)
        self.assertEqual(deferred.deletions_deferred, 2)
        self.assertEqual(
            self.database.connection.execute(
                "SELECT COUNT(*) FROM calendar_events WHERE source = 'calendar-provider' AND deleted_at IS NOT NULL"
            ).fetchone()[0],
            0,
        )
        confirmed = await self.cache.refresh(W2)
        self.assertEqual(confirmed.tombstones_created, 2)
        self.assertEqual(confirmed.deletions_confirmed, 2)
        self.assertEqual(
            self.database.connection.execute(
                "SELECT COUNT(*) FROM calendar_events WHERE source = 'calendar-provider' AND deleted_at IS NOT NULL"
            ).fetchone()[0],
            2,
        )
        self.assertEqual(
            self.database.connection.execute(
                "SELECT COUNT(*) FROM calendar_events WHERE source = 'calendar-provider' AND deleted_at IS NULL"
            ).fetchone()[0],
            12,
        )
        tombstone = self.database.connection.execute(
            "SELECT provider_id, provider_calendar_id FROM calendar_events WHERE deleted_at IS NOT NULL LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(tombstone["provider_id"])
        self.assertIsNotNone(tombstone["provider_calendar_id"])
        tombstone_versions = self.database.connection.execute(
            "SELECT id, version FROM calendar_events WHERE deleted_at IS NOT NULL ORDER BY id"
        ).fetchall()
        repeated = await self.cache.refresh(W2)
        self.assertEqual(repeated.tombstones_created, 0)
        self.assertEqual(
            [(row["id"], row["version"]) for row in tombstone_versions],
            [
                (row["id"], row["version"])
                for row in self.database.connection.execute(
                    "SELECT id, version FROM calendar_events WHERE deleted_at IS NOT NULL ORDER BY id"
                ).fetchall()
            ],
        )
        self.transport.delete_second_event = False
        resurrected = await self.cache.refresh(W2)
        self.assertEqual(resurrected.tombstones_created, 0)
        self.assertEqual(
            self.database.connection.execute(
                "SELECT COUNT(*) FROM calendar_events WHERE source = 'calendar-provider' AND deleted_at IS NULL"
            ).fetchone()[0],
            14,
        )
        self.assertEqual(
            {row["id"] for row in self.database.connection.execute(
                "SELECT id FROM calendar_events WHERE source = 'calendar-provider'"
            ).fetchall()},
            initial_provider_ids,
        )

    async def test_moved_outside_window_is_present_but_not_tombstoned(self) -> None:
        await self.cache.refresh(W1)
        self.transport.move_second_event_outside = True
        moved = await self.cache.refresh(W2)
        self.assertEqual(moved.tombstones_created, 0)
        rows = self.database.connection.execute(
            "SELECT deleted_at, sync_state FROM calendar_events WHERE title = '<b>literal</b>'"
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["deleted_at"] is None for row in rows))
        self.assertTrue(all(row["sync_state"] == "stale" for row in rows))

    async def test_disappeared_calendar_marks_cached_events_stale_without_deleting_them(self) -> None:
        await self.cache.refresh(W1)
        second_calendar_id = self.database.connection.execute(
            "SELECT provider_calendar_id FROM provider_calendars ORDER BY provider_calendar_id DESC LIMIT 1"
        ).fetchone()[0]
        self.transport.include_second_calendar = False
        result = await self.cache.refresh(W2)
        self.assertEqual(result.status, "current")
        calendar = self.database.connection.execute(
            "SELECT enabled, status, last_error_code FROM provider_calendars WHERE provider_calendar_id = ?",
            (second_calendar_id,),
        ).fetchone()
        self.assertEqual(calendar["enabled"], 0)
        self.assertEqual(calendar["status"], "disabled")
        self.assertEqual(calendar["last_error_code"], "provider_calendar_disappeared")
        events = self.database.connection.execute(
            "SELECT sync_state, deleted_at FROM calendar_events WHERE provider_calendar_id = ?",
            (second_calendar_id,),
        ).fetchall()
        self.assertEqual(len(events), 7)
        self.assertTrue(all(row["sync_state"] == "stale" for row in events))
        self.assertTrue(all(row["deleted_at"] is None for row in events))

    async def test_dense_recurrence_is_rejected_before_library_expansion(self) -> None:
        self.transport.dense_only = True
        calendars = await self.provider.list_calendars()
        with patch("app.planning.providers.icloud.recurring_ical_events.of") as expand:
            with self.assertRaises(ProviderPayloadError) as raised:
                await self.provider.fetch_events(calendars[0], W2)
        self.assertEqual(raised.exception.code, "provider_event_limit")
        expand.assert_not_called()

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
        resource_ref = self.database.connection.execute(
            "SELECT resource_ref FROM provider_event_cache WHERE resource_ref IS NOT NULL LIMIT 1"
        ).fetchone()[0]
        self.assertTrue(resource_ref)
        self.assertNotIn(resource_ref, serialized)
        self.assertNotIn(resource_ref, str(self.cache.health_snapshot()))
        event_source_ref = self.database.connection.execute(
            "SELECT source_ref FROM calendar_events WHERE id = ?", (imported,)
        ).fetchone()[0]
        self.assertNotEqual(event_source_ref, resource_ref)
        self.assertNotIn(resource_ref, str(self.database.connection.execute("SELECT * FROM audit_events").fetchall()))
        with self.assertNoLogs("app.planning.providers", level="INFO"):
            await self.cache.refresh(W2)

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
