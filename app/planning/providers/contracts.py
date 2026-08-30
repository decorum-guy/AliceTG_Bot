from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Literal, Protocol


ProviderStatus = Literal["current", "stale", "error", "not_configured", "disabled"]


class ProviderFailureCode(str, Enum):
    """Closed, browser-safe provider failure vocabulary.

    Values are deliberately semantic rather than a URL, response, host, or
    exception rendering.  Provider adapters may retain a private exception
    cause for server-side debugging, but only these values cross into cache or
    status state.
    """

    ERROR = "provider_error"
    TIMEOUT = "provider_timeout"
    AUTHENTICATION_FAILED = "provider_authentication_failed"
    FETCH_FAILED = "provider_fetch_failed"
    PAYLOAD_INVALID = "provider_payload_invalid"
    IDENTITY_MISMATCH = "provider_identity_mismatch"
    REFRESH_FAILED = "provider_refresh_failed"
    CALENDAR_LIMIT = "provider_calendar_limit"
    CALENDAR_DISAPPEARED = "provider_calendar_disappeared"

    # iCloud HTTP and transport attempt categories.
    RATE_LIMITED = "provider_rate_limited"
    SERVER_FAILURE = "provider_server_failure"
    READ_FAILED = "provider_read_failed"
    CONNECTION_TIMEOUT = "provider_connection_timeout"
    READ_TIMEOUT = "provider_read_timeout"
    DNS_FAILED = "provider_dns_failed"
    CONNECTION_REFUSED = "provider_connection_refused"
    CONNECTION_FAILED = "provider_connection_failed"
    TLS_FAILED = "provider_tls_failed"
    CONNECTION_RESET = "provider_connection_reset"
    CONNECTION_ABORTED = "provider_connection_aborted"
    SERVER_DISCONNECTED = "provider_server_disconnected"
    TRANSPORT_UNKNOWN = "provider_transport_unknown"

    # iCloud redirect and payload/protocol categories.
    METHOD_NOT_ALLOWED = "provider_method_not_allowed"
    REDIRECT_INVALID = "provider_redirect_invalid"
    REDIRECT_UNTRUSTED = "provider_redirect_untrusted"
    REDIRECT_LIMIT = "provider_redirect_limit"
    PAYLOAD_TOO_LARGE = "provider_payload_too_large"
    CALENDAR_DATA_INVALID = "provider_calendar_data_invalid"
    CALENDAR_DATA_MISSING = "provider_calendar_data_missing"
    EVENT_LIMIT = "provider_event_limit"
    EVENT_RESOURCE_MISSING = "provider_event_resource_missing"
    EVENT_UID_MISSING = "provider_event_uid_missing"
    EVENT_START_MISSING = "provider_event_start_missing"
    EVENT_START_INVALID = "provider_event_start_invalid"
    ALL_DAY_END_INVALID = "provider_all_day_end_invalid"
    TIMED_END_MISSING = "provider_timed_end_missing"
    TIMED_END_INVALID = "provider_timed_end_invalid"
    RECURRENCE_INVALID = "provider_recurrence_invalid"
    RECURRENCE_ID_INVALID = "provider_recurrence_id_invalid"
    TIMEZONE_INVALID = "provider_timezone_invalid"
    PRIVATE_FIELD_TOO_LARGE = "provider_private_field_too_large"
    XML_INVALID = "provider_xml_invalid"
    PROPSTAT_INVALID = "provider_propstat_invalid"
    CURRENT_USER_PRINCIPAL_MISSING = "provider_current_user_principal_missing"
    CURRENT_USER_PRINCIPAL_AMBIGUOUS = "provider_current_user_principal_ambiguous"
    CALENDAR_HOME_SET_MISSING = "provider_calendar_home_set_missing"
    CALENDAR_HOME_SET_AMBIGUOUS = "provider_calendar_home_set_ambiguous"
    RESOURCE_REF_TOO_LARGE = "provider_resource_ref_too_large"
    RESOURCE_REF_UNTRUSTED = "provider_resource_ref_untrusted"
    RESOURCE_STATUS_CONFLICT = "provider_resource_status_conflict"
    RESOURCE_VERIFICATION_DUPLICATE = "provider_resource_verification_duplicate"
    RESOURCE_VERIFICATION_FAILED = "provider_resource_verification_failed"
    RESOURCE_VERIFICATION_INCOMPLETE = "provider_resource_verification_incomplete"
    RESOURCE_VERIFICATION_LIMIT = "provider_resource_verification_limit"

    # Existing bounded iCloud VTODO-probe categories are retained so the
    # shared provider exception boundary cannot widen them dynamically.
    COLLECTION_DUPLICATE = "provider_collection_duplicate"
    COLLECTION_HREF_MISSING = "provider_collection_href_missing"
    PROBE_FAILED = "provider_probe_failed"
    VTODO_CALENDAR_DATA_INVALID = "provider_vtodo_calendar_data_invalid"
    VTODO_CALENDAR_DATA_MISSING = "provider_vtodo_calendar_data_missing"
    VTODO_COLLECTION_LIMIT = "provider_vtodo_collection_limit"
    VTODO_COMPONENT_MISMATCH = "provider_vtodo_component_mismatch"
    VTODO_COMPONENT_SET_INVALID = "provider_vtodo_component_set_invalid"
    VTODO_DUE_DURATION_CONFLICT = "provider_vtodo_due_duration_conflict"
    VTODO_DUE_INVALID = "provider_vtodo_due_invalid"
    VTODO_DURATION_WITHOUT_DTSTART = "provider_vtodo_duration_without_dtstart"
    VTODO_MIXED_COMPONENT_PAYLOAD = "provider_vtodo_mixed_component_payload"
    VTODO_PERCENT_COMPLETE_INVALID = "provider_vtodo_percent_complete_invalid"
    VTODO_READ_FAILED = "provider_vtodo_read_failed"
    VTODO_RESOURCE_DUPLICATE = "provider_vtodo_resource_duplicate"
    VTODO_RESOURCE_HREF_MISSING = "provider_vtodo_resource_href_missing"
    VTODO_RESOURCE_LIMIT = "provider_vtodo_resource_limit"
    VTODO_UID_MISSING = "provider_vtodo_uid_missing"


def _safe_failure_code(
    detail: ProviderFailureCode | str | None,
    fallback: ProviderFailureCode,
) -> str:
    """Return a fixed code, never a caller-controlled diagnostic string."""

    if isinstance(detail, ProviderFailureCode):
        return detail.value
    if isinstance(detail, str):
        try:
            return ProviderFailureCode(detail).value
        except ValueError:
            pass
    return fallback.value


class ProviderAdapterError(RuntimeError):
    """Sanitized, stable provider failure; private provider content is excluded."""

    code = ProviderFailureCode.ERROR

    def __init__(self, detail: ProviderFailureCode | str | None = None) -> None:
        selected = _safe_failure_code(detail, type(self).code)
        self.code = selected
        super().__init__(selected)


class ProviderTimeoutError(ProviderAdapterError):
    code = ProviderFailureCode.TIMEOUT


class ProviderAuthError(ProviderAdapterError):
    code = ProviderFailureCode.AUTHENTICATION_FAILED


class ProviderFetchError(ProviderAdapterError):
    code = ProviderFailureCode.FETCH_FAILED


class ProviderPayloadError(ProviderAdapterError):
    code = ProviderFailureCode.PAYLOAD_INVALID


@dataclass(frozen=True)
class CalendarWindow:
    """A finite UTC range. Provider adapters never accept an unbounded range."""

    start: datetime
    end: datetime

    def validate(self) -> "CalendarWindow":
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("calendar window must be timezone-aware")
        if self.end <= self.start:
            raise ValueError("calendar window end must be later than start")
        return self


@dataclass(frozen=True)
class ExternalProviderAccount:
    provider: str
    account_id: str
    display_label: str


@dataclass(frozen=True)
class ExternalCalendar:
    provider_calendar_id: str
    display_name: str
    color: str | None
    enabled: bool
    # This is an adapter-internal fetch reference. It is never serialized or persisted.
    fetch_ref: str


@dataclass(frozen=True)
class ExternalCalendarEvent:
    provider_calendar_id: str
    provider_event_id: str
    recurrence_instance_key: str
    title: str
    notes: str | None
    location: str | None
    all_day: bool
    timezone: str
    start_at_utc: str | None
    end_at_utc: str | None
    start_date: str | None
    end_date_exclusive: str | None
    # Provider-internal resource identity used only by a read-only verifier.
    # It is never copied into CalendarEvent.source_ref or API envelopes.
    resource_ref: str | None = None


@dataclass(frozen=True)
class ExternalResourceVerification:
    """Conclusive read-only status for one provider resource reference."""

    resource_ref: str
    status: Literal["present", "missing"]
    events: tuple[ExternalCalendarEvent, ...] = ()


class ExternalCalendarProvider(Protocol):
    """Read-only provider contract. Deliberately contains no mutation methods."""

    async def discover_account(self) -> ExternalProviderAccount: ...

    async def list_calendars(self) -> list[ExternalCalendar]: ...

    async def fetch_events(
        self,
        calendar: ExternalCalendar,
        window: CalendarWindow,
    ) -> list[ExternalCalendarEvent]: ...


class ExternalCalendarResourceVerifier(Protocol):
    """Optional read-only capability for authoritative missing-resource checks."""

    async def verify_resources(
        self,
        calendar: ExternalCalendar,
        resource_refs: list[str],
        window: CalendarWindow,
    ) -> list[ExternalResourceVerification]: ...
