from __future__ import annotations

import asyncio
import hashlib
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp
import recurring_ical_events
from icalendar import Calendar

from app.planning.models import validate_date, validate_timezone, validate_utc_timestamp
from app.planning.providers.contracts import (
    CalendarWindow,
    ExternalCalendar,
    ExternalCalendarEvent,
    ExternalCalendarProvider,
    ExternalProviderAccount,
    ProviderAdapterError,
    ProviderAuthError,
    ProviderFetchError,
    ProviderPayloadError,
    ProviderTimeoutError,
)


_DAV = "DAV:"
_CALDAV = "urn:ietf:params:xml:ns:caldav"
_MAX_TEXT = 4_000
_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6,8}$")


class ReadOnlyCalDavTransport(Protocol):
    """The transport surface intentionally has no PUT/DELETE/write operation."""

    async def propfind(self, url: str, *, body: bytes, depth: str) -> bytes: ...

    async def report(self, url: str, *, body: bytes, depth: str) -> bytes: ...


class AiohttpCalDavTransport:
    """Bounded HTTPS CalDAV transport restricted to discovery and reads."""

    _READ_METHODS = frozenset({"PROPFIND", "REPORT"})

    def __init__(
        self,
        *,
        bootstrap_url: str,
        username: str,
        password: str,
        connect_timeout_seconds: float = 10.0,
        read_timeout_seconds: float = 30.0,
        total_timeout_seconds: float = 45.0,
        max_redirects: int = 3,
        max_payload_bytes: int = 8 * 1024 * 1024,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        parsed = urlsplit(bootstrap_url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("iCloud CalDAV bootstrap URL must be HTTPS")
        if connect_timeout_seconds <= 0 or read_timeout_seconds <= 0 or total_timeout_seconds <= 0:
            raise ValueError("CalDAV timeouts must be positive")
        if max_redirects < 0 or max_payload_bytes <= 0:
            raise ValueError("CalDAV transport bounds are invalid")
        self.bootstrap_url = bootstrap_url
        self._bootstrap_host = parsed.hostname.lower().rstrip(".")
        self._auth = aiohttp.BasicAuth(username, password)
        self._timeout = aiohttp.ClientTimeout(
            total=total_timeout_seconds,
            connect=connect_timeout_seconds,
            sock_read=read_timeout_seconds,
        )
        self._max_redirects = max_redirects
        self._max_payload_bytes = max_payload_bytes
        self._session = session
        self._owns_session = session is None

    async def propfind(self, url: str, *, body: bytes, depth: str) -> bytes:
        return await self._request("PROPFIND", url, body=body, depth=depth)

    async def report(self, url: str, *, body: bytes, depth: str) -> bytes:
        return await self._request("REPORT", url, body=body, depth=depth)

    async def _request(self, method: str, url: str, *, body: bytes, depth: str) -> bytes:
        if method not in self._READ_METHODS:
            raise ProviderFetchError("unsupported_provider_method")
        current_url = self._trusted_url(url)
        session = self._session
        if session is None:
            session = aiohttp.ClientSession(timeout=self._timeout, raise_for_status=False)
            self._session = session
        for _ in range(self._max_redirects + 1):
            try:
                async with session.request(
                    method,
                    current_url,
                    data=body,
                    headers={
                        "Content-Type": "application/xml; charset=utf-8",
                        "Depth": depth,
                        "Accept": "application/xml, text/xml, text/calendar",
                    },
                    auth=self._auth,
                    allow_redirects=False,
                ) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location")
                        if not location:
                            raise ProviderFetchError("provider_redirect_invalid")
                        current_url = self._trusted_url(urljoin(current_url, location))
                        continue
                    if response.status in {401, 403}:
                        raise ProviderAuthError("provider_authentication_failed")
                    if response.status < 200 or response.status >= 300:
                        raise ProviderFetchError("provider_read_failed")
                    return await self._read_bounded(response)
            except ProviderAdapterError:
                raise
            except asyncio.TimeoutError as exc:
                raise ProviderTimeoutError("provider_timeout") from exc
            except aiohttp.ClientError as exc:
                raise ProviderFetchError("provider_transport_failed") from exc
        raise ProviderFetchError("provider_redirect_limit")

    async def _read_bounded(self, response: aiohttp.ClientResponse) -> bytes:
        content_length = response.headers.get("Content-Length")
        if content_length:
            try:
                if int(content_length) > self._max_payload_bytes:
                    raise ProviderPayloadError("provider_payload_too_large")
            except ValueError as exc:
                raise ProviderPayloadError("provider_payload_invalid") from exc
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.content.iter_chunked(64 * 1024):
            size += len(chunk)
            if size > self._max_payload_bytes:
                raise ProviderPayloadError("provider_payload_too_large")
            chunks.append(chunk)
        return b"".join(chunks)

    def _trusted_url(self, url: str) -> str:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme != "https" or not host:
            raise ProviderFetchError("provider_redirect_untrusted")
        if parsed.username or parsed.password:
            raise ProviderFetchError("provider_redirect_untrusted")
        # Apple discovery may move between iCloud and Apple-owned hosts. No
        # arbitrary user-controlled host or browser URL is accepted.
        if not (
            host == self._bootstrap_host
            or host.endswith(".icloud.com")
            or host.endswith(".apple.com")
        ):
            raise ProviderFetchError("provider_redirect_untrusted")
        return url

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None


@dataclass
class _DiscoveredState:
    account: ExternalProviderAccount
    principal_url: str
    calendar_home_url: str
    calendars: list[ExternalCalendar]


class ICloudCalDavProvider(ExternalCalendarProvider):
    """iCloud/CalDAV adapter exposing discovery and event reads only."""

    provider = "icloud"

    @staticmethod
    def account_id_for(account_name: str) -> str:
        account_name = account_name.strip()
        if not account_name:
            raise ValueError("iCloud account name is required")
        return _opaque("account", account_name.casefold())

    def __init__(
        self,
        *,
        transport: ReadOnlyCalDavTransport,
        account_name: str,
        default_timezone: str = "Europe/Moscow",
        max_calendars: int = 32,
        max_events_per_calendar: int = 5_000,
    ) -> None:
        account_name = account_name.strip()
        if not account_name:
            raise ValueError("iCloud account name is required")
        validate_timezone(default_timezone, "icloud.default_timezone")
        if not 1 <= max_calendars <= 100 or not 1 <= max_events_per_calendar <= 50_000:
            raise ValueError("iCloud provider bounds are invalid")
        self.transport = transport
        self._account_name = account_name
        self.default_timezone = default_timezone
        self.max_calendars = max_calendars
        self.max_events_per_calendar = max_events_per_calendar
        self._state: _DiscoveredState | None = None

    async def discover_account(self) -> ExternalProviderAccount:
        account_id = self.account_id_for(self._account_name)
        account = ExternalProviderAccount(provider=self.provider, account_id=account_id, display_label="iCloud")
        principal_url, home_url = await self._discover_principal_and_home()
        self._state = _DiscoveredState(account, principal_url, home_url, [])
        return account

    async def list_calendars(self) -> list[ExternalCalendar]:
        if self._state is None:
            await self.discover_account()
        assert self._state is not None
        body = _propfind_body(
            "<d:resourcetype/><d:displayname/><x:calendar-color/>",
            {"d": _DAV, "x": "http://apple.com/ns/ical/"},
        )
        response = await self.transport.propfind(self._state.calendar_home_url, body=body, depth="1")
        calendars = self._parse_calendars(response, self._state.calendar_home_url)
        if len(calendars) > self.max_calendars:
            raise ProviderPayloadError("provider_calendar_limit")
        self._state.calendars = calendars
        return list(calendars)

    async def fetch_events(
        self,
        calendar: ExternalCalendar,
        window: CalendarWindow,
    ) -> list[ExternalCalendarEvent]:
        window.validate()
        if self._state is None:
            await self.discover_account()
        assert self._state is not None
        body = _calendar_query_body(window)
        response = await self.transport.report(calendar.fetch_ref, body=body, depth="1")
        ical_payloads = self._parse_calendar_data(response)
        results: list[ExternalCalendarEvent] = []
        for payload in ical_payloads:
            try:
                calendar_data = Calendar.from_ical(payload)
                occurrences = recurring_ical_events.of(calendar_data).between(window.start, window.end)
                for component in occurrences:
                    if len(results) >= self.max_events_per_calendar:
                        raise ProviderPayloadError("provider_event_limit")
                    if str(component.get("STATUS", "")).upper() == "CANCELLED":
                        continue
                    results.append(self._normalize_event(calendar, calendar_data, component))
            except ProviderAdapterError:
                raise
            except Exception as exc:
                raise ProviderPayloadError("provider_calendar_data_invalid") from exc
        return _deduplicate_occurrences(results)

    async def _discover_principal_and_home(self) -> tuple[str, str]:
        bootstrap_url = getattr(self.transport, "bootstrap_url", None)
        if not isinstance(bootstrap_url, str) or not bootstrap_url:
            # Fixture transports can expose a logical bootstrap endpoint without
            # a public URL, but still must return standards-based hrefs.
            bootstrap_url = "https://fixture.invalid/.well-known/caldav"
        principal_body = _propfind_body("<d:current-user-principal/>", {"d": _DAV})
        principal_response = await self.transport.propfind(bootstrap_url, body=principal_body, depth="0")
        principal_href = _required_href(principal_response, "current-user-principal")
        principal_url = urljoin(bootstrap_url, principal_href)
        home_body = _propfind_body("<x:calendar-home-set/>", {"x": _CALDAV})
        home_response = await self.transport.propfind(principal_url, body=home_body, depth="0")
        home_href = _required_href(home_response, "calendar-home-set")
        return principal_url, urljoin(principal_url, home_href)

    def _parse_calendars(self, payload: bytes, base_url: str) -> list[ExternalCalendar]:
        root = _xml_root(payload)
        results: list[ExternalCalendar] = []
        for response in _xml_children(root, "response"):
            href = _first_text(response, "href")
            if not href:
                continue
            resource_types = {element.tag.rsplit("}", 1)[-1] for element in response.iter()}
            if "calendar" not in resource_types:
                continue
            absolute_href = urljoin(base_url, href)
            display_name = (_first_text(response, "displayname") or "Calendar")[:200]
            color = _first_text(response, "calendar-color")
            color = color if color and _COLOR_RE.fullmatch(color) else None
            results.append(
                ExternalCalendar(
                    provider_calendar_id=_opaque(
                        "calendar",
                        f"{self._state.account.account_id if self._state else 'unknown'}|{absolute_href}",
                    ),
                    display_name=display_name,
                    color=color,
                    enabled=True,
                    fetch_ref=absolute_href,
                )
            )
        return results

    def _parse_calendar_data(self, payload: bytes) -> list[bytes]:
        root = _xml_root(payload)
        results = [
            (element.text or "").encode("utf-8")
            for element in root.iter()
            if element.tag.rsplit("}", 1)[-1] == "calendar-data" and element.text
        ]
        if not results:
            raise ProviderPayloadError("provider_calendar_data_missing")
        return results

    def _normalize_event(
        self,
        calendar: ExternalCalendar,
        source_calendar: Calendar,
        component: object,
    ) -> ExternalCalendarEvent:
        uid_value = _component_value(component, "UID")
        if not isinstance(uid_value, str) or not uid_value.strip():
            raise ProviderPayloadError("provider_event_uid_missing")
        uid = uid_value.strip()
        start_property = _component_property(component, "DTSTART")
        start_value = getattr(start_property, "dt", None)
        if start_value is None:
            raise ProviderPayloadError("provider_event_start_missing")
        timezone_name = _semantic_timezone(
            start_property,
            source_calendar,
            self.default_timezone,
        )
        recurrence_property = _component_property(component, "RECURRENCE-ID")
        if not _uid_has_recurrence(source_calendar, uid):
            recurrence_key = "base"
        else:
            recurrence_key = _occurrence_key(recurrence_property, start_value, timezone_name)
        title = _bounded_private_text(_component_value(component, "SUMMARY"), "Untitled event", 500)
        notes = _optional_private_text(_component_value(component, "DESCRIPTION"), 4000)
        location = _optional_private_text(_component_value(component, "LOCATION"), 1000)
        if isinstance(start_value, date) and not isinstance(start_value, datetime):
            end_property = _component_property(component, "DTEND")
            end_value = getattr(end_property, "dt", None) if end_property is not None else None
            if end_value is None:
                end_value = start_value + timedelta(days=1)
            if not isinstance(end_value, date) or isinstance(end_value, datetime):
                raise ProviderPayloadError("provider_all_day_end_invalid")
            return ExternalCalendarEvent(
                provider_calendar_id=calendar.provider_calendar_id,
                provider_event_id=_opaque("event", f"{calendar.provider_calendar_id}|{uid}|{recurrence_key}"),
                recurrence_instance_key=recurrence_key,
                title=title,
                notes=notes,
                location=location,
                all_day=True,
                timezone=timezone_name,
                start_at_utc=None,
                end_at_utc=None,
                start_date=validate_date(start_value.isoformat(), "provider.start_date"),
                end_date_exclusive=validate_date(end_value.isoformat(), "provider.end_date_exclusive"),
            )
        if not isinstance(start_value, datetime):
            raise ProviderPayloadError("provider_event_start_invalid")
        end_property = _component_property(component, "DTEND")
        end_value = getattr(end_property, "dt", None) if end_property is not None else None
        if end_value is None:
            duration = _component_value(component, "DURATION")
            if isinstance(duration, timedelta):
                end_value = start_value + duration
        if not isinstance(end_value, datetime):
            raise ProviderPayloadError("provider_timed_end_missing")
        start_utc = _as_utc(start_value, timezone_name)
        end_utc = _as_utc(end_value, timezone_name)
        if end_utc <= start_utc:
            raise ProviderPayloadError("provider_timed_end_invalid")
        return ExternalCalendarEvent(
            provider_calendar_id=calendar.provider_calendar_id,
            provider_event_id=_opaque("event", f"{calendar.provider_calendar_id}|{uid}|{recurrence_key}"),
            recurrence_instance_key=recurrence_key,
            title=title,
            notes=notes,
            location=location,
            all_day=False,
            timezone=timezone_name,
            start_at_utc=_timestamp(start_utc),
            end_at_utc=_timestamp(end_utc),
            start_date=None,
            end_date_exclusive=None,
        )

    async def close(self) -> None:
        close = getattr(self.transport, "close", None)
        if callable(close):
            await close()


def _opaque(kind: str, value: str) -> str:
    return f"icloud_{kind}_{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _as_utc(value: datetime, timezone_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=ZoneInfo(timezone_name))
    return value.astimezone(timezone.utc)


def _semantic_timezone(property_value: object, source_calendar: Calendar, default_timezone: str) -> str:
    params = getattr(property_value, "params", {}) or {}
    candidate = params.get("TZID")
    if candidate:
        candidate = str(candidate)
    if not candidate:
        value = getattr(property_value, "dt", None)
        tzinfo = getattr(value, "tzinfo", None)
        candidate = getattr(tzinfo, "key", None)
    if not candidate:
        candidate_value = source_calendar.get("X-WR-TIMEZONE")
        candidate = str(candidate_value) if candidate_value else default_timezone
    try:
        ZoneInfo(candidate)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ProviderPayloadError("provider_timezone_invalid") from exc
    return validate_timezone(candidate, "provider.timezone")


def _occurrence_key(property_value: object | None, start_value: object, timezone_name: str) -> str:
    value = getattr(property_value, "dt", None) if property_value is not None else None
    if value is None:
        value = start_value
    if isinstance(value, datetime):
        return _as_utc(value, timezone_name).isoformat(timespec="seconds").replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    raise ProviderPayloadError("provider_recurrence_id_invalid")


def _uid_has_recurrence(source_calendar: Calendar, uid: str) -> bool:
    """Account for recurrence libraries adding RECURRENCE-ID to plain events."""

    for component in source_calendar.subcomponents:
        if str(_component_value(component, "UID") or "").strip() != uid:
            continue
        if _component_property(component, "RRULE") is not None or _component_property(component, "RDATE") is not None:
            return True
    return False


def _component_property(component: object, name: str) -> object | None:
    try:
        return component.get(name)  # type: ignore[union-attr]
    except (AttributeError, KeyError):
        return None


def _component_value(component: object, name: str) -> object | None:
    prop = _component_property(component, name)
    if prop is None:
        return None
    return getattr(prop, "dt", prop)


def _bounded_private_text(value: object, default: str, max_length: int) -> str:
    if value is None:
        return default
    result = str(value)
    if not result:
        return default
    if len(result) > max_length:
        raise ProviderPayloadError("provider_private_field_too_large")
    return result


def _optional_private_text(value: object, max_length: int) -> str | None:
    if value is None:
        return None
    result = str(value)
    if len(result) > max_length:
        raise ProviderPayloadError("provider_private_field_too_large")
    return result or None


def _deduplicate_occurrences(events: list[ExternalCalendarEvent]) -> list[ExternalCalendarEvent]:
    selected: dict[str, ExternalCalendarEvent] = {}
    for event in events:
        selected[event.provider_event_id] = event
    return list(selected.values())


def _xml_root(payload: bytes) -> ET.Element:
    if not payload or len(payload) > 8 * 1024 * 1024:
        raise ProviderPayloadError("provider_payload_too_large")
    try:
        return ET.fromstring(payload)
    except ET.ParseError as exc:
        raise ProviderPayloadError("provider_xml_invalid") from exc


def _xml_children(root: ET.Element, local_name: str) -> list[ET.Element]:
    return [element for element in root.iter() if element.tag.rsplit("}", 1)[-1] == local_name]


def _first_text(root: ET.Element, local_name: str) -> str | None:
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] == local_name and element.text:
            return element.text.strip()
    return None


def _required_href(payload: bytes, property_name: str) -> str:
    root = _xml_root(payload)
    href = _first_text(root, "href")
    if not href:
        raise ProviderPayloadError(f"provider_{property_name.replace('-', '_')}_missing")
    return href


def _propfind_body(properties: str, namespaces: dict[str, str]) -> bytes:
    attrs = " ".join(f'xmlns:{prefix}="{uri}"' for prefix, uri in namespaces.items())
    return f"<?xml version=\"1.0\" encoding=\"utf-8\"?><d:propfind {attrs}><d:prop>{properties}</d:prop></d:propfind>".encode()


def _calendar_query_body(window: CalendarWindow) -> bytes:
    start = window.start.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    end = window.end.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
        '<d:prop><d:getetag/><c:calendar-data/></d:prop>'
        '<c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT">'
        f'<c:time-range start="{start}" end="{end}"/>'
        '</c:comp-filter></c:comp-filter></c:filter></c:calendar-query>'
    ).encode()
