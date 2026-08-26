from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import urljoin

from icalendar import Calendar

from app.planning.providers.contracts import (
    ProviderAdapterError,
    ProviderAuthError,
    ProviderFetchError,
    ProviderPayloadError,
)
from app.planning.providers.icloud import (
    _CALDAV,
    _DAV,
    ReadOnlyCalDavTransport,
    _direct_child,
    _direct_child_text,
    _direct_children,
    _http_status_code,
    _local_name,
    _namespace,
    _opaque,
    _propfind_body,
    _required_property_href,
    _trusted_resource_ref,
    _xml_root,
)


_APPLE_ICAL = "http://apple.com/ns/ical/"
_CALENDARSERVER = "http://calendarserver.org/ns/"
_MAX_XML_BYTES = 8 * 1024 * 1024
_MAX_DEFAULT_COLLECTIONS = 32
_MAX_DEFAULT_RESOURCES = 128
_WRITE_PRIVILEGES = frozenset(
    {"write", "write-content", "write-properties", "bind", "unbind", "write-acl"}
)


def _collection_propfind_body() -> bytes:
    return _propfind_body(
        """
        <d:resourcetype/>
        <d:displayname/>
        <c:supported-calendar-component-set/>
        <d:current-user-privilege-set/>
        <d:supported-report-set/>
        <d:sync-token/>
        <d:resource-id/>
        <d:getetag/>
        <d:getlastmodified/>
        <d:owner/>
        <x:calendar-color/>
        <x:calendar-order/>
        <cs:getctag/>
        """,
        {"d": _DAV, "c": _CALDAV, "x": _APPLE_ICAL, "cs": _CALENDARSERVER},
    )


def _vtodo_query_body(*, start: datetime, end: datetime) -> bytes:
    """Build the only bounded VTODO read request; it contains no write operation."""

    if start.tzinfo is None or end.tzinfo is None or end <= start:
        raise ValueError("VTODO probe window must be finite and timezone-aware")
    start_text = start.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    end_text = end.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
        '<d:prop><d:getetag/><c:calendar-data/></d:prop>'
        '<c:filter><c:comp-filter name="VCALENDAR">'
        f'<c:comp-filter name="VTODO"><c:time-range start="{start_text}" end="{end_text}"/></c:comp-filter>'
        "</c:comp-filter></c:filter></c:calendar-query>"
    ).encode("utf-8")


@dataclass(frozen=True)
class _Collection:
    href: str
    collection_id: str
    components: frozenset[str] | None
    display_name_present: bool
    resource_id_present: bool
    collection_etag_present: bool
    sync_token_present: bool
    ctag_present: bool
    owner_metadata_present: bool
    privileges_present: bool
    read_privilege_present: bool
    write_privilege_present: bool
    supported_reports: tuple[str, ...]

    @property
    def is_vtodo_capable(self) -> bool:
        return self.components is not None and "VTODO" in self.components


@dataclass(frozen=True)
class _VTodoResource:
    href: str
    etag: str | None
    calendar_data: bytes


def _initial_result() -> dict[str, Any]:
    return {
        "schemaVersion": "b4.apple-vtodo-probe.v1",
        "transport": {
            "allowedMethods": ["PROPFIND", "REPORT"],
            "writesAvailable": False,
            "credentialReturned": False,
            "arbitraryBrowserRequest": False,
        },
        "authentication": {"status": "not_observed"},
        "principalDiscovery": {"status": "not_observed"},
        "calendarHome": {"status": "not_observed"},
        "collectionDiscovery": {
            "status": "not_observed",
            "calendarCount": 0,
            "bounded": True,
        },
        "collections": [],
        "vTodoCollections": {
            "count": 0,
            "explicitlyAdvertised": 0,
            "stableIdentitiesAvailable": "not_observed",
            "readable": "not_observed",
        },
        "resourceRead": {
            "status": "not_observed",
            "supported": "not_observed",
            "collectionsQueried": 0,
            "resourcesSeen": 0,
            "itemsSeen": 0,
            "boundedByMaxResources": True,
            "queryWindowFinite": True,
            "queryWindowDaysPast": 30,
            "queryWindowDaysFuture": 365,
        },
        "identity": {
            "collectionHrefAvailable": "not_observed",
            "collectionResourceIdObserved": False,
            "itemUidAvailable": "not_observed",
            "itemHrefAvailable": "not_observed",
            "itemEtagAvailable": "not_observed",
            "recurrenceIdObserved": False,
            "stableIdentity": "not_observed",
            "duplicateUidCount": 0,
        },
        "freshness": {
            "collectionEtagObserved": False,
            "ctagObserved": False,
            "syncTokenObserved": False,
            "itemEtagAvailable": "not_observed",
            "incrementalVTodoSync": "not_tested_safely",
        },
        "completion": {
            "statusValuesObserved": [],
            "openObserved": False,
            "completedObserved": False,
            "cancelledObserved": False,
            "statusMissingCount": 0,
            "completedPropertyObserved": False,
            "percentCompleteObserved": False,
            "ambiguousCount": 0,
            "consistencyAmbiguousCount": 0,
        },
        "due": {
            "dateObserved": False,
            "dateTimeObserved": False,
            "noDueObserved": False,
            "timezoneKindsObserved": [],
            "dtstartObserved": False,
            "durationObserved": False,
            "dateOnlyPreserved": True,
        },
        "recurrence": {
            "rruleObserved": False,
            "recurrenceIdObserved": False,
            "exdateObserved": False,
            "rdateObserved": False,
            "expanded": False,
            "exceptions": "not_observed",
            "limitations": [
                "The probe records recurrence properties but does not expand or mutate a series.",
            ],
        },
        "advancedProperties": {
            "priorityObserved": False,
            "notesObserved": False,
            "urlObserved": False,
            "locationObserved": False,
            "alarmsObserved": False,
            "parentRelationshipObserved": False,
            "flaggedOrTagsObserved": False,
            "undocumentedXPropertiesObserved": False,
        },
        "deletion": {
            "status": "not_testable_safely",
            "syncTokenObserved": False,
            "explicitTombstonesObserved": False,
            "absenceOnly": True,
        },
        "sharedLists": {
            "sharedMetadataObserved": False,
            "ownerVsParticipant": "not_observed",
            "readPrivilegeObserved": False,
            "readOnlyCollections": 0,
            "privilegeLimitations": "not_observed",
        },
        "errors": [],
    }


def _error_code(error: BaseException) -> str:
    if isinstance(error, ProviderAdapterError):
        return error.code
    return "provider_probe_failed"


def _record_error(result: dict[str, Any], layer: str, error: BaseException) -> None:
    result["errors"].append({"layer": layer, "code": _error_code(error)})


def _safe_xml_root(payload: bytes):
    if b"<!DOCTYPE" in payload.upper() or b"<!ENTITY" in payload.upper():
        raise ProviderPayloadError("provider_xml_invalid")
    if len(payload) > _MAX_XML_BYTES:
        raise ProviderPayloadError("provider_payload_too_large")
    return _xml_root(payload)


def _successful_props(response: Any) -> list[Any]:
    props: list[Any] = []
    for propstat in _direct_children(response, "propstat", namespace=_DAV):
        status = _direct_child_text(propstat, "status", namespace=_DAV)
        status_code = _http_status_code(status)
        if status_code is not None and not 200 <= status_code < 300:
            continue
        prop = _direct_child(propstat, "prop", namespace=_DAV)
        if prop is not None:
            props.append(prop)
    return props


def _property(props: list[Any], local_name: str, namespace: str | None = None) -> Any | None:
    for prop in props:
        value = _direct_child(prop, local_name, namespace=namespace)
        if value is not None:
            return value
    return None


def _has_property(props: list[Any], local_name: str, namespace: str | None = None) -> bool:
    return _property(props, local_name, namespace) is not None


def _parse_components(component_property: Any | None) -> frozenset[str] | None:
    if component_property is None:
        return None
    components: set[str] = set()
    for component in _direct_children(component_property, "comp", namespace=_CALDAV):
        name = (component.attrib.get("name") or "").strip().upper()
        if not name:
            raise ProviderPayloadError("provider_vtodo_component_set_invalid")
        components.add(name)
    if not components:
        raise ProviderPayloadError("provider_vtodo_component_set_invalid")
    return frozenset(components)


def _parse_privileges(privilege_property: Any | None) -> tuple[bool, bool, bool]:
    if privilege_property is None:
        return False, False, False
    names = {
        _local_name(element.tag).lower()
        for element in privilege_property.iter()
        if _namespace(element.tag) == _DAV
    }
    read = "read" in names or "all" in names
    write = bool(names & _WRITE_PRIVILEGES)
    return True, read, write


def _parse_reports(report_property: Any | None) -> tuple[str, ...]:
    if report_property is None:
        return ()
    reports: set[str] = set()
    for supported_report in _direct_children(report_property, "supported-report", namespace=_DAV):
        report = _direct_child(supported_report, "report", namespace=_DAV)
        if report is None:
            continue
        for child in list(report):
            reports.add(_local_name(child.tag))
    return tuple(sorted(reports))


def _parse_collections(payload: bytes, base_url: str, account_id: str) -> list[_Collection]:
    root = _safe_xml_root(payload)
    if root.tag != f"{{{_DAV}}}multistatus":
        raise ProviderPayloadError("provider_xml_invalid")
    results: list[_Collection] = []
    seen_hrefs: set[str] = set()
    for response in _xml_responses(root):
        href = _direct_child_text(response, "href", namespace=_DAV)
        if not href:
            raise ProviderPayloadError("provider_collection_href_missing")
        absolute_href = _trusted_resource_ref(urljoin(base_url, href), base_url)
        if absolute_href in seen_hrefs:
            raise ProviderPayloadError("provider_collection_duplicate")
        seen_hrefs.add(absolute_href)
        props = _successful_props(response)
        resource_type = _property(props, "resourcetype", _DAV)
        if resource_type is None:
            continue
        if not any(
            _local_name(child.tag) == "calendar" and _namespace(child.tag) == _CALDAV
            for child in list(resource_type)
        ):
            continue
        privileges_present, read_privilege, write_privilege = _parse_privileges(
            _property(props, "current-user-privilege-set", _DAV)
        )
        results.append(
            _Collection(
                href=absolute_href,
                collection_id=_opaque("vtodo_collection", f"{account_id}|{absolute_href}"),
                components=_parse_components(
                    _property(props, "supported-calendar-component-set", _CALDAV)
                ),
                display_name_present=_has_property(props, "displayname", _DAV),
                resource_id_present=_has_property(props, "resource-id", _DAV),
                collection_etag_present=_has_property(props, "getetag", _DAV),
                sync_token_present=_has_property(props, "sync-token", _DAV),
                ctag_present=_has_property(props, "getctag"),
                owner_metadata_present=_has_property(props, "owner", _DAV),
                privileges_present=privileges_present,
                read_privilege_present=read_privilege,
                write_privilege_present=write_privilege,
                supported_reports=_parse_reports(_property(props, "supported-report-set", _DAV)),
            )
        )
    return results


def _xml_responses(root: Any) -> list[Any]:
    return [child for child in list(root) if _local_name(child.tag) == "response" and _namespace(child.tag) == _DAV]


def _parse_vtodo_resources(payload: bytes, base_url: str, max_resources: int) -> list[_VTodoResource]:
    root = _safe_xml_root(payload)
    if root.tag != f"{{{_DAV}}}multistatus":
        raise ProviderPayloadError("provider_xml_invalid")
    results: list[_VTodoResource] = []
    seen_hrefs: set[str] = set()
    for response in _xml_responses(root):
        if len(results) >= max_resources:
            raise ProviderPayloadError("provider_vtodo_resource_limit")
        href = _direct_child_text(response, "href", namespace=_DAV)
        if not href:
            raise ProviderPayloadError("provider_vtodo_resource_href_missing")
        absolute_href = _trusted_resource_ref(urljoin(base_url, href), base_url)
        if absolute_href in seen_hrefs:
            raise ProviderPayloadError("provider_vtodo_resource_duplicate")
        seen_hrefs.add(absolute_href)
        direct_status = _http_status_code(_direct_child_text(response, "status", namespace=_DAV))
        etag: str | None = None
        calendar_data: bytes | None = None
        propstat_statuses: list[int] = []
        for propstat in _direct_children(response, "propstat", namespace=_DAV):
            status = _http_status_code(_direct_child_text(propstat, "status", namespace=_DAV))
            if status is not None:
                propstat_statuses.append(status)
            if status is not None and not 200 <= status < 300:
                continue
            prop = _direct_child(propstat, "prop", namespace=_DAV)
            if prop is None:
                continue
            data = _direct_child(prop, "calendar-data", namespace=_CALDAV)
            if data is not None and data.text:
                calendar_data = data.text.encode("utf-8")
            etag_element = _direct_child(prop, "getetag", namespace=_DAV)
            if etag_element is not None and etag_element.text:
                etag = etag_element.text.strip()
        status_code = direct_status or (propstat_statuses[0] if propstat_statuses else None)
        if status_code is not None and not 200 <= status_code < 300:
            raise ProviderFetchError("provider_vtodo_read_failed")
        if calendar_data is None:
            raise ProviderPayloadError("provider_vtodo_calendar_data_missing")
        results.append(_VTodoResource(absolute_href, etag, calendar_data))
    return results


def _value(component: Any, name: str) -> Any | None:
    property_value = component.get(name)
    if property_value is None:
        return None
    return getattr(property_value, "dt", property_value)


def _property_value(component: Any, name: str) -> Any | None:
    return component.get(name)


def _date_time_kind(property_value: Any) -> str:
    params = getattr(property_value, "params", {}) or {}
    if params.get("TZID"):
        return "TZID"
    value = getattr(property_value, "dt", None)
    if isinstance(value, datetime):
        if value.tzinfo is not None and value.utcoffset() is not None:
            return "UTC" if value.utcoffset().total_seconds() == 0 else "named-or-offset"
        return "floating"
    return "unknown"


def _due_observation(component: Any) -> dict[str, Any]:
    due_property = _property_value(component, "DUE")
    due_value = _value(component, "DUE")
    dtstart_property = _property_value(component, "DTSTART")
    dtstart_value = _value(component, "DTSTART")
    duration_value = _value(component, "DURATION")
    if due_property is not None and duration_value is not None:
        raise ProviderPayloadError("provider_vtodo_due_duration_conflict")
    if duration_value is not None and dtstart_value is None:
        raise ProviderPayloadError("provider_vtodo_duration_without_dtstart")
    if due_value is None:
        due_kind = "not_observed"
        timezone_kind = None
    elif isinstance(due_value, datetime):
        due_kind = "date-time"
        timezone_kind = _date_time_kind(due_property)
    elif isinstance(due_value, date):
        due_kind = "date"
        timezone_kind = None
    else:
        raise ProviderPayloadError("provider_vtodo_due_invalid")
    return {
        "kind": due_kind,
        "timezoneKind": timezone_kind,
        "dtstartObserved": dtstart_value is not None,
        "durationObserved": duration_value is not None,
    }


def _completion_observation(component: Any) -> dict[str, Any]:
    status_value = _value(component, "STATUS")
    status = str(status_value).strip().upper() if status_value is not None else None
    state = {
        "NEEDS-ACTION": "open",
        "IN-PROCESS": "open",
        "COMPLETED": "completed",
        "CANCELLED": "cancelled",
    }.get(status, "ambiguous" if status else "missing")
    percent_value = _value(component, "PERCENT-COMPLETE")
    percent_valid = True
    if percent_value is not None:
        try:
            percent_valid = 0 <= int(percent_value) <= 100
        except (TypeError, ValueError):
            percent_valid = False
    if not percent_valid:
        raise ProviderPayloadError("provider_vtodo_percent_complete_invalid")
    completed_observed = _property_value(component, "COMPLETED") is not None
    consistency = "observed"
    if status == "COMPLETED" and not completed_observed:
        consistency = "partial"
    elif status in {"NEEDS-ACTION", "IN-PROCESS"} and completed_observed:
        consistency = "ambiguous"
    elif status is None and (completed_observed or percent_value is not None):
        consistency = "ambiguous"
    return {
        "status": status,
        "state": state,
        "completedPropertyObserved": completed_observed,
        "percentCompleteObserved": percent_value is not None,
        "consistency": consistency,
    }


def _parse_vtodo_calendar_data(
    resource: _VTodoResource,
    collection_id: str,
) -> tuple[list[dict[str, Any]], set[str]]:
    try:
        calendar = Calendar.from_ical(resource.calendar_data)
    except Exception as error:
        raise ProviderPayloadError("provider_vtodo_calendar_data_invalid") from error
    component_names = {
        str(getattr(component, "name", "")).upper()
        for component in calendar.subcomponents
        if str(getattr(component, "name", "")).upper() != "VTIMEZONE"
    }
    if "VTODO" not in component_names:
        if component_names:
            raise ProviderPayloadError("provider_vtodo_component_mismatch")
        return [], component_names
    if component_names - {"VTODO"}:
        raise ProviderPayloadError("provider_vtodo_mixed_component_payload")
    items: list[dict[str, Any]] = []
    for component in calendar.subcomponents:
        if str(getattr(component, "name", "")).upper() != "VTODO":
            continue
        uid = _value(component, "UID")
        if not isinstance(uid, str) or not uid.strip():
            raise ProviderPayloadError("provider_vtodo_uid_missing")
        due = _due_observation(component)
        completion = _completion_observation(component)
        property_names = {str(name).upper() for name in component.keys()}
        recurrence_id_observed = "RECURRENCE-ID" in property_names
        items.append(
            {
                "_uid": uid.strip(),
                "itemId": _opaque("vtodo_item", f"{collection_id}|{resource.href}|{uid.strip()}"),
                "uidAvailable": True,
                "hrefAvailable": True,
                "etagAvailable": resource.etag is not None,
                "recurrenceIdObserved": recurrence_id_observed,
                "completion": completion,
                "due": due,
                "recurrence": {
                    "rruleObserved": "RRULE" in property_names,
                    "recurrenceIdObserved": recurrence_id_observed,
                    "exdateObserved": "EXDATE" in property_names,
                    "rdateObserved": "RDATE" in property_names,
                },
                "advanced": {
                    "priorityObserved": "PRIORITY" in property_names,
                    "notesObserved": "DESCRIPTION" in property_names,
                    "urlObserved": "URL" in property_names,
                    "locationObserved": "LOCATION" in property_names,
                    "alarmsObserved": any(
                        str(getattr(child, "name", "")).upper() == "VALARM"
                        for child in component.subcomponents
                    ),
                    "parentRelationshipObserved": "RELATED-TO" in property_names,
                    "flaggedOrTagsObserved": "CATEGORIES" in property_names,
                    "undocumentedXPropertiesObserved": any(
                        name.startswith("X-") for name in property_names
                    ),
                },
            }
        )
    return items, component_names


def _collection_output(collection: _Collection, query_status: str) -> dict[str, Any]:
    component_status = "observed" if collection.components is not None else "not_observed"
    return {
        "collectionId": collection.collection_id,
        "componentCapability": {
            "status": component_status,
            "advertisedComponents": sorted(collection.components or ()),
            "vTodoAdvertised": collection.is_vtodo_capable,
            "mixedVeventVtodo": bool(
                collection.components is not None
                and {"VEVENT", "VTODO"}.issubset(collection.components)
            ),
        },
        "hrefIdentity": {"available": True, "opaqueStableId": True},
        "displayNamePresent": collection.display_name_present,
        "resourceIdPresent": collection.resource_id_present,
        "freshness": {
            "collectionEtagPresent": collection.collection_etag_present,
            "syncTokenPresent": collection.sync_token_present,
            "ctagPresent": collection.ctag_present,
        },
        "supportedReports": list(collection.supported_reports),
        "privileges": {
            "metadataPresent": collection.privileges_present,
            "readPresent": collection.read_privilege_present,
            "writePresent": collection.write_privilege_present,
            "readOnly": (
                True
                if collection.read_privilege_present and not collection.write_privilege_present
                else False
                if collection.write_privilege_present
                else None
            ),
        },
        "sharedMetadataPresent": collection.owner_metadata_present,
        "queryStatus": query_status,
    }


class ICloudVTodoProbe:
    """Bounded, server-only VTODO capability probe using the trusted DAV transport."""

    def __init__(
        self,
        *,
        transport: ReadOnlyCalDavTransport,
        account_name: str,
        max_collections: int = _MAX_DEFAULT_COLLECTIONS,
        max_resources_per_collection: int = _MAX_DEFAULT_RESOURCES,
    ) -> None:
        account_name = account_name.strip()
        if not account_name:
            raise ValueError("iCloud account name is required")
        if not 1 <= max_collections <= 100 or not 1 <= max_resources_per_collection <= 1_000:
            raise ValueError("VTODO probe bounds are invalid")
        self.transport = transport
        self.account_id = _opaque("account", account_name.casefold())
        self.max_collections = max_collections
        self.max_resources_per_collection = max_resources_per_collection

    def _bootstrap_url(self) -> str:
        bootstrap_url = getattr(self.transport, "bootstrap_url", None)
        if not isinstance(bootstrap_url, str) or not bootstrap_url:
            return "https://fixture.invalid/.well-known/caldav"
        return bootstrap_url

    async def run(self) -> dict[str, Any]:
        result = _initial_result()
        bootstrap_url = self._bootstrap_url()
        try:
            principal_body = _propfind_body("<d:current-user-principal/>", {"d": _DAV})
            principal_response = await self.transport.propfind(
                bootstrap_url, body=principal_body, depth="0"
            )
            principal_href = _required_property_href(
                principal_response,
                property_name="current-user-principal",
                namespace=_DAV,
            )
            principal_url = _trusted_resource_ref(urljoin(bootstrap_url, principal_href), bootstrap_url)
            result["authentication"]["status"] = "supported"
            result["principalDiscovery"]["status"] = "supported"
        except Exception as error:
            result["authentication"]["status"] = (
                "failed" if isinstance(error, ProviderAuthError) else "not_observed"
            )
            result["principalDiscovery"]["status"] = "failed"
            _record_error(result, "principalDiscovery", error)
            result["calendarHome"]["status"] = "not_testable_safely"
            result["collectionDiscovery"]["status"] = "not_testable_safely"
            return result

        try:
            home_body = _propfind_body("<c:calendar-home-set/>", {"c": _CALDAV})
            home_response = await self.transport.propfind(
                principal_url, body=home_body, depth="0"
            )
            home_href = _required_property_href(
                home_response,
                property_name="calendar-home-set",
                namespace=_CALDAV,
            )
            home_url = _trusted_resource_ref(urljoin(principal_url, home_href), principal_url)
            result["calendarHome"]["status"] = "supported"
        except Exception as error:
            result["calendarHome"]["status"] = "failed"
            _record_error(result, "calendarHome", error)
            result["collectionDiscovery"]["status"] = "not_testable_safely"
            return result

        try:
            collection_response = await self.transport.propfind(
                home_url, body=_collection_propfind_body(), depth="1"
            )
            collections = _parse_collections(collection_response, home_url, self.account_id)
            if len(collections) > self.max_collections:
                raise ProviderPayloadError("provider_vtodo_collection_limit")
            result["collectionDiscovery"].update(
                {"status": "supported", "calendarCount": len(collections)}
            )
        except Exception as error:
            result["collectionDiscovery"]["status"] = "failed"
            _record_error(result, "collectionDiscovery", error)
            return result

        candidates = [collection for collection in collections if collection.is_vtodo_capable]
        result["identity"].update(
            {
                "collectionHrefAvailable": "supported" if collections else "not_observed",
                "collectionResourceIdObserved": any(
                    collection.resource_id_present for collection in collections
                ),
            }
        )
        result["vTodoCollections"].update(
            {
                "count": len(candidates),
                "explicitlyAdvertised": len(candidates),
                "stableIdentitiesAvailable": (
                    "supported" if candidates else "not_observed"
                ),
            }
        )
        result["freshness"].update(
            {
                "collectionEtagObserved": any(
                    collection.collection_etag_present for collection in collections
                ),
                "ctagObserved": any(collection.ctag_present for collection in collections),
                "syncTokenObserved": any(
                    collection.sync_token_present for collection in collections
                ),
            }
        )
        result["deletion"]["syncTokenObserved"] = result["freshness"]["syncTokenObserved"]
        result["sharedLists"].update(
            {
                "sharedMetadataObserved": any(
                    collection.owner_metadata_present for collection in collections
                ),
                "readPrivilegeObserved": any(
                    collection.read_privilege_present for collection in collections
                ),
                "readOnlyCollections": sum(
                    collection.read_privilege_present and not collection.write_privilege_present
                    for collection in collections
                ),
            }
        )

        query_statuses: dict[str, str] = {}
        item_observations: list[dict[str, Any]] = []
        uid_by_collection: Counter[tuple[str, str]] = Counter()
        resources_seen = 0
        now = datetime.now(timezone.utc)
        query_start = now - timedelta(days=30)
        query_end = now + timedelta(days=365)
        for collection in candidates:
            try:
                payload = await self.transport.report(
                    collection.href,
                    body=_vtodo_query_body(start=query_start, end=query_end),
                    depth="1",
                )
                resources = _parse_vtodo_resources(
                    payload,
                    collection.href,
                    self.max_resources_per_collection,
                )
                resources_seen += len(resources)
                for resource in resources:
                    items, _ = _parse_vtodo_calendar_data(resource, collection.collection_id)
                    for item in items:
                        item_observations.append(item)
                        uid_by_collection[(collection.collection_id, item["_uid"])] += 1
                query_statuses[collection.collection_id] = "supported"
            except Exception as error:
                query_statuses[collection.collection_id] = "failed"
                _record_error(result, "resourceRead", error)

        successful_queries = sum(status == "supported" for status in query_statuses.values())
        failed_queries = sum(status == "failed" for status in query_statuses.values())
        resource_read = result["resourceRead"]
        resource_read.update(
            {
                "status": (
                    "supported"
                    if successful_queries and not failed_queries
                    else "partial"
                    if successful_queries
                    else "failed"
                    if failed_queries
                    else "not_observed"
                ),
                "supported": (
                    True
                    if successful_queries and not failed_queries
                    else "ambiguous"
                    if successful_queries or failed_queries
                    else "not_observed"
                ),
                "collectionsQueried": len(query_statuses),
                "resourcesSeen": resources_seen,
                "itemsSeen": len(item_observations),
            }
        )
        duplicate_uid_count = sum(max(count - 1, 0) for count in uid_by_collection.values())
        for item in item_observations:
            item.pop("_uid", None)
        result["vTodoCollections"]["readable"] = (
            "supported"
            if successful_queries and not failed_queries
            else "partial"
            if successful_queries
            else "failed"
            if failed_queries
            else "not_observed"
        )
        result["collections"] = [
            _collection_output(
                collection,
                query_statuses.get(collection.collection_id, "not_tested"),
            )
            for collection in collections
        ]
        self._aggregate_observations(result, item_observations)
        result["identity"]["duplicateUidCount"] = duplicate_uid_count
        if duplicate_uid_count:
            result["identity"]["stableIdentity"] = "ambiguous"
        return result

    @staticmethod
    def _aggregate_observations(
        result: dict[str, Any], observations: list[dict[str, Any]]
    ) -> None:
        if not observations:
            return
        completion = result["completion"]
        statuses = {
            item["completion"]["status"]
            for item in observations
            if item["completion"]["status"] is not None
        }
        completion.update(
            {
                "statusValuesObserved": sorted(statuses),
                "openObserved": any(item["completion"]["state"] == "open" for item in observations),
                "completedObserved": any(
                    item["completion"]["state"] == "completed" for item in observations
                ),
                "cancelledObserved": any(
                    item["completion"]["state"] == "cancelled" for item in observations
                ),
                "statusMissingCount": sum(
                    item["completion"]["state"] == "missing" for item in observations
                ),
                "completedPropertyObserved": any(
                    item["completion"]["completedPropertyObserved"] for item in observations
                ),
                "percentCompleteObserved": any(
                    item["completion"]["percentCompleteObserved"] for item in observations
                ),
                "ambiguousCount": sum(
                    item["completion"]["state"] == "ambiguous"
                    or item["completion"]["consistency"] != "observed"
                    for item in observations
                ),
                "consistencyAmbiguousCount": sum(
                    item["completion"]["consistency"] != "observed"
                    for item in observations
                ),
            }
        )
        due = result["due"]
        due_kinds = {item["due"]["kind"] for item in observations}
        timezone_kinds = {
            item["due"]["timezoneKind"]
            for item in observations
            if item["due"]["timezoneKind"] is not None
        }
        due.update(
            {
                "dateObserved": "date" in due_kinds,
                "dateTimeObserved": "date-time" in due_kinds,
                "noDueObserved": "not_observed" in due_kinds,
                "timezoneKindsObserved": sorted(timezone_kinds),
                "dtstartObserved": any(item["due"]["dtstartObserved"] for item in observations),
                "durationObserved": any(item["due"]["durationObserved"] for item in observations),
            }
        )
        recurrence = result["recurrence"]
        recurrence.update(
            {
                "rruleObserved": any(item["recurrence"]["rruleObserved"] for item in observations),
                "recurrenceIdObserved": any(
                    item["recurrence"]["recurrenceIdObserved"] for item in observations
                ),
                "exdateObserved": any(item["recurrence"]["exdateObserved"] for item in observations),
                "rdateObserved": any(item["recurrence"]["rdateObserved"] for item in observations),
                "exceptions": (
                    "observed"
                    if any(item["recurrence"]["recurrenceIdObserved"] for item in observations)
                    else "not_observed"
                ),
            }
        )
        advanced = result["advancedProperties"]
        for key in advanced:
            advanced[key] = any(item["advanced"][key] for item in observations)
        identity = result["identity"]
        identity.update(
            {
                "collectionHrefAvailable": "supported",
                "itemUidAvailable": "supported",
                "itemHrefAvailable": "supported",
                "itemEtagAvailable": (
                    "supported" if all(item["etagAvailable"] for item in observations) else "partial"
                ),
                "recurrenceIdObserved": any(
                    item["recurrenceIdObserved"] for item in observations
                ),
                "stableIdentity": (
                    "supported"
                    if len({item["itemId"] for item in observations}) == len(observations)
                    else "ambiguous"
                ),
            }
        )
        identity["duplicateUidCount"] = 0
        result["freshness"]["itemEtagAvailable"] = identity["itemEtagAvailable"]
