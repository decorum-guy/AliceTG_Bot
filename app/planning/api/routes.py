from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp import web

from app.planning.alice import AliceInterpretationService
from app.planning.api.auth import (
    AuthenticatedPlanningContext,
    PlanningAuthenticator,
)
from app.planning.api.errors import PlanningApiError
from app.planning.api.schemas import (
    parse_empty_body,
    parse_alice_interpret_request,
    parse_event_create,
    parse_event_patch,
    parse_event_query,
    parse_object_id,
    parse_parse_preview_request,
    parse_project_query,
    parse_reminder_create,
    parse_reminder_patch,
    parse_reminder_query,
    parse_task_create,
    parse_task_patch,
    parse_task_query,
    read_json_body,
    validate_mutation_headers,
)
from app.planning.api.service import PlanningApiService, StoredMutationResponse
from app.planning.errors import (
    PlanningEventNotLocalOnlyError,
    PlanningIdempotencyConflictError,
    PlanningIdempotencyInProgressError,
    PlanningNotFoundError,
    PlanningValidationError,
    PlanningVersionConflictError,
)
from app.planning.models import new_uuid4
from app.planning.parser import PlanningParser


LOGGER = logging.getLogger(__name__)
PLANNING_PREFIX = "/internal/planning/v1"


Handler = Callable[[web.Request, AuthenticatedPlanningContext], Awaitable[web.Response]]


def _json_response(payload: dict[str, Any], *, status: int = 200, correlation_id: str | None = None) -> web.Response:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    headers = {"Cache-Control": "no-store"}
    if correlation_id:
        headers["X-Correlation-ID"] = correlation_id
    return web.Response(
        body=encoded.encode("utf-8"),
        status=status,
        content_type="application/json",
        charset="utf-8",
        headers=headers,
    )


def _service(request: web.Request) -> PlanningApiService:
    service = request.app.get("planning_api_service")
    if not isinstance(service, PlanningApiService):
        raise RuntimeError("Planning API service is not configured")
    return service


def _error_response(
    request: web.Request,
    *,
    error: PlanningApiError,
    actor: AuthenticatedPlanningContext | None,
    correlation_id: str,
) -> web.Response:
    builder = _service(request).envelopes
    payload = builder.error_response(
        status=error.status,
        code=error.code,
        message=error.message,
        details=error.details,
        retryable=error.retryable,
        correlation_id=correlation_id,
        actor=None if actor is None else actor.actor,
    )
    return _json_response(payload, status=error.status, correlation_id=correlation_id)


def _domain_error(exc: Exception) -> PlanningApiError:
    if isinstance(exc, PlanningEventNotLocalOnlyError):
        return PlanningApiError(
            code="event_not_local_only",
            message="Only native local-only calendar events can be mutated through Planning.",
            status=409,
        )
    if isinstance(exc, PlanningVersionConflictError):
        details: dict[str, Any] = {
            "domain": exc.domain,
            "object_id": exc.object_id,
            "expected_version": exc.expected_version,
        }
        if exc.actual_version is not None:
            details["actual_version"] = exc.actual_version
        return PlanningApiError(
            code="version_conflict",
            message="Object version is stale.",
            status=409,
            details=details,
        )
    if isinstance(exc, PlanningIdempotencyConflictError):
        return PlanningApiError(
            code="idempotency_conflict",
            message="Idempotency-Key is already bound to another request.",
            status=409,
        )
    if isinstance(exc, PlanningIdempotencyInProgressError):
        return PlanningApiError(
            code="idempotency_in_progress",
            message="The idempotent Planning mutation is still in progress.",
            status=409,
            retryable=True,
        )
    if isinstance(exc, PlanningNotFoundError):
        return PlanningApiError(
            code="not_found",
            message="Planning object was not found.",
            status=404,
        )
    if isinstance(exc, PlanningValidationError):
        return PlanningApiError(
            code="validation_error",
            message="Planning request validation failed.",
            status=400,
        )
    if isinstance(exc, sqlite3.IntegrityError):
        return PlanningApiError(
            code="mutation_conflict",
            message="Planning mutation conflicts with current state.",
            status=409,
        )
    return PlanningApiError(
        code="internal_error",
        message="Planning service is temporarily unavailable.",
        status=500,
        retryable=True,
    )


async def _dispatch(request: web.Request, route_key: str, handler: Handler) -> web.Response:
    correlation_id = new_uuid4()
    actor: AuthenticatedPlanningContext | None = None
    try:
        authenticator: PlanningAuthenticator = request.app["planning_authenticator"]
        actor = authenticator.authenticate(request, route_key)
        response = await handler(request, actor)
        return response
    except PlanningApiError as exc:
        return _error_response(request, error=exc, actor=actor, correlation_id=correlation_id)
    except (
        PlanningEventNotLocalOnlyError,
        PlanningVersionConflictError,
        PlanningIdempotencyConflictError,
        PlanningIdempotencyInProgressError,
        PlanningNotFoundError,
        PlanningValidationError,
        sqlite3.IntegrityError,
    ) as exc:
        return _error_response(request, error=_domain_error(exc), actor=actor, correlation_id=correlation_id)
    except Exception as exc:
        # Do not log exception messages: repository/database errors may contain
        # paths or provider text.  Correlation plus type is enough to find the
        # failing class in local diagnostics without leaking request content.
        LOGGER.error(
            "Planning API request failed: method=%s route=%s correlation_id=%s error_type=%s",
            request.method,
            route_key,
            correlation_id,
            type(exc).__name__,
        )
        return _error_response(
            request,
            error=_domain_error(exc),
            actor=actor,
            correlation_id=correlation_id,
        )


async def _get_reminders(request: web.Request, auth: AuthenticatedPlanningContext) -> web.Response:
    state, from_utc, to_utc, limit, offset = parse_reminder_query(request)
    correlation_id = new_uuid4()
    payload = _service(request).list_reminders(
        state=state,
        from_utc=from_utc,
        to_utc=to_utc,
        limit=limit,
        offset=offset,
        correlation_id=correlation_id,
    )
    return _json_response(payload, correlation_id=correlation_id)


async def _create_reminder(request: web.Request, auth: AuthenticatedPlanningContext) -> web.Response:
    key, expected_version = validate_mutation_headers(request, expected_version=False)
    del expected_version
    payload = parse_reminder_create(await read_json_body(request))
    result = _service(request).create_reminder(auth=auth, key=key, payload=payload)
    return _stored_response(result)


async def _patch_reminder(request: web.Request, auth: AuthenticatedPlanningContext) -> web.Response:
    reminder_id = parse_object_id(request.match_info["reminder_id"], domain="reminder")
    key, expected_version = validate_mutation_headers(request, expected_version=True)
    assert expected_version is not None
    payload = parse_reminder_patch(await read_json_body(request))
    result = _service(request).patch_reminder(
        auth=auth,
        key=key,
        reminder_id=reminder_id,
        expected_version=expected_version,
        payload=payload,
    )
    return _stored_response(result)


async def _complete_reminder(request: web.Request, auth: AuthenticatedPlanningContext) -> web.Response:
    reminder_id = parse_object_id(request.match_info["reminder_id"], domain="reminder")
    key, expected_version = validate_mutation_headers(request, expected_version=True)
    assert expected_version is not None
    parse_empty_body(await read_json_body(request, required=False))
    result = _service(request).complete_reminder(
        auth=auth,
        key=key,
        reminder_id=reminder_id,
        expected_version=expected_version,
    )
    return _stored_response(result)


async def _cancel_reminder(request: web.Request, auth: AuthenticatedPlanningContext) -> web.Response:
    reminder_id = parse_object_id(request.match_info["reminder_id"], domain="reminder")
    key, expected_version = validate_mutation_headers(request, expected_version=True)
    assert expected_version is not None
    parse_empty_body(await read_json_body(request, required=False))
    result = _service(request).cancel_reminder(
        auth=auth,
        key=key,
        reminder_id=reminder_id,
        expected_version=expected_version,
    )
    return _stored_response(result)


async def _get_tasks(request: web.Request, auth: AuthenticatedPlanningContext) -> web.Response:
    view, project_id, limit, offset = parse_task_query(request)
    correlation_id = new_uuid4()
    payload = _service(request).list_tasks(
        view=view,
        project_id=project_id,
        limit=limit,
        offset=offset,
        correlation_id=correlation_id,
    )
    return _json_response(payload, correlation_id=correlation_id)


async def _get_task(request: web.Request, auth: AuthenticatedPlanningContext) -> web.Response:
    task_id = parse_object_id(request.match_info["task_id"], domain="task")
    if request.query_string:
        raise PlanningApiError(
            code="validation_error",
            message="This Planning task read does not accept query parameters.",
            status=400,
        )
    correlation_id = new_uuid4()
    payload = _service(request).get_task(task_id=task_id, correlation_id=correlation_id)
    return _json_response(payload, correlation_id=correlation_id)


async def _create_task(request: web.Request, auth: AuthenticatedPlanningContext) -> web.Response:
    key, _ = validate_mutation_headers(request, expected_version=False)
    payload = parse_task_create(await read_json_body(request))
    return _stored_response(_service(request).create_task(auth=auth, key=key, payload=payload))


async def _patch_task(request: web.Request, auth: AuthenticatedPlanningContext) -> web.Response:
    task_id = parse_object_id(request.match_info["task_id"], domain="task")
    key, expected_version = validate_mutation_headers(request, expected_version=True)
    assert expected_version is not None
    payload = parse_task_patch(await read_json_body(request))
    return _stored_response(
        _service(request).patch_task(
            auth=auth,
            key=key,
            task_id=task_id,
            expected_version=expected_version,
            payload=payload,
        )
    )


async def _complete_task(request: web.Request, auth: AuthenticatedPlanningContext) -> web.Response:
    task_id = parse_object_id(request.match_info["task_id"], domain="task")
    key, expected_version = validate_mutation_headers(request, expected_version=True)
    assert expected_version is not None
    parse_empty_body(await read_json_body(request, required=False))
    return _stored_response(
        _service(request).complete_task(
            auth=auth,
            key=key,
            task_id=task_id,
            expected_version=expected_version,
        )
    )


async def _delete_task(request: web.Request, auth: AuthenticatedPlanningContext) -> web.Response:
    task_id = parse_object_id(request.match_info["task_id"], domain="task")
    key, expected_version = validate_mutation_headers(request, expected_version=True)
    assert expected_version is not None
    parse_empty_body(await read_json_body(request, required=False))
    return _stored_response(
        _service(request).archive_task(
            auth=auth,
            key=key,
            task_id=task_id,
            expected_version=expected_version,
        )
    )


async def _get_events(request: web.Request, auth: AuthenticatedPlanningContext) -> web.Response:
    from_utc, to_utc, limit, offset = parse_event_query(request)
    correlation_id = new_uuid4()
    payload = _service(request).list_events(
        from_utc=from_utc,
        to_utc=to_utc,
        limit=limit,
        offset=offset,
        correlation_id=correlation_id,
    )
    return _json_response(payload, correlation_id=correlation_id)


async def _get_event(request: web.Request, auth: AuthenticatedPlanningContext) -> web.Response:
    event_id = parse_object_id(request.match_info["event_id"], domain="calendar_event")
    if request.query_string:
        raise PlanningApiError(
            code="validation_error",
            message="This Planning event read does not accept query parameters.",
            status=400,
        )
    correlation_id = new_uuid4()
    payload = _service(request).get_event(event_id=event_id, correlation_id=correlation_id)
    return _json_response(payload, correlation_id=correlation_id)


async def _create_event(request: web.Request, auth: AuthenticatedPlanningContext) -> web.Response:
    key, _ = validate_mutation_headers(request, expected_version=False)
    payload = parse_event_create(await read_json_body(request))
    return _stored_response(_service(request).create_event(auth=auth, key=key, payload=payload))


async def _patch_event(request: web.Request, auth: AuthenticatedPlanningContext) -> web.Response:
    event_id = parse_object_id(request.match_info["event_id"], domain="calendar_event")
    key, expected_version = validate_mutation_headers(request, expected_version=True)
    assert expected_version is not None
    payload = parse_event_patch(await read_json_body(request))
    return _stored_response(
        _service(request).patch_event(
            auth=auth,
            key=key,
            event_id=event_id,
            expected_version=expected_version,
            payload=payload,
        )
    )


async def _delete_event(request: web.Request, auth: AuthenticatedPlanningContext) -> web.Response:
    event_id = parse_object_id(request.match_info["event_id"], domain="calendar_event")
    key, expected_version = validate_mutation_headers(request, expected_version=True)
    assert expected_version is not None
    parse_empty_body(await read_json_body(request, required=False))
    return _stored_response(
        _service(request).delete_event(
            auth=auth,
            key=key,
            event_id=event_id,
            expected_version=expected_version,
        )
    )


async def _get_projects(request: web.Request, auth: AuthenticatedPlanningContext) -> web.Response:
    limit, offset = parse_project_query(request)
    correlation_id = new_uuid4()
    payload = _service(request).list_projects(
        limit=limit,
        offset=offset,
        correlation_id=correlation_id,
    )
    return _json_response(payload, correlation_id=correlation_id)


async def _get_status(request: web.Request, auth: AuthenticatedPlanningContext) -> web.Response:
    correlation_id = new_uuid4()
    payload = _service(request).status(audience=auth.audience, correlation_id=correlation_id)
    return _json_response(payload, correlation_id=correlation_id)


async def _parse_preview(request: web.Request, auth: AuthenticatedPlanningContext) -> web.Response:
    del auth
    parser_input = parse_parse_preview_request(await read_json_body(request))
    # This route deliberately calls the canonical parser directly.  It does
    # not construct or invoke AliceInterpretationService, repository services,
    # idempotency, audit, outbox, or provider adapters.
    parsed = PlanningParser().parse(parser_input)
    correlation_id = new_uuid4()
    payload = _service(request).envelopes.parse_preview_response(
        result=parsed,
        correlation_id=correlation_id,
    )
    return _json_response(payload, correlation_id=correlation_id)


async def _alice_interpret(request: web.Request, auth: AuthenticatedPlanningContext) -> web.Response:
    service = request.app.get("planning_alice_service")
    if not isinstance(service, AliceInterpretationService):
        raise RuntimeError("Alice interpretation service is not configured")
    payload = parse_alice_interpret_request(await read_json_body(request))
    result = service.interpret(auth=auth, request=payload)
    return _stored_response(result)


async def _planning_not_found(request: web.Request, auth: AuthenticatedPlanningContext) -> web.Response:
    raise PlanningApiError(
        code="route_not_found",
        message="Planning route is not implemented.",
        status=404,
    )


def _stored_response(result: StoredMutationResponse) -> web.Response:
    return web.Response(
        body=result.response_json.encode("utf-8"),
        status=result.status,
        content_type="application/json",
        charset="utf-8",
        headers={
            "Cache-Control": "no-store",
            "X-Correlation-ID": _correlation_from_response(result.response_json),
        },
    )


def _correlation_from_response(response_json: str) -> str:
    try:
        value = json.loads(response_json).get("correlation_id")
    except (TypeError, ValueError, AttributeError):
        value = None
    return value if isinstance(value, str) else "unknown"


def _route(handler: Handler, route_key: str) -> Callable[[web.Request], Awaitable[web.Response]]:
    async def wrapped(request: web.Request) -> web.Response:
        return await _dispatch(request, route_key, handler)

    return wrapped


def setup_planning_routes(
    app: web.Application,
    *,
    include_domain_routes: bool = True,
    include_alice_route: bool = False,
) -> None:
    """Register the fixed A4 domain surface and/or the narrow A5a Alice route."""

    database = app.get("planning_database")
    if database is None:
        raise RuntimeError("Planning API cannot be enabled without Planning storage")
    settings = app["settings"]
    service = app.get("planning_api_service")
    if service is None:
        service = PlanningApiService(
            database,
            default_timezone=str(getattr(settings, "planning_default_timezone", "Europe/Moscow")),
            stale_after_seconds=int(getattr(settings, "planning_api_stale_after_seconds", 300)),
            health_service=app.get("planning_health_service"),
            provider_cache=app.get("planning_icloud_cache"),
        )
        app["planning_api_service"] = service
    app["planning_authenticator"] = PlanningAuthenticator.from_settings(
        settings,
        require_panel_agent=include_domain_routes,
    )

    if include_alice_route:
        app["planning_alice_service"] = AliceInterpretationService(
            database,
            idempotency_secret=str(getattr(settings, "planning_alice_idempotency_secret", "") or ""),
        )

    if not include_domain_routes:
        if include_alice_route:
            app.router.add_post(
                f"{PLANNING_PREFIX}/alice/interpret",
                _route(_alice_interpret, "POST /alice/interpret"),
            )
        return

    app.router.add_get(f"{PLANNING_PREFIX}/reminders", _route(_get_reminders, "GET /reminders"))
    app.router.add_post(f"{PLANNING_PREFIX}/reminders", _route(_create_reminder, "POST /reminders"))
    app.router.add_patch(
        f"{PLANNING_PREFIX}/reminders/{{reminder_id}}",
        _route(_patch_reminder, "PATCH /reminders/{id}"),
    )
    app.router.add_post(
        f"{PLANNING_PREFIX}/reminders/{{reminder_id}}/complete",
        _route(_complete_reminder, "POST /reminders/{id}/complete"),
    )
    app.router.add_post(
        f"{PLANNING_PREFIX}/reminders/{{reminder_id}}/cancel",
        _route(_cancel_reminder, "POST /reminders/{id}/cancel"),
    )
    app.router.add_get(f"{PLANNING_PREFIX}/tasks", _route(_get_tasks, "GET /tasks"))
    app.router.add_get(
        f"{PLANNING_PREFIX}/tasks/{{task_id}}",
        _route(_get_task, "GET /tasks/{id}"),
    )
    app.router.add_post(f"{PLANNING_PREFIX}/tasks", _route(_create_task, "POST /tasks"))
    app.router.add_patch(
        f"{PLANNING_PREFIX}/tasks/{{task_id}}",
        _route(_patch_task, "PATCH /tasks/{id}"),
    )
    app.router.add_post(
        f"{PLANNING_PREFIX}/tasks/{{task_id}}/complete",
        _route(_complete_task, "POST /tasks/{id}/complete"),
    )
    app.router.add_delete(
        f"{PLANNING_PREFIX}/tasks/{{task_id}}",
        _route(_delete_task, "DELETE /tasks/{id}"),
    )
    app.router.add_get(f"{PLANNING_PREFIX}/events", _route(_get_events, "GET /events"))
    app.router.add_get(
        f"{PLANNING_PREFIX}/events/{{event_id}}",
        _route(_get_event, "GET /events/{id}"),
    )
    app.router.add_post(f"{PLANNING_PREFIX}/events", _route(_create_event, "POST /events"))
    app.router.add_patch(
        f"{PLANNING_PREFIX}/events/{{event_id}}",
        _route(_patch_event, "PATCH /events/{id}"),
    )
    app.router.add_delete(
        f"{PLANNING_PREFIX}/events/{{event_id}}",
        _route(_delete_event, "DELETE /events/{id}"),
    )
    app.router.add_get(f"{PLANNING_PREFIX}/projects", _route(_get_projects, "GET /projects"))
    app.router.add_get(f"{PLANNING_PREFIX}/status", _route(_get_status, "GET /status"))
    app.router.add_post(f"{PLANNING_PREFIX}/parse", _route(_parse_preview, "POST /parse"))
    if include_alice_route:
        app.router.add_post(
            f"{PLANNING_PREFIX}/alice/interpret",
            _route(_alice_interpret, "POST /alice/interpret"),
        )
    # Keep unmatched Planning paths inside the versioned error envelope.  This
    # does not create a generic operation surface; it is only a redacted 404.
    app.router.add_route(
        "*",
        f"{PLANNING_PREFIX}/{{tail:.*}}",
        _route(_planning_not_found, "__not_found__"),
    )
