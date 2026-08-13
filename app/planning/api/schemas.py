from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from aiohttp import web

from app.planning.api.errors import PlanningApiError
from app.planning.alice import AliceInterpretRequest
from app.planning.models import (
    LOCAL_TIME_PATTERN,
    REMINDER_STATUSES,
    TASK_PRIORITIES,
    validate_date,
    validate_event_shape,
    validate_text,
    validate_timezone,
    validate_utc_timestamp,
    validate_uuid4,
    validate_task_shape,
)
from app.planning.parser import ParserInput


MAX_BODY_BYTES = 64 * 1024
MAX_QUERY_STRING_BYTES = 1024
MAX_QUERY_VALUE_LENGTH = 128
MAX_IDEMPOTENCY_KEY_LENGTH = 256
MAX_LIST_LIMIT = 100
DEFAULT_LIST_LIMIT = 50
MAX_OFFSET = 10_000
MAX_RANGE_DAYS = 366

_UNSAFE_STRUCTURAL_FIELDS = frozenset(
    {
        "service",
        "service_data",
        "entity",
        "entity_id",
        "shell",
        "shell_command",
        "command",
        "executable",
        "url",
        "host",
        "path",
        "filesystem_path",
        "headers",
        "arbitrary_headers",
        "method",
    }
)
_SERVER_OWNED_FIELDS = frozenset(
    {
        "id",
        "version",
        "created_at",
        "updated_at",
        "audit_correlation_id",
        "source",
        "source_ref",
        "created_by",
        "request_hash",
        "audience",
        "internal_sync_metadata",
        "delivery_state",
        "status",
        "completed_at",
        "cancelled_at",
        "archived_at",
        "deleted_at",
        "sync_state",
        "provider_id",
        "provider_calendar_id",
    }
)


def _validation(message: str, *, details: dict[str, Any] | None = None) -> PlanningApiError:
    return PlanningApiError(
        code="validation_error",
        message=message,
        status=400,
        details=details or {},
    )


def _duplicate_rejecting_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _reject_unsafe_fields(value: Any, *, path: str = "body") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key)
            if key_text.lower() in _UNSAFE_STRUCTURAL_FIELDS:
                raise _validation("Request contains a forbidden structural field.")
            _reject_unsafe_fields(child, path=f"{path}.{key_text}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_unsafe_fields(child, path=f"{path}[{index}]")


def _require_object(value: Any, *, context: str = "request body") -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _validation(f"{context} must be a JSON object.")
    _reject_unsafe_fields(value)
    return value


def _assert_keys(value: Mapping[str, Any], allowed: set[str], *, context: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise _validation("Request contains unknown fields.", details={"fields": sorted(unknown)[:8]})


def _assert_not_server_owned(value: Mapping[str, Any]) -> None:
    injected = set(value) & _SERVER_OWNED_FIELDS
    if injected:
        raise _validation("Request contains server-owned fields.", details={"fields": sorted(injected)[:8]})


def _required(value: Mapping[str, Any], name: str) -> Any:
    if name not in value:
        raise _validation(f"Request field {name!r} is required.")
    return value[name]


def _optional_text(value: Any, field: str, *, max_length: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _validation(f"Request field {field!r} is invalid.")
    try:
        return validate_text(value, field, max_length=max_length, allow_empty=True)
    except ValueError as exc:
        raise _validation(f"Request field {field!r} is invalid.") from exc


def _required_text(value: Any, field: str, *, max_length: int) -> str:
    if not isinstance(value, str):
        raise _validation(f"Request field {field!r} is invalid.")
    try:
        return validate_text(value, field, max_length=max_length)
    except ValueError as exc:
        raise _validation(f"Request field {field!r} is invalid.") from exc


def _optional_uuid(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _validation(f"Request field {field!r} is invalid.")
    try:
        return validate_uuid4(value, field)
    except ValueError as exc:
        raise _validation(f"Request field {field!r} is invalid.") from exc


def _optional_timezone(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _validation(f"Request field {field!r} is invalid.")
    try:
        return validate_timezone(value, field)
    except ValueError as exc:
        raise _validation(f"Request field {field!r} is invalid.") from exc


def _optional_timestamp(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _validation(f"Request field {field!r} is invalid.")
    try:
        return validate_utc_timestamp(value, field)
    except ValueError as exc:
        raise _validation(f"Request field {field!r} is invalid.") from exc


def _required_timestamp(value: Any, field: str) -> str:
    if value is None:
        raise _validation(f"Request field {field!r} is invalid.")
    result = _optional_timestamp(value, field)
    assert result is not None
    return result


def _optional_date(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _validation(f"Request field {field!r} is invalid.")
    try:
        return validate_date(value, field)
    except ValueError as exc:
        raise _validation(f"Request field {field!r} is invalid.") from exc


def _required_timezone(value: Any, field: str) -> str:
    if value is None:
        raise _validation(f"Request field {field!r} is invalid.")
    result = _optional_timezone(value, field)
    assert result is not None
    return result


def _optional_local_time(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or LOCAL_TIME_PATTERN.fullmatch(value) is None:
        raise _validation(f"Request field {field!r} is invalid.")
    return value


async def read_json_body(request: web.Request, *, required: bool = True) -> dict[str, Any]:
    """Read a bounded JSON object and reject duplicate/unsafe fields."""

    if request.content_length is not None and request.content_length > MAX_BODY_BYTES:
        raise PlanningApiError(
            code="body_too_large",
            message="Planning request body is too large.",
            status=413,
        )
    raw = bytearray()
    while not request.content.at_eof():
        chunk = await request.content.read(8192)
        if not chunk:
            break
        raw.extend(chunk)
        if len(raw) > MAX_BODY_BYTES:
            raise PlanningApiError(
                code="body_too_large",
                message="Planning request body is too large.",
                status=413,
            )
    if not raw:
        if required:
            raise PlanningApiError(
                code="malformed_json",
                message="Planning request body must contain JSON.",
                status=400,
            )
        return {}
    try:
        decoded = json.loads(raw.decode("utf-8"), object_pairs_hook=_duplicate_rejecting_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise PlanningApiError(
            code="malformed_json",
            message="Planning request body is not valid JSON.",
            status=400,
        ) from None
    return _require_object(decoded)


def validate_mutation_headers(request: web.Request, *, expected_version: bool) -> tuple[str, int | None]:
    """Validate mutation headers without ever accepting a client request hash."""

    for name in request.headers:
        normalized = name.lower().replace("-", "_")
        if normalized in {"request_hash", "x_request_hash"}:
            raise _validation("Request hash is server-computed and must not be supplied.")
    key = request.headers.get("Idempotency-Key", "")
    if not key or len(key) > MAX_IDEMPOTENCY_KEY_LENGTH or any(ord(char) < 0x20 for char in key):
        raise PlanningApiError(
            code="missing_idempotency_key" if not key else "invalid_idempotency_key",
            message="Every Planning mutation requires a valid Idempotency-Key.",
            status=400,
        )
    if not expected_version:
        return key, None
    value = request.headers.get("If-Match", "")
    if not value or not value.isdigit() or int(value) < 1:
        raise PlanningApiError(
            code="invalid_if_match",
            message="This Planning mutation requires a positive If-Match version.",
            status=400,
        )
    return key, int(value)


def parse_reminder_create(body: Mapping[str, Any]) -> dict[str, Any]:
    body = _require_object(body)
    _assert_keys(body, {"title", "notes", "due_at_utc", "timezone"}, context="reminder create")
    _assert_not_server_owned(body)
    result = {
        "title": _required_text(_required(body, "title"), "title", max_length=500),
        "notes": _optional_text(body.get("notes"), "notes", max_length=4000),
        "due_at_utc": _required_timestamp(_required(body, "due_at_utc"), "due_at_utc"),
        "timezone": _required_timezone(_required(body, "timezone"), "timezone"),
    }
    return result


def parse_reminder_patch(body: Mapping[str, Any]) -> dict[str, Any]:
    body = _require_object(body)
    _assert_keys(body, {"title", "notes", "due_at_utc", "timezone"}, context="reminder patch")
    _assert_not_server_owned(body)
    if not body:
        raise _validation("Reminder patch must contain at least one mutable field.")
    result: dict[str, Any] = {}
    if "title" in body:
        result["title"] = _required_text(body["title"], "title", max_length=500)
    if "notes" in body:
        result["notes"] = _optional_text(body["notes"], "notes", max_length=4000)
    if "due_at_utc" in body:
        result["due_at_utc"] = _required_timestamp(body["due_at_utc"], "due_at_utc")
    if "timezone" in body:
        result["timezone"] = _required_timezone(body["timezone"], "timezone")
    return result


def parse_task_create(body: Mapping[str, Any]) -> dict[str, Any]:
    body = _require_object(body)
    allowed = {"title", "notes", "due_date", "due_time", "timezone", "priority", "project_id"}
    _assert_keys(body, allowed, context="task create")
    _assert_not_server_owned(body)
    title = _required_text(_required(body, "title"), "title", max_length=500)
    priority = _required(body, "priority")
    if not isinstance(priority, str) or priority not in TASK_PRIORITIES:
        raise _validation("Request field 'priority' is invalid.")
    result = {
        "title": title,
        "notes": _optional_text(body.get("notes"), "notes", max_length=4000),
        "due_date": _optional_date(body.get("due_date"), "due_date"),
        "due_time": _optional_local_time(body.get("due_time"), "due_time"),
        "timezone": _optional_timezone(body.get("timezone"), "timezone"),
        "priority": priority,
        "project_id": _optional_uuid(body.get("project_id"), "project_id"),
    }
    try:
        validate_task_shape(
            title=result["title"],
            priority=result["priority"],
            status="open",
            notes=result["notes"],
            due_date=result["due_date"],
            due_time=result["due_time"],
            timezone_name=result["timezone"],
            project_id=result["project_id"],
        )
    except ValueError as exc:
        raise _validation("Task date/time fields are inconsistent.") from exc
    return result


def parse_task_patch(body: Mapping[str, Any]) -> dict[str, Any]:
    body = _require_object(body)
    allowed = {"title", "notes", "due_date", "due_time", "timezone", "priority", "project_id"}
    _assert_keys(body, allowed, context="task patch")
    _assert_not_server_owned(body)
    if not body:
        raise _validation("Task patch must contain at least one mutable field.")
    result: dict[str, Any] = {}
    if "title" in body:
        result["title"] = _required_text(body["title"], "title", max_length=500)
    if "notes" in body:
        result["notes"] = _optional_text(body["notes"], "notes", max_length=4000)
    if "due_date" in body:
        result["due_date"] = _optional_date(body["due_date"], "due_date")
    if "due_time" in body:
        result["due_time"] = _optional_local_time(body["due_time"], "due_time")
    if "timezone" in body:
        result["timezone"] = _optional_timezone(body["timezone"], "timezone")
    if "priority" in body:
        if not isinstance(body["priority"], str) or body["priority"] not in TASK_PRIORITIES:
            raise _validation("Request field 'priority' is invalid.")
        result["priority"] = body["priority"]
    if "project_id" in body:
        result["project_id"] = _optional_uuid(body["project_id"], "project_id")
    return result


def parse_event_create(body: Mapping[str, Any]) -> dict[str, Any]:
    body = _require_object(body)
    allowed = {
        "title",
        "notes",
        "location",
        "all_day",
        "timezone",
        "start_at_utc",
        "end_at_utc",
        "start_date",
        "end_date_exclusive",
        "recurrence_rule",
    }
    _assert_keys(body, allowed, context="event create")
    _assert_not_server_owned(body)
    title = _required_text(_required(body, "title"), "title", max_length=500)
    all_day = _required(body, "all_day")
    if not isinstance(all_day, bool):
        raise _validation("Request field 'all_day' is invalid.")
    timezone_name = _required_timezone(_required(body, "timezone"), "timezone")
    result = {
        "title": title,
        "notes": _optional_text(body.get("notes"), "notes", max_length=4000),
        "location": _optional_text(body.get("location"), "location", max_length=1000),
        "all_day": all_day,
        "timezone": timezone_name,
        "start_at_utc": _optional_timestamp(body.get("start_at_utc"), "start_at_utc"),
        "end_at_utc": _optional_timestamp(body.get("end_at_utc"), "end_at_utc"),
        "start_date": _optional_date(body.get("start_date"), "start_date"),
        "end_date_exclusive": _optional_date(body.get("end_date_exclusive"), "end_date_exclusive"),
        "recurrence_rule": body.get("recurrence_rule"),
    }
    if result["recurrence_rule"] is not None:
        raise _validation("Calendar recurrence is disabled in Planning v1.")
    try:
        validate_event_shape(
            all_day=result["all_day"],
            timezone_name=result["timezone"],
            start_at_utc=result["start_at_utc"],
            end_at_utc=result["end_at_utc"],
            start_date=result["start_date"],
            end_date_exclusive=result["end_date_exclusive"],
            sync_state="local_only",
            title=result["title"],
            notes=result["notes"],
            location=result["location"],
            recurrence_rule=None,
            provider_id=None,
            provider_calendar_id=None,
        )
    except ValueError as exc:
        raise _validation("Calendar event fields are inconsistent.") from exc
    return result


def parse_event_patch(body: Mapping[str, Any]) -> dict[str, Any]:
    body = _require_object(body)
    allowed = {
        "title",
        "notes",
        "location",
        "all_day",
        "timezone",
        "start_at_utc",
        "end_at_utc",
        "start_date",
        "end_date_exclusive",
    }
    _assert_keys(body, allowed, context="event patch")
    _assert_not_server_owned(body)
    if not body:
        raise _validation("Event patch must contain at least one mutable field.")
    result: dict[str, Any] = {}
    if "title" in body:
        result["title"] = _required_text(body["title"], "title", max_length=500)
    if "notes" in body:
        result["notes"] = _optional_text(body["notes"], "notes", max_length=4000)
    if "location" in body:
        result["location"] = _optional_text(body["location"], "location", max_length=1000)
    if "all_day" in body:
        if not isinstance(body["all_day"], bool):
            raise _validation("Request field 'all_day' is invalid.")
        result["all_day"] = body["all_day"]
    if "timezone" in body:
        result["timezone"] = _optional_timezone(body["timezone"], "timezone")
    for field in ("start_at_utc", "end_at_utc"):
        if field in body:
            result[field] = _optional_timestamp(body[field], field)
    for field in ("start_date", "end_date_exclusive"):
        if field in body:
            result[field] = _optional_date(body[field], field)
    return result


def parse_empty_body(body: Mapping[str, Any]) -> None:
    body = _require_object(body)
    if body:
        raise _validation("This Planning action does not accept a request body.")


def parse_alice_interpret_request(body: Mapping[str, Any]) -> AliceInterpretRequest:
    """Parse the closed HA/Yandex adapter input; body metadata never grants authority."""

    body = _require_object(body)
    allowed = {
        "text",
        "intent",
        "dialog",
        "application_id",
        "session_id",
        "message_id",
        "request_id",
        "user_id",
        "reference_time_utc",
        "timezone",
        "locale",
        "correlation_id",
    }
    _assert_keys(body, allowed, context="Alice interpretation")
    _assert_not_server_owned(body)
    locale = body.get("locale", "ru-RU")
    if locale != "ru-RU":
        raise _validation("Alice interpretation supports only locale ru-RU.")
    return AliceInterpretRequest(
        text=_required_text(_required(body, "text"), "text", max_length=2000),
        intent=_optional_text(body.get("intent"), "intent", max_length=128),
        dialog=_optional_text(body.get("dialog"), "dialog", max_length=128),
        application_id=_optional_text(body.get("application_id"), "application_id", max_length=256),
        session_id=_optional_text(body.get("session_id"), "session_id", max_length=256),
        message_id=_optional_text(body.get("message_id"), "message_id", max_length=256),
        request_id=_optional_text(body.get("request_id"), "request_id", max_length=256),
        user_id=_optional_text(body.get("user_id"), "user_id", max_length=256),
        reference_time_utc=_required_timestamp(_required(body, "reference_time_utc"), "reference_time_utc"),
        timezone=_required_timezone(_required(body, "timezone"), "timezone"),
        locale=locale,
        correlation_id=_optional_text(body.get("correlation_id"), "correlation_id", max_length=256),
    )


def parse_parse_preview_request(body: Mapping[str, Any]) -> ParserInput:
    """Parse the fixed, surface-neutral Planning parser preview input."""

    body = _require_object(body)
    _assert_keys(
        body,
        {"text", "reference_time_utc", "timezone", "locale"},
        context="Planning parse preview",
    )
    _assert_not_server_owned(body)
    locale = _required_text(body.get("locale", "ru-RU"), "locale", max_length=16)
    if locale != "ru-RU":
        raise _validation("Planning parse preview supports only locale ru-RU.")
    return ParserInput(
        utterance=_required_text(_required(body, "text"), "text", max_length=2000),
        reference_time_utc=_required_timestamp(
            _required(body, "reference_time_utc"),
            "reference_time_utc",
        ),
        timezone=_required_timezone(_required(body, "timezone"), "timezone"),
        locale=locale,
    )


def parse_list_options(query: Mapping[str, str], *, allowed: set[str]) -> tuple[int, int]:
    _assert_keys(query, allowed | {"limit", "offset"}, context="query")
    limit = DEFAULT_LIST_LIMIT
    offset = 0
    if "limit" in query:
        value = query["limit"]
        if not value.isdigit() or not 1 <= int(value) <= MAX_LIST_LIMIT:
            raise _validation("Query limit is outside the allowed range.")
        limit = int(value)
    if "offset" in query:
        value = query["offset"]
        if not value.isdigit() or not 0 <= int(value) <= MAX_OFFSET:
            raise _validation("Query offset is outside the allowed range.")
        offset = int(value)
    return limit, offset


def parse_query(request: web.Request, *, allowed: set[str]) -> dict[str, str]:
    if len(request.query_string.encode("utf-8")) > MAX_QUERY_STRING_BYTES:
        raise PlanningApiError(
            code="query_too_large",
            message="Planning query is too large.",
            status=413,
        )
    query = request.rel_url.query
    result: dict[str, str] = {}
    for key in query.keys():
        values = query.getall(key)
        if key not in allowed | {"limit", "offset"}:
            raise _validation("Query contains unknown fields.", details={"fields": [key]})
        if len(values) != 1 or len(values[0]) > MAX_QUERY_VALUE_LENGTH:
            raise _validation("Query contains a repeated or oversized field.")
        result[key] = values[0]
    return result


def parse_reminder_query(request: web.Request) -> tuple[str | None, str | None, str | None, int, int]:
    query = parse_query(request, allowed={"state", "from", "to"})
    state = query.get("state")
    if state is not None and state not in REMINDER_STATUSES:
        raise _validation("Query field 'state' is invalid.")
    from_utc = query.get("from")
    to_utc = query.get("to")
    if (from_utc is None) != (to_utc is None):
        raise _validation("Reminder range requires both 'from' and 'to'.")
    _validate_range(from_utc, to_utc, required=False)
    limit, offset = parse_list_options(query, allowed={"state", "from", "to"})
    return state, from_utc, to_utc, limit, offset


def parse_task_query(request: web.Request) -> tuple[str, str | None, int, int]:
    query = parse_query(request, allowed={"view", "project_id"})
    view = query.get("view")
    if view not in {"today", "overdue", "upcoming"}:
        raise _validation("Task query requires view=today, overdue, or upcoming.")
    project_id = _optional_uuid(query.get("project_id"), "project_id")
    limit, offset = parse_list_options(query, allowed={"view", "project_id"})
    return view, project_id, limit, offset


def parse_event_query(request: web.Request) -> tuple[str, str, int, int]:
    query = parse_query(request, allowed={"from", "to"})
    from_utc = query.get("from")
    to_utc = query.get("to")
    _validate_range(from_utc, to_utc, required=True)
    assert from_utc is not None and to_utc is not None
    limit, offset = parse_list_options(query, allowed={"from", "to"})
    return from_utc, to_utc, limit, offset


def parse_project_query(request: web.Request) -> tuple[int, int]:
    query = parse_query(request, allowed=set())
    return parse_list_options(query, allowed=set())


def _validate_range(from_utc: str | None, to_utc: str | None, *, required: bool) -> None:
    if from_utc is None or to_utc is None:
        if required:
            raise _validation("Planning range requires both 'from' and 'to'.")
        return
    try:
        validate_utc_timestamp(from_utc, "from")
        validate_utc_timestamp(to_utc, "to")
        start = datetime.fromisoformat(from_utc[:-1] + "+00:00")
        end = datetime.fromisoformat(to_utc[:-1] + "+00:00")
    except ValueError as exc:
        raise _validation("Planning range timestamps are invalid.") from exc
    if end <= start:
        raise _validation("Planning range end must be later than its start.")
    if end - start > timedelta(days=MAX_RANGE_DAYS):
        raise PlanningApiError(
            code="range_too_large",
            message="Planning query range is too large.",
            status=413,
        )


def parse_object_id(value: str, *, domain: str) -> str:
    try:
        return validate_uuid4(value, f"{domain}.id")
    except ValueError as exc:
        raise _validation("Planning object id is invalid.") from exc
