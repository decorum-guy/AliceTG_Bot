from __future__ import annotations

import json
import unittest
from textwrap import dedent

from app.planning.providers.contracts import ProviderAuthError, ProviderFetchError, ProviderTimeoutError
from app.planning.providers.icloud import AiohttpCalDavTransport, _trusted_resource_ref
from app.planning.providers.icloud_vtodo_probe import ICloudVTodoProbe


BASE = "https://fixture.invalid"
HOME = f"{BASE}/home/"


def _discovery_response() -> bytes:
    return b'''<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
<d:response><d:href>/wrong-resource/</d:href><d:propstat><d:prop>
<d:current-user-principal><d:href>/principal/</d:href></d:current-user-principal>
</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response></d:multistatus>'''


def _home_response() -> bytes:
    return b'''<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
<d:response><d:href>/wrong-principal-resource/</d:href><d:propstat><d:prop>
<c:calendar-home-set><d:href>/home/</d:href></c:calendar-home-set>
</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response></d:multistatus>'''


def _collection(
    path: str,
    components: tuple[str, ...] | None,
    *,
    readonly: bool = False,
    shared: bool = False,
    freshness: bool = False,
) -> str:
    component_set = "" if components is None else (
        "<c:supported-calendar-component-set>"
        + "".join(f'<c:comp name="{name}"/>' for name in components)
        + "</c:supported-calendar-component-set>"
    )
    privileges = (
        "<d:current-user-privilege-set><d:privilege><d:read/></d:privilege></d:current-user-privilege-set>"
        if readonly
        else "<d:current-user-privilege-set><d:privilege><d:read/><d:write-content/></d:privilege></d:current-user-privilege-set>"
    )
    owner = "<d:owner><d:href>/synthetic-owner/</d:href></d:owner>" if shared else ""
    revision = (
        '<d:sync-token>https://fixture.invalid/sync/synthetic-1</d:sync-token>'
        '<d:resource-id><d:href>/synthetic-resource-id</d:href></d:resource-id>'
        '<d:getetag>"synthetic-collection-etag"</d:getetag>'
        '<cs:getctag>synthetic-ctag</cs:getctag>'
        if freshness
        else ""
    )
    return dedent(
        f'''
        <d:response><d:href>{path}</d:href><d:propstat><d:prop>
        <d:resourcetype><c:calendar/></d:resourcetype>
        <d:displayname>PRIVATE SYNTHETIC LIST NAME</d:displayname>
        {component_set}
        {privileges}
        <d:supported-report-set>
          <d:supported-report><d:report><c:calendar-query/></d:report></d:supported-report>
          <d:supported-report><d:report><c:calendar-multiget/></d:report></d:supported-report>
          <d:supported-report><d:report><d:sync-collection/></d:report></d:supported-report>
        </d:supported-report-set>
        {revision}
        {owner}
        </d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>
        '''
    )


def _collections(*items: str) -> bytes:
    return (
        '<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav" '
        'xmlns:cs="http://calendarserver.org/ns/">'
        + "".join(items)
        + "</d:multistatus>"
    ).encode()


def _calendar(*vtodos: str, include_vevent: bool = False) -> str:
    components = ("\n".join(vtodos))
    if include_vevent:
        components = """
BEGIN:VEVENT
UID:synthetic-event-should-not-be-read
DTSTART:20260820T090000Z
DTEND:20260820T100000Z
SUMMARY:SYNTHETIC VEVENT
END:VEVENT
""" + components
    return f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Synthetic B4 Fixture//EN
{components}
END:VCALENDAR
"""


def _vtodo(
    uid: str,
    *,
    status: str | None = "NEEDS-ACTION",
    due: str | None = None,
    due_params: str = "",
    dtstart: str | None = None,
    rrule: str | None = None,
    recurrence_id: str | None = None,
    extra: str = "",
) -> str:
    status_line = f"STATUS:{status}\n" if status else ""
    due_line = f"DUE{due_params}:{due}\n" if due else ""
    dtstart_line = f"DTSTART:{dtstart}\n" if dtstart else ""
    rrule_line = f"RRULE:{rrule}\n" if rrule else ""
    recurrence_line = f"RECURRENCE-ID:{recurrence_id}\n" if recurrence_id else ""
    return f"""BEGIN:VTODO
UID:{uid}
DTSTAMP:20260816T120000Z
SUMMARY:SHOULD_NOT_LEAK_PRIVATE_SYNTHETIC_TITLE
DESCRIPTION:SHOULD_NOT_LEAK_PRIVATE_SYNTHETIC_NOTE
{status_line}{due_line}{dtstart_line}{rrule_line}{recurrence_line}{extra}END:VTODO
"""


def _resource_response(href: str, payload: str, *, etag: str = '"synthetic-etag"') -> str:
    return f"""<d:response><d:href>{href}</d:href><d:propstat><d:prop>
<d:getetag>{etag}</d:getetag><c:calendar-data><![CDATA[{payload}]]></c:calendar-data>
</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"""


def _resources(*items: tuple[str, str]) -> bytes:
    return (
        '<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
        + "".join(_resource_response(href, payload) for href, payload in items)
        + "</d:multistatus>"
    ).encode()


class FixtureReadOnlyTransport:
    bootstrap_url = f"{BASE}/.well-known/caldav"

    def __init__(self, collection_payload: bytes, reports: dict[str, bytes] | None = None) -> None:
        self.collection_payload = collection_payload
        self.reports = reports or {}
        self.calls: list[tuple[str, str, str]] = []
        self.bodies: list[str] = []
        self.report_error: BaseException | None = None

    async def propfind(self, url: str, *, body: bytes, depth: str) -> bytes:
        self.calls.append(("PROPFIND", url, depth))
        self.bodies.append(body.decode("utf-8"))
        if "current-user-principal" in body.decode("utf-8"):
            return _discovery_response()
        if "calendar-home-set" in body.decode("utf-8"):
            return _home_response()
        return self.collection_payload

    async def report(self, url: str, *, body: bytes, depth: str) -> bytes:
        self.calls.append(("REPORT", url, depth))
        body_text = body.decode("utf-8")
        self.bodies.append(body_text)
        if self.report_error is not None:
            raise self.report_error
        if "VTODO" not in body_text:
            raise AssertionError("probe issued a non-VTODO REPORT")
        return self.reports.get(
            url,
            b'<d:multistatus xmlns:d="DAV:" />',
        )


def _probe(transport: FixtureReadOnlyTransport) -> dict[str, object]:
    import asyncio

    return asyncio.run(
        ICloudVTodoProbe(
            transport=transport,
            account_name="synthetic-owner@example.invalid",
            max_collections=8,
            max_resources_per_collection=8,
        ).run()
    )


class ICloudVTodoProbeTests(unittest.TestCase):
    def test_no_vtodo_collection_does_not_infer_from_names(self) -> None:
        transport = FixtureReadOnlyTransport(
            _collections(_collection("/home/calendar/", ("VEVENT",)))
        )
        result = _probe(transport)
        self.assertEqual(result["vTodoCollections"]["count"], 0)
        self.assertEqual(result["resourceRead"]["status"], "not_observed")
        self.assertEqual([method for method, _, _ in transport.calls], ["PROPFIND"] * 3)
        serialized = json.dumps(result, sort_keys=True)
        self.assertNotIn("PRIVATE SYNTHETIC LIST NAME", serialized)

    def test_vtodo_collection_and_empty_query_are_supported(self) -> None:
        path = "/home/reminders/"
        transport = FixtureReadOnlyTransport(_collections(_collection(path, ("VTODO",), readonly=True)))
        result = _probe(transport)
        self.assertEqual(result["vTodoCollections"]["count"], 1)
        self.assertEqual(result["vTodoCollections"]["readable"], "supported")
        self.assertEqual(result["resourceRead"]["itemsSeen"], 0)
        self.assertEqual(result["collections"][0]["queryStatus"], "supported")
        self.assertEqual([method for method, _, _ in transport.calls], ["PROPFIND", "PROPFIND", "PROPFIND", "REPORT"])
        self.assertEqual(result["transport"]["allowedMethods"], ["PROPFIND", "REPORT"])
        self.assertFalse(result["transport"]["writesAvailable"])
        self.assertIn('<c:comp-filter name="VTODO"><c:time-range ', transport.bodies[-1])
        self.assertNotIn("SUMMARY", transport.bodies[-1])

    def test_mixed_component_capability_is_distinguished(self) -> None:
        path = "/home/mixed/"
        transport = FixtureReadOnlyTransport(_collections(_collection(path, ("VEVENT", "VTODO"))))
        result = _probe(transport)
        capability = result["collections"][0]["componentCapability"]
        self.assertEqual(capability["advertisedComponents"], ["VEVENT", "VTODO"])
        self.assertTrue(capability["mixedVeventVtodo"])

    def test_vtodo_fields_are_observed_without_returning_private_content(self) -> None:
        path = "/home/reminders/"
        open_item = _calendar(
            _vtodo(
                "synthetic-open",
                due="20260820",
                due_params=";VALUE=DATE",
                extra="PRIORITY:1\nLOCATION:SHOULD_NOT_LEAK_LOCATION\nURL:https://fixture.invalid/private\n",
            )
        )
        completed_item = _calendar(
            _vtodo(
                "synthetic-completed",
                status="COMPLETED",
                due="20260820T120000",
                due_params=";TZID=Europe/Moscow",
                extra="COMPLETED:20260819T120000Z\nPERCENT-COMPLETE:100\n",
            )
        )
        recurring_item = _calendar(
            _vtodo(
                "synthetic-recurring",
                status=None,
                dtstart="20260817T120000Z",
                rrule="FREQ=DAILY;COUNT=3",
                extra="EXDATE:20260818T120000Z\nCATEGORIES:synthetic\nX-APPLE-FLAGGED:1\n",
            )
        )
        exception_item = _calendar(
            _vtodo(
                "synthetic-recurring",
                status="NEEDS-ACTION",
                dtstart="20260818T140000Z",
                recurrence_id="20260818T120000Z",
                extra="RELATED-TO:synthetic-parent\nBEGIN:VALARM\nACTION:DISPLAY\nTRIGGER:-PT15M\nEND:VALARM\n",
            )
        )
        reports = {
            f"{BASE}{path}": _resources(
                ("open.ics", open_item),
                ("completed.ics", completed_item),
                ("recurring.ics", recurring_item),
                ("exception.ics", exception_item),
            )
        }
        transport = FixtureReadOnlyTransport(_collections(_collection(path, ("VTODO",))), reports)
        result = _probe(transport)
        serialized = json.dumps(result, sort_keys=True)
        self.assertNotIn("SHOULD_NOT_LEAK", serialized)
        self.assertEqual(result["resourceRead"]["itemsSeen"], 4)
        self.assertEqual(result["completion"]["statusValuesObserved"], ["COMPLETED", "NEEDS-ACTION"])
        self.assertTrue(result["completion"]["openObserved"])
        self.assertTrue(result["completion"]["completedObserved"])
        self.assertEqual(result["completion"]["statusMissingCount"], 1)
        self.assertTrue(result["due"]["dateObserved"])
        self.assertTrue(result["due"]["dateTimeObserved"])
        self.assertTrue(result["due"]["noDueObserved"])
        self.assertEqual(result["due"]["timezoneKindsObserved"], ["TZID"])
        self.assertTrue(result["recurrence"]["rruleObserved"])
        self.assertTrue(result["recurrence"]["recurrenceIdObserved"])
        self.assertTrue(result["recurrence"]["exdateObserved"])
        self.assertEqual(result["recurrence"]["exceptions"], "observed")
        self.assertTrue(result["advancedProperties"]["alarmsObserved"])
        self.assertTrue(result["advancedProperties"]["undocumentedXPropertiesObserved"])
        self.assertEqual(result["identity"]["itemEtagAvailable"], "supported")

    def test_date_time_without_timezone_is_recorded_as_floating(self) -> None:
        path = "/home/reminders/"
        payload = _calendar(
            _vtodo("synthetic-floating", due="20260820T120000", extra="STATUS:NEEDS-ACTION\n")
        )
        transport = FixtureReadOnlyTransport(
            _collections(_collection(path, ("VTODO",))),
            {f"{BASE}{path}": _resources(("floating.ics", payload))},
        )
        result = _probe(transport)
        self.assertEqual(result["due"]["timezoneKindsObserved"], ["floating"])

    def test_duplicate_uid_is_ambiguous_but_distinct_hrefs_remain_visible(self) -> None:
        path = "/home/list/"
        payload = _calendar(_vtodo("same-uid", due="20260820", due_params=";VALUE=DATE"))
        transport = FixtureReadOnlyTransport(
            _collections(_collection(path, ("VTODO",))),
            {f"{BASE}{path}": _resources(("first.ics", payload), ("second.ics", payload))},
        )
        result = _probe(transport)
        self.assertEqual(result["resourceRead"]["itemsSeen"], 2)
        self.assertEqual(result["identity"]["duplicateUidCount"], 1)
        self.assertEqual(result["identity"]["stableIdentity"], "ambiguous")

    def test_same_title_in_distinct_lists_does_not_collapse_collection_identity(self) -> None:
        first_path = "/home/first-list/"
        second_path = "/home/second-list/"
        payload = _calendar(_vtodo("synthetic-same-title"))
        reports = {
            f"{BASE}{first_path}": _resources(("one.ics", payload)),
            f"{BASE}{second_path}": _resources(("one.ics", payload)),
        }
        transport = FixtureReadOnlyTransport(
            _collections(
                _collection(first_path, ("VTODO",)),
                _collection(second_path, ("VTODO",)),
            ),
            reports,
        )
        result = _probe(transport)
        self.assertEqual(result["vTodoCollections"]["count"], 2)
        self.assertEqual(
            len({collection["collectionId"] for collection in result["collections"]}),
            2,
        )
        self.assertEqual(result["resourceRead"]["itemsSeen"], 2)
        self.assertNotIn("SHOULD_NOT_LEAK_PRIVATE_SYNTHETIC_TITLE", json.dumps(result))

    def test_shared_readonly_and_revision_metadata_are_facts_not_identity(self) -> None:
        path = "/home/shared/"
        transport = FixtureReadOnlyTransport(
            _collections(_collection(path, ("VTODO",), readonly=True, shared=True, freshness=True))
        )
        result = _probe(transport)
        collection = result["collections"][0]
        self.assertTrue(collection["hrefIdentity"]["opaqueStableId"])
        self.assertTrue(collection["resourceIdPresent"])
        self.assertTrue(collection["freshness"]["syncTokenPresent"])
        self.assertTrue(collection["freshness"]["ctagPresent"])
        self.assertTrue(collection["privileges"]["readOnly"])
        self.assertTrue(result["sharedLists"]["sharedMetadataObserved"])
        self.assertEqual(result["sharedLists"]["ownerVsParticipant"], "not_observed")
        self.assertEqual(result["deletion"]["status"], "not_testable_safely")
        self.assertTrue(result["freshness"]["syncTokenObserved"])

    def test_malformed_xml_and_ics_are_sanitized_and_fail_closed(self) -> None:
        path = "/home/reminders/"
        transport = FixtureReadOnlyTransport(
            _collections(_collection(path, ("VTODO",))),
            {f"{BASE}{path}": b"not xml"},
        )
        result = _probe(transport)
        self.assertEqual(result["resourceRead"]["status"], "failed")
        self.assertEqual(result["errors"], [{"layer": "resourceRead", "code": "provider_xml_invalid"}])
        entity_transport = FixtureReadOnlyTransport(
            _collections(_collection(path, ("VTODO",))),
            {f"{BASE}{path}": b'<!DOCTYPE x [<!ENTITY file SYSTEM "file:///private/synthetic">]><x>&file;</x>'},
        )
        entity_result = _probe(entity_transport)
        self.assertEqual(
            entity_result["errors"],
            [{"layer": "resourceRead", "code": "provider_xml_invalid"}],
        )
        malformed_ics_transport = FixtureReadOnlyTransport(
            _collections(_collection(path, ("VTODO",))),
            {f"{BASE}{path}": _resources(("malformed.ics", "BEGIN:VCALENDAR\nBEGIN:VTODO\nUID:"))},
        )
        malformed_result = _probe(malformed_ics_transport)
        self.assertEqual(
            malformed_result["errors"],
            [{"layer": "resourceRead", "code": "provider_vtodo_calendar_data_invalid"}],
        )

    def test_mixed_resource_payload_and_duplicate_href_are_rejected(self) -> None:
        path = "/home/reminders/"
        mixed = _calendar(
            _vtodo("synthetic-vtodo"),
            include_vevent=True,
        )
        mixed_transport = FixtureReadOnlyTransport(
            _collections(_collection(path, ("VTODO",))),
            {f"{BASE}{path}": _resources(("mixed.ics", mixed))},
        )
        mixed_result = _probe(mixed_transport)
        self.assertEqual(
            mixed_result["errors"],
            [{"layer": "resourceRead", "code": "provider_vtodo_mixed_component_payload"}],
        )
        duplicate_payload = (
            '<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
            + _resource_response("duplicate.ics", _calendar(_vtodo("one")))
            + _resource_response("duplicate.ics", _calendar(_vtodo("two")))
            + "</d:multistatus>"
        ).encode()
        duplicate_transport = FixtureReadOnlyTransport(
            _collections(_collection(path, ("VTODO",))),
            {f"{BASE}{path}": duplicate_payload},
        )
        duplicate_result = _probe(duplicate_transport)
        self.assertEqual(
            duplicate_result["errors"],
            [{"layer": "resourceRead", "code": "provider_vtodo_resource_duplicate"}],
        )

    def test_untrusted_collection_href_is_rejected(self) -> None:
        transport = FixtureReadOnlyTransport(
            _collections(_collection("https://evil.invalid/reminders/", ("VTODO",)))
        )
        result = _probe(transport)
        self.assertEqual(result["collectionDiscovery"]["status"], "failed")
        self.assertEqual(
            result["errors"],
            [{"layer": "collectionDiscovery", "code": "provider_resource_ref_untrusted"}],
        )

    def test_existing_transport_rejects_untrusted_hosts(self) -> None:
        transport = AiohttpCalDavTransport(
            bootstrap_url=f"{BASE}/.well-known/caldav",
            username="synthetic-owner@example.invalid",
            password="synthetic-password",
        )
        with self.assertRaisesRegex(ProviderFetchError, "provider_resource_ref_untrusted"):
            _trusted_resource_ref("https://evil.invalid/reminders/", transport.bootstrap_url)
        with self.assertRaisesRegex(ProviderFetchError, "provider_redirect_untrusted"):
            transport._trusted_url("https://evil.invalid/reminders/")

    def test_provider_errors_are_safe_and_never_include_exception_text(self) -> None:
        path = "/home/reminders/"
        for error in (
            ProviderAuthError("secret-account@example.invalid"),
            ProviderFetchError("provider_rate_limited"),
            ProviderFetchError("provider_server_failure"),
            ProviderTimeoutError("timeout with private text"),
        ):
            transport = FixtureReadOnlyTransport(_collections(_collection(path, ("VTODO",))))
            transport.report_error = error
            result = _probe(transport)
            serialized = json.dumps(result, sort_keys=True)
            self.assertNotIn("secret-account@example.invalid", serialized)
            self.assertNotIn("private text", serialized)
            self.assertIn(result["errors"][0]["code"], serialized)

    def test_missing_resource_status_is_not_treated_as_a_deletion_tombstone(self) -> None:
        path = "/home/reminders/"
        missing_payload = b'''<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
<d:response><d:href>missing.ics</d:href><d:status>HTTP/1.1 410 Gone</d:status></d:response>
</d:multistatus>'''
        transport = FixtureReadOnlyTransport(
            _collections(_collection(path, ("VTODO",))),
            {f"{BASE}{path}": missing_payload},
        )
        result = _probe(transport)
        self.assertEqual(
            result["errors"],
            [{"layer": "resourceRead", "code": "provider_vtodo_read_failed"}],
        )
        self.assertEqual(result["deletion"]["status"], "not_testable_safely")
        self.assertFalse(result["deletion"]["explicitTombstonesObserved"])

    def test_probe_uses_only_read_methods_and_never_writes_planning(self) -> None:
        path = "/home/reminders/"
        transport = FixtureReadOnlyTransport(
            _collections(_collection(path, ("VTODO",))),
            {f"{BASE}{path}": _resources(("one.ics", _calendar(_vtodo("one"))))},
        )
        result = _probe(transport)
        self.assertTrue(all(method in {"PROPFIND", "REPORT"} for method, _, _ in transport.calls))
        self.assertFalse(any(method in {"GET", "PUT", "POST", "PATCH", "DELETE", "MKCALENDAR", "MOVE", "COPY"} for method, _, _ in transport.calls))
        self.assertFalse(result["transport"]["writesAvailable"])
        self.assertFalse(result["transport"]["credentialReturned"])
        self.assertFalse(result["transport"]["arbitraryBrowserRequest"])
        self.assertTrue(all("Authorization" not in body for body in transport.bodies))
        self.assertLessEqual(len(transport.calls), 4)


if __name__ == "__main__":
    unittest.main()
