"""Read-only external calendar provider adapters for Planning."""

from app.planning.providers.cache import ProviderCalendarCache, ProviderRefreshResult
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
from app.planning.providers.icloud import AiohttpCalDavTransport, ICloudCalDavProvider
from app.planning.providers.sync import ICloudCalendarRefreshLoop

__all__ = [
    "CalendarWindow",
    "AiohttpCalDavTransport",
    "ExternalCalendar",
    "ExternalCalendarEvent",
    "ExternalCalendarProvider",
    "ExternalProviderAccount",
    "ICloudCalDavProvider",
    "ICloudCalendarRefreshLoop",
    "ProviderAdapterError",
    "ProviderAuthError",
    "ProviderCalendarCache",
    "ProviderFetchError",
    "ProviderPayloadError",
    "ProviderRefreshResult",
    "ProviderTimeoutError",
]
