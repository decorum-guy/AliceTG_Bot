from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from app.planning.api.auth import AuthenticatedPlanningContext
from app.planning.models import validate_utc_timestamp, utc_now


def _normalise_now(value: str | datetime) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Planning API clock must be timezone-aware")
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    validate_utc_timestamp(value, "planning.api.now")
    return value


def _as_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value[:-1] + "+00:00")


def _format_timestamp(value: datetime) -> str:
    timespec = "microseconds" if value.microsecond else "seconds"
    return value.astimezone(timezone.utc).isoformat(timespec=timespec).replace("+00:00", "Z")


@dataclass(frozen=True)
class FreshnessEnvelopeBuilder:
    now_fn: Callable[[], str | datetime] = utc_now
    stale_after_seconds: int = 300

    def __post_init__(self) -> None:
        if self.stale_after_seconds <= 0:
            raise ValueError("Planning freshness interval must be positive")

    def now(self) -> str:
        return _normalise_now(self.now_fn())

    def freshness(self, *, now: str | None = None) -> dict[str, Any]:
        selected = now or self.now()
        stale_after = _format_timestamp(
            _as_datetime(selected) + timedelta(seconds=self.stale_after_seconds)
        )
        return {
            "sourceStatus": "current",
            "lastSyncedAt": selected,
            "staleAfter": stale_after,
        }

    def object_response(
        self,
        *,
        domain: str,
        object_value: Mapping[str, Any],
        correlation_id: str,
    ) -> dict[str, Any]:
        response = {
            "schemaVersion": "planning.v1",
            "kind": "object",
            "domain": domain,
            "object": dict(object_value),
            **self.freshness(),
            "correlation_id": correlation_id,
        }
        return response

    def list_response(
        self,
        *,
        domain: str,
        items: list[Mapping[str, Any]],
        correlation_id: str,
        limit: int,
        offset: int,
        has_more: bool,
    ) -> dict[str, Any]:
        response = {
            "schemaVersion": "planning.v1",
            "kind": "list",
            "domain": domain,
            "items": [dict(item) for item in items],
            "generatedAt": self.now(),
            **self.freshness(),
            "pagination": {
                "limit": limit,
                "offset": offset,
                "count": len(items),
                "has_more": has_more,
                "next_offset": offset + len(items) if has_more else None,
            },
            "correlation_id": correlation_id,
        }
        return response

    def status_response(
        self,
        *,
        capabilities: Mapping[str, list[str]],
        capability_metadata: Mapping[str, Mapping[str, Any]] | None = None,
        storage_status: str,
        correlation_id: str,
    ) -> dict[str, Any]:
        response: dict[str, Any] = {
            "schemaVersion": "planning.v1",
            "kind": "status",
            "apiVersion": "v1",
            "capabilities": {key: list(value) for key, value in capabilities.items()},
            "storageStatus": storage_status,
            **self.freshness(),
            "correlation_id": correlation_id,
        }
        if capability_metadata is not None:
            response["capabilityMetadata"] = {
                key: dict(value) for key, value in capability_metadata.items()
            }
        return response

    def error_response(
        self,
        *,
        status: int,
        code: str,
        message: str,
        details: Mapping[str, Any] | None,
        retryable: bool,
        correlation_id: str,
        actor: Mapping[str, str] | None,
    ) -> dict[str, Any]:
        selected_actor = actor or {"id": "anonymous", "type": "service", "surface": "system"}
        return {
            "schemaVersion": "planning.v1",
            "kind": "error",
            "http_status": status,
            "error": {
                "code": code,
                "message": message,
                "details": dict(details or {}),
                "retryable": retryable,
            },
            **self.freshness(),
            "actor": dict(selected_actor),
            "correlation_id": correlation_id,
        }
