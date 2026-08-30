"""Read-only external calendar provider adapters for Planning."""

from app.planning.providers.cache import ProviderCalendarCache, ProviderRefreshResult
from app.planning.providers.contracts import (
    CalendarWindow,
    ExternalCalendar,
    ExternalCalendarEvent,
    ExternalCalendarProvider,
    ExternalCalendarResourceVerifier,
    ExternalProviderAccount,
    ExternalResourceVerification,
    ProviderAdapterError,
    ProviderAuthError,
    ProviderFetchError,
    ProviderPayloadError,
    ProviderTimeoutError,
)
from app.planning.providers.icloud import AiohttpCalDavTransport, ICloudCalDavProvider
from app.planning.providers.sync import ICloudCalendarRefreshLoop, provider_stale_after_seconds

__all__ = [
    "CalendarWindow",
    "AiohttpCalDavTransport",
    "ExternalCalendar",
    "ExternalCalendarEvent",
    "ExternalCalendarProvider",
    "ExternalCalendarResourceVerifier",
    "ExternalProviderAccount",
    "ExternalResourceVerification",
    "ICloudCalDavProvider",
    "ICloudCalendarRefreshLoop",
    "ProviderAdapterError",
    "ProviderAuthError",
    "ProviderCalendarCache",
    "ProviderFetchError",
    "ProviderPayloadError",
    "ProviderRefreshResult",
    "ProviderTimeoutError",
    "provider_stale_after_seconds",
]
