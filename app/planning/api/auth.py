from __future__ import annotations

import hmac
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Callable, Mapping

from aiohttp import web

from app.planning.api.errors import PlanningApiError
from app.planning.models import MutationContext, validate_text


PLANNING_AUDIENCE_HEADER = "X-Planning-Audience"
PLANNING_SECRET_HEADER = "X-Planning-Secret"
INTERNAL_SECRET_HEADER = "X-Internal-Secret"
_AUDIENCE_VALUES = frozenset({"ha", "panel-agent", "operator"})

# Route keys are deliberately explicit.  Adding a new HTTP route requires
# adding it here, so authentication and authorization cannot drift apart.
ROUTE_PERMISSIONS: dict[str, frozenset[str]] = {
    "GET /reminders": frozenset({"ha", "panel-agent", "operator"}),
    "POST /reminders": frozenset({"panel-agent", "operator"}),
    "PATCH /reminders/{id}": frozenset({"panel-agent", "operator"}),
    "POST /reminders/{id}/complete": frozenset({"panel-agent", "operator"}),
    "POST /reminders/{id}/cancel": frozenset({"panel-agent", "operator"}),
    "GET /tasks": frozenset({"ha", "panel-agent", "operator"}),
    "GET /tasks/{id}": frozenset({"panel-agent", "operator"}),
    "POST /tasks": frozenset({"panel-agent", "operator"}),
    "PATCH /tasks/{id}": frozenset({"panel-agent", "operator"}),
    "POST /tasks/{id}/complete": frozenset({"panel-agent", "operator"}),
    "DELETE /tasks/{id}": frozenset({"panel-agent", "operator"}),
    "GET /events": frozenset({"ha", "panel-agent", "operator"}),
    "POST /events": frozenset({"panel-agent", "operator"}),
    "PATCH /events/{id}": frozenset({"panel-agent", "operator"}),
    "DELETE /events/{id}": frozenset({"panel-agent", "operator"}),
    "GET /projects": frozenset({"ha", "panel-agent", "operator"}),
    "GET /status": frozenset({"ha", "panel-agent", "operator"}),
    # This is the only A5a write-capable ingress for Home Assistant.  HA does
    # not receive generic Planning domain CRUD access.
    "POST /alice/interpret": frozenset({"ha"}),
    # Parse preview is a panel-agent-only, read-only computation.  It never
    # reaches the Alice adapter or a Planning mutation path.
    "POST /parse": frozenset({"panel-agent"}),
}
_SPECIAL_ROUTE_PERMISSIONS = {"__not_found__": frozenset(_AUDIENCE_VALUES)}

ACTOR_BY_AUDIENCE: dict[str, tuple[str, str, str]] = {
    "ha": ("planning-ha", "service", "ha"),
    "panel-agent": ("planning-panel-agent", "service", "panel-agent"),
    "operator": ("planning-operator", "operator", "operator"),
}

_READ_CAPABILITIES = {
    "reminders": ["read"],
    "tasks": ["read"],
    "events": ["read"],
    "projects": ["read"],
    "status": ["read"],
}
_WRITE_CAPABILITIES = {
    "reminders": ["read", "create", "update", "complete", "cancel"],
    "tasks": ["read", "create", "update", "complete", "archive"],
    "events": ["read", "create", "update", "delete"],
    "projects": ["read"],
    "status": ["read"],
}


@dataclass(frozen=True)
class AuthenticatedPlanningContext:
    """Identity derived only after a configured audience secret succeeds."""

    audience: str
    actor_id: str
    actor_type: str
    surface: str

    @property
    def actor(self) -> dict[str, str]:
        return {"id": self.actor_id, "type": self.actor_type, "surface": self.surface}

    def mutation_context(self, *, correlation_id: str) -> MutationContext:
        return MutationContext(
            audience=self.audience,
            actor_id=self.actor_id,
            actor_type=self.actor_type,
            surface=self.surface,
            correlation_id=correlation_id,
            source_ref=f"planning-api:{self.audience}",
        ).validate()


class InProcessRateLimiter:
    """Small per-process sliding-window limiter for the private API."""

    def __init__(
        self,
        *,
        max_requests: int = 120,
        window_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if isinstance(max_requests, bool) or not isinstance(max_requests, int) or max_requests < 1:
            raise ValueError("Planning API rate limit must be positive")
        if window_seconds <= 0:
            raise ValueError("Planning API rate window must be positive")
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._events: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = self._clock()
        cutoff = now - self.window_seconds
        with self._lock:
            events = self._events[key]
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= self.max_requests:
                return False
            events.append(now)
            return True


class PlanningAuthenticator:
    def __init__(
        self,
        secrets: Mapping[str, str],
        *,
        internal_secret: str,
        rate_limiter: InProcessRateLimiter | None = None,
    ) -> None:
        selected = {audience: secret for audience, secret in secrets.items() if secret}
        invalid = set(selected) - _AUDIENCE_VALUES
        if invalid:
            raise ValueError("Planning API has an unsupported audience configuration")
        for audience, secret in selected.items():
            validate_text(secret, f"planning.{audience}.secret", max_length=512)
        validate_text(internal_secret, "planning.internal_secret", max_length=512)
        self.secrets = selected
        self.internal_secret = internal_secret
        self.rate_limiter = rate_limiter or InProcessRateLimiter()

    @classmethod
    def from_settings(
        cls,
        settings: object,
        *,
        require_panel_agent: bool = True,
    ) -> "PlanningAuthenticator":
        secrets = {
            "ha": str(getattr(settings, "planning_ha_secret", "") or "").strip(),
            "panel-agent": str(getattr(settings, "planning_panel_agent_secret", "") or "").strip(),
            "operator": str(getattr(settings, "planning_operator_secret", "") or "").strip(),
        }
        internal_secret = str(getattr(settings, "internal_webhook_secret", "") or "").strip()
        if not internal_secret:
            raise RuntimeError(
                "Planning API is enabled but INTERNAL_WEBHOOK_SECRET is not configured"
            )
        if not secrets["ha"] or (require_panel_agent and not secrets["panel-agent"]):
            raise RuntimeError(
                "Planning API is enabled but PLANNING_HA_SECRET and, when domain "
                "routes are enabled, PLANNING_PANEL_AGENT_SECRET are not configured"
            )
        configured = [secret for secret in secrets.values() if secret]
        if len(configured) != len(set(configured)):
            raise RuntimeError("Planning API audience secrets must be independently rotatable")
        limit = int(getattr(settings, "planning_api_rate_limit_per_minute", 120))
        return cls(
            secrets,
            internal_secret=internal_secret,
            rate_limiter=InProcessRateLimiter(max_requests=limit),
        )

    def authenticate(self, request: web.Request, route_key: str) -> AuthenticatedPlanningContext:
        provided_internal_secret = request.headers.get(INTERNAL_SECRET_HEADER, "")
        audience = request.headers.get(PLANNING_AUDIENCE_HEADER, "").strip()
        provided_secret = request.headers.get(PLANNING_SECRET_HEADER, "")
        expected_secret = self.secrets.get(audience)
        if (
            not provided_internal_secret
            or not hmac.compare_digest(provided_internal_secret, self.internal_secret)
            or audience not in _AUDIENCE_VALUES
            or not expected_secret
            or not provided_secret
            or not hmac.compare_digest(provided_secret, expected_secret)
        ):
            raise PlanningApiError(
                code="authentication_failed",
                message="Planning authentication failed.",
                status=401,
            )
        identity = ACTOR_BY_AUDIENCE[audience]
        context = AuthenticatedPlanningContext(
            audience=audience,
            actor_id=identity[0],
            actor_type=identity[1],
            surface=identity[2],
        )
        allowed_audiences = ROUTE_PERMISSIONS.get(route_key) or _SPECIAL_ROUTE_PERMISSIONS.get(route_key)
        if allowed_audiences is None:
            raise PlanningApiError(
                code="route_not_allowed",
                message="Planning route is not allowlisted.",
                status=403,
            )
        if audience not in allowed_audiences:
            raise PlanningApiError(
                code="audience_forbidden",
                message="The authenticated Planning audience cannot use this route.",
                status=403,
            )
        if not self.rate_limiter.allow(audience):
            raise PlanningApiError(
                code="rate_limited",
                message="Planning request rate limit exceeded.",
                status=429,
                retryable=True,
            )
        return context


def route_permissions() -> dict[str, frozenset[str]]:
    """Return a copy for status/tests without exposing mutable auth state."""

    return dict(ROUTE_PERMISSIONS)


def capabilities_for_audience(audience: str) -> dict[str, list[str]]:
    if audience == "ha":
        return {key: list(value) for key, value in _READ_CAPABILITIES.items()}
    if audience in {"panel-agent", "operator"}:
        return {key: list(value) for key, value in _WRITE_CAPABILITIES.items()}
    return {}
