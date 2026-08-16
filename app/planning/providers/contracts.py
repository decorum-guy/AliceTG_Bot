from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol


ProviderStatus = Literal["current", "stale", "error", "not_configured", "disabled"]


class ProviderAdapterError(RuntimeError):
    """Sanitized, stable provider failure; private provider content is excluded."""

    code = "provider_error"

    def __init__(self, detail: str | None = None) -> None:
        selected = detail if isinstance(detail, str) and detail.startswith("provider_") else type(self).code
        self.code = selected
        super().__init__(selected)


class ProviderTimeoutError(ProviderAdapterError):
    code = "provider_timeout"


class ProviderAuthError(ProviderAdapterError):
    code = "provider_authentication_failed"


class ProviderFetchError(ProviderAdapterError):
    code = "provider_fetch_failed"


class ProviderPayloadError(ProviderAdapterError):
    code = "provider_payload_invalid"


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
