from __future__ import annotations

import json
import re
import unittest
import uuid
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "planning_v1"
UTC_RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")
DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
LOCAL_TIME = re.compile(r"^\d{2}:\d{2}(?::\d{2})?$")
SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")

FORBIDDEN_EXECUTION_FIELDS = {
    "ha_service",
    "ha_entity",
    "service",
    "entity",
    "entity_id",
    "shell_command",
    "command",
    "executable",
    "url",
    "host",
    "path",
    "filesystem_path",
}

COMMON_OBJECT_FIELDS = {
    "domain",
    "id",
    "source",
    "source_ref",
    "version",
    "created_at",
    "updated_at",
    "deleted_at",
    "audit_correlation_id",
}
COMMON_REQUIRED_FIELDS = COMMON_OBJECT_FIELDS - {"source_ref", "deleted_at"}

SOURCE_VALUES = {"alice", "telegram", "panel-agent", "operator", "ticktick", "calendar-provider", "system"}
SOURCE_STATUS_VALUES = {"current", "stale", "offline", "degraded"}
ACTOR_TYPES = {"user", "service", "operator"}
SURFACES = {"ha", "panel-agent", "telegram", "operator", "system"}
AUDIENCES = {"ha", "panel-agent", "operator"}
CLIENT_TASK_CREATE_FIELDS = {"title", "notes", "due_date", "due_time", "timezone", "priority", "project_id"}
SERVER_MANAGED_CLIENT_FIELDS = {"id", "version", "created_at", "updated_at", "audit_correlation_id"}


class ContractViolation(AssertionError):
    pass


def _load(name: str) -> Any:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _require_mapping(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractViolation(f"{context} must be an object")
    _reject_execution_fields(value, context)
    return value


def _assert_keys(value: dict[str, Any], allowed: set[str], required: set[str], context: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise ContractViolation(f"{context} has unknown fields: {sorted(unknown)}")
    missing = required - set(value)
    if missing:
        raise ContractViolation(f"{context} is missing fields: {sorted(missing)}")


def _reject_execution_fields(value: Any, context: str) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in FORBIDDEN_EXECUTION_FIELDS:
                raise ContractViolation(f"{context} contains forbidden execution field {key!r}")
            _reject_execution_fields(child, f"{context}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_execution_fields(child, f"{context}[{index}]")


def _assert_nonempty_string(value: Any, context: str) -> None:
    if not isinstance(value, str) or not value:
        raise ContractViolation(f"{context} must be a non-empty string")


def _assert_uuid4(value: Any, context: str) -> None:
    if not isinstance(value, str):
        raise ContractViolation(f"{context} must be a UUID string")
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise ContractViolation(f"{context} is not a UUID") from exc
    if parsed.version != 4 or parsed.variant != uuid.RFC_4122:
        raise ContractViolation(f"{context} must be UUIDv4")


def _assert_utc_timestamp(value: Any, context: str) -> None:
    if not isinstance(value, str) or not UTC_RFC3339.fullmatch(value):
        raise ContractViolation(f"{context} must be a UTC RFC3339 timestamp with Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ContractViolation(f"{context} is not a valid timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ContractViolation(f"{context} must be UTC")


def _assert_optional_timestamp(value: Any, context: str) -> None:
    if value is not None:
        _assert_utc_timestamp(value, context)


def _assert_timezone(value: Any, context: str) -> None:
    if not isinstance(value, str):
        raise ContractViolation(f"{context} must be an IANA timezone")
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise ContractViolation(f"{context} is not an IANA timezone") from exc


def _assert_date(value: Any, context: str) -> None:
    if not isinstance(value, str) or not DATE_ONLY.fullmatch(value):
        raise ContractViolation(f"{context} must be YYYY-MM-DD")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ContractViolation(f"{context} is not a valid calendar date") from exc


def _assert_local_time(value: Any, context: str) -> None:
    if not isinstance(value, str) or not LOCAL_TIME.fullmatch(value):
        raise ContractViolation(f"{context} must be HH:MM or HH:MM:SS")
    parts = [int(part) for part in value.split(":")]
    if parts[0] > 23 or parts[1] > 59 or (len(parts) == 3 and parts[2] > 59):
        raise ContractViolation(f"{context} is not a valid local time")


def _assert_positive_version(value: Any, context: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ContractViolation(f"{context} must be a positive integer")


def _assert_source_fields(value: dict[str, Any], context: str) -> None:
    _assert_nonempty_string(value["source"], f"{context}.source")
    if value["source"] not in SOURCE_VALUES:
        raise ContractViolation(f"{context}.source has an invalid enum")
    if "source_ref" in value and value["source_ref"] is not None:
        _assert_nonempty_string(value["source_ref"], f"{context}.source_ref")
    _assert_uuid4(value["audit_correlation_id"], f"{context}.audit_correlation_id")


def _assert_common_object_fields(value: dict[str, Any], domain: str, context: str) -> None:
    if value["domain"] != domain:
        raise ContractViolation(f"{context}.domain must be {domain!r}")
    _assert_uuid4(value["id"], f"{context}.id")
    _assert_positive_version(value["version"], f"{context}.version")
    _assert_utc_timestamp(value["created_at"], f"{context}.created_at")
    _assert_utc_timestamp(value["updated_at"], f"{context}.updated_at")
    if "deleted_at" in value:
        _assert_optional_timestamp(value["deleted_at"], f"{context}.deleted_at")
    _assert_source_fields(value, context)


def _validate_reminder(value: Any, context: str = "reminder") -> dict[str, Any]:
    obj = _require_mapping(value, context)
    allowed = COMMON_OBJECT_FIELDS | {
        "title",
        "notes",
        "due_at_utc",
        "timezone",
        "status",
        "created_by",
        "completed_at",
        "cancelled_at",
        "delivery_state",
        "next_attempt_at",
        "final_failure_at",
    }
    required = COMMON_REQUIRED_FIELDS | {
        "title",
        "due_at_utc",
        "timezone",
        "status",
        "created_by",
        "delivery_state",
    }
    _assert_keys(obj, allowed, required, context)
    _assert_common_object_fields(obj, "reminder", context)
    _assert_nonempty_string(obj["title"], f"{context}.title")
    if "notes" in obj and obj["notes"] is not None:
        _assert_nonempty_string(obj["notes"], f"{context}.notes")
    _assert_utc_timestamp(obj["due_at_utc"], f"{context}.due_at_utc")
    _assert_timezone(obj["timezone"], f"{context}.timezone")
    if obj["status"] not in {"pending", "due", "completed", "cancelled"}:
        raise ContractViolation(f"{context}.status has an invalid enum")
    _assert_nonempty_string(obj["created_by"], f"{context}.created_by")
    if obj["delivery_state"] not in {"not_due", "queued", "retrying", "delivered", "failed"}:
        raise ContractViolation(f"{context}.delivery_state has an invalid enum")
    for field in ("completed_at", "cancelled_at", "next_attempt_at", "final_failure_at"):
        if field in obj:
            _assert_optional_timestamp(obj[field], f"{context}.{field}")
    return obj


def _validate_task(value: Any, context: str = "task") -> dict[str, Any]:
    obj = _require_mapping(value, context)
    allowed = COMMON_OBJECT_FIELDS | {
        "title",
        "notes",
        "due_date",
        "due_time",
        "timezone",
        "priority",
        "project_id",
        "status",
        "completed_at",
        "archived_at",
    }
    required = COMMON_REQUIRED_FIELDS | {"title", "priority", "status"}
    _assert_keys(obj, allowed, required, context)
    _assert_common_object_fields(obj, "task", context)
    _assert_nonempty_string(obj["title"], f"{context}.title")
    if "notes" in obj and obj["notes"] is not None:
        _assert_nonempty_string(obj["notes"], f"{context}.notes")
    if "due_date" in obj and obj["due_date"] is not None:
        _assert_date(obj["due_date"], f"{context}.due_date")
    if "due_time" in obj and obj["due_time"] is not None:
        if "due_date" not in obj or obj.get("due_date") is None or "timezone" not in obj:
            raise ContractViolation(f"{context}.due_time requires due_date and timezone")
        _assert_local_time(obj["due_time"], f"{context}.due_time")
    if "timezone" in obj and obj["timezone"] is not None:
        _assert_timezone(obj["timezone"], f"{context}.timezone")
    if obj["priority"] not in {"none", "low", "normal", "high"}:
        raise ContractViolation(f"{context}.priority has an invalid enum")
    if "project_id" in obj and obj["project_id"] is not None:
        _assert_uuid4(obj["project_id"], f"{context}.project_id")
    if obj["status"] not in {"open", "completed", "archived"}:
        raise ContractViolation(f"{context}.status has an invalid enum")
    for field in ("completed_at", "archived_at"):
        if field in obj:
            _assert_optional_timestamp(obj[field], f"{context}.{field}")
    return obj


def _validate_event(value: Any, context: str = "calendar_event") -> dict[str, Any]:
    obj = _require_mapping(value, context)
    allowed = COMMON_OBJECT_FIELDS | {
        "title",
        "notes",
        "location",
        "all_day",
        "start_at_utc",
        "end_at_utc",
        "start_date",
        "end_date_exclusive",
        "timezone",
        "recurrence_rule",
        "provider_id",
        "provider_calendar_id",
        "sync_state",
    }
    required = COMMON_REQUIRED_FIELDS | {"title", "all_day", "timezone", "sync_state"}
    _assert_keys(obj, allowed, required, context)
    _assert_common_object_fields(obj, "calendar_event", context)
    _assert_nonempty_string(obj["title"], f"{context}.title")
    if "notes" in obj and obj["notes"] is not None:
        _assert_nonempty_string(obj["notes"], f"{context}.notes")
    if "location" in obj and obj["location"] is not None:
        _assert_nonempty_string(obj["location"], f"{context}.location")
    if not isinstance(obj["all_day"], bool):
        raise ContractViolation(f"{context}.all_day must be boolean")
    _assert_timezone(obj["timezone"], f"{context}.timezone")
    if obj["sync_state"] not in {"local_only", "pending", "synced", "stale", "conflict", "error"}:
        raise ContractViolation(f"{context}.sync_state has an invalid enum")
    if obj["all_day"]:
        if "start_date" not in obj or "end_date_exclusive" not in obj:
            raise ContractViolation(f"{context} all-day representation requires dates")
        if "start_at_utc" in obj or "end_at_utc" in obj:
            raise ContractViolation(f"{context} all-day representation cannot contain timed fields")
        _assert_date(obj["start_date"], f"{context}.start_date")
        _assert_date(obj["end_date_exclusive"], f"{context}.end_date_exclusive")
        if date.fromisoformat(obj["end_date_exclusive"]) <= date.fromisoformat(obj["start_date"]):
            raise ContractViolation(f"{context}.end_date_exclusive must be later and exclusive")
    else:
        if "start_at_utc" not in obj or "end_at_utc" not in obj:
            raise ContractViolation(f"{context} timed representation requires timestamps")
        if "start_date" in obj or "end_date_exclusive" in obj:
            raise ContractViolation(f"{context} timed representation cannot contain date-only fields")
        _assert_utc_timestamp(obj["start_at_utc"], f"{context}.start_at_utc")
        _assert_utc_timestamp(obj["end_at_utc"], f"{context}.end_at_utc")
        start = datetime.fromisoformat(obj["start_at_utc"][:-1] + "+00:00")
        end = datetime.fromisoformat(obj["end_at_utc"][:-1] + "+00:00")
        if end <= start:
            raise ContractViolation(f"{context}.end_at_utc must be later than start")
    if "recurrence_rule" in obj and obj["recurrence_rule"] is not None:
        raise ContractViolation("recurrence behavior is disabled in A0")
    for field in ("provider_id", "provider_calendar_id"):
        if field in obj and obj[field] is not None:
            _assert_nonempty_string(obj[field], f"{context}.{field}")
    return obj


def _validate_actor(value: Any, context: str = "actor") -> dict[str, Any]:
    actor = _require_mapping(value, context)
    _assert_keys(actor, {"id", "type", "surface"}, {"id", "type", "surface"}, context)
    _assert_nonempty_string(actor["id"], f"{context}.id")
    if actor["type"] not in ACTOR_TYPES:
        raise ContractViolation(f"{context}.type has an invalid enum")
    if actor["surface"] not in SURFACES:
        raise ContractViolation(f"{context}.surface has an invalid enum")
    return actor


def _validate_freshness_envelope(value: Any) -> dict[str, Any]:
    envelope = _require_mapping(value, "list envelope")
    _assert_keys(
        envelope,
        {"schemaVersion", "kind", "domain", "items", "generatedAt", "sourceStatus", "lastSyncedAt", "staleAfter", "correlation_id"},
        {"schemaVersion", "kind", "domain", "items", "generatedAt", "sourceStatus", "lastSyncedAt", "staleAfter", "correlation_id"},
        "list envelope",
    )
    if envelope["schemaVersion"] != "planning.v1" or envelope["kind"] != "list":
        raise ContractViolation("list envelope version or kind is invalid")
    if envelope["domain"] not in {"reminder", "task", "calendar_event"}:
        raise ContractViolation("list envelope domain is invalid")
    if not isinstance(envelope["items"], list):
        raise ContractViolation("list envelope.items must be an array")
    validator = {"reminder": _validate_reminder, "task": _validate_task, "calendar_event": _validate_event}[envelope["domain"]]
    for index, item in enumerate(envelope["items"]):
        validator(item, f"list envelope.items[{index}]")
    _assert_utc_timestamp(envelope["generatedAt"], "list envelope.generatedAt")
    if envelope["sourceStatus"] not in SOURCE_STATUS_VALUES:
        raise ContractViolation("list envelope.sourceStatus has an invalid enum")
    if envelope["lastSyncedAt"] is not None:
        _assert_utc_timestamp(envelope["lastSyncedAt"], "list envelope.lastSyncedAt")
    _assert_utc_timestamp(envelope["staleAfter"], "list envelope.staleAfter")
    _assert_uuid4(envelope["correlation_id"], "list envelope.correlation_id")
    return envelope


def _validate_authenticated_context(value: Any, context: str = "authenticated_context") -> dict[str, Any]:
    auth_context = _require_mapping(value, context)
    _assert_keys(auth_context, {"audience", "actor"}, {"audience", "actor"}, context)
    if auth_context["audience"] not in AUDIENCES:
        raise ContractViolation(f"{context}.audience has an invalid enum")
    _validate_actor(auth_context["actor"], f"{context}.actor")
    return auth_context


def _validate_task_create_body(value: Any, context: str = "task create body") -> dict[str, Any]:
    body = _require_mapping(value, context)
    _assert_keys(body, CLIENT_TASK_CREATE_FIELDS, {"title", "priority"}, context)
    _assert_nonempty_string(body["title"], f"{context}.title")
    if "notes" in body and body["notes"] is not None:
        _assert_nonempty_string(body["notes"], f"{context}.notes")
    if "due_date" in body and body["due_date"] is not None:
        _assert_date(body["due_date"], f"{context}.due_date")
    if "due_time" in body and body["due_time"] is not None:
        if "due_date" not in body or body.get("due_date") is None or "timezone" not in body:
            raise ContractViolation(f"{context}.due_time requires due_date and timezone")
        _assert_local_time(body["due_time"], f"{context}.due_time")
    if "timezone" in body and body["timezone"] is not None:
        _assert_timezone(body["timezone"], f"{context}.timezone")
    if body["priority"] not in {"none", "low", "normal", "high"}:
        raise ContractViolation(f"{context}.priority has an invalid enum")
    if "project_id" in body and body["project_id"] is not None:
        _assert_uuid4(body["project_id"], f"{context}.project_id")
    return body


def _validate_create_request(value: Any, context: str = "create request") -> dict[str, Any]:
    request = _require_mapping(value, context)
    _assert_keys(request, {"route", "headers", "body"}, {"route", "headers", "body"}, context)
    if request["route"] != "/internal/planning/v1/tasks":
        raise ContractViolation(f"{context}.route must identify the task create route")
    headers = _require_mapping(request["headers"], f"{context}.headers")
    _assert_keys(headers, {"Idempotency-Key"}, {"Idempotency-Key"}, f"{context}.headers")
    _assert_nonempty_string(headers["Idempotency-Key"], f"{context}.headers.Idempotency-Key")
    _validate_task_create_body(request["body"], f"{context}.body")
    return request


def _validate_canonical_response(value: Any, context: str = "canonical response") -> dict[str, Any]:
    envelope = _require_mapping(value, context)
    _assert_keys(
        envelope,
        {"schemaVersion", "kind", "domain", "object", "sourceStatus", "lastSyncedAt", "staleAfter", "correlation_id"},
        {"schemaVersion", "kind", "domain", "object", "sourceStatus", "lastSyncedAt", "staleAfter", "correlation_id"},
        context,
    )
    if envelope["schemaVersion"] != "planning.v1" or envelope["kind"] != "object":
        raise ContractViolation(f"{context} version or kind is invalid")
    validators = {"reminder": _validate_reminder, "task": _validate_task, "calendar_event": _validate_event}
    validator = validators.get(envelope["domain"])
    if validator is None:
        raise ContractViolation(f"{context}.domain has an invalid enum")
    validator(envelope["object"], f"{context}.object")
    if envelope["sourceStatus"] not in SOURCE_STATUS_VALUES:
        raise ContractViolation(f"{context}.sourceStatus has an invalid enum")
    if envelope["lastSyncedAt"] is not None:
        _assert_utc_timestamp(envelope["lastSyncedAt"], f"{context}.lastSyncedAt")
    _assert_utc_timestamp(envelope["staleAfter"], f"{context}.staleAfter")
    _assert_uuid4(envelope["correlation_id"], f"{context}.correlation_id")
    return envelope


def _validate_server_record(value: Any, context: str = "server record") -> dict[str, Any]:
    record = _require_mapping(value, context)
    _assert_keys(record, {"request_hash", "canonical_response"}, {"request_hash", "canonical_response"}, context)
    if not isinstance(record["request_hash"], str) or not SHA256.fullmatch(record["request_hash"]):
        raise ContractViolation(f"{context}.request_hash must be server-computed sha256 hex")
    _validate_canonical_response(record["canonical_response"], f"{context}.canonical_response")
    return record


def _validate_mutation(value: Any) -> dict[str, Any]:
    example = _require_mapping(value, "mutation example")
    _assert_keys(
        example,
        {"schemaVersion", "kind", "operation", "request", "authenticated_context", "server_record", "correlation_id"},
        {"schemaVersion", "kind", "operation", "request", "authenticated_context", "server_record", "correlation_id"},
        "mutation example",
    )
    if example["schemaVersion"] != "planning.v1" or example["kind"] != "mutation_example" or example["operation"] != "create":
        raise ContractViolation("mutation example version, kind or operation is invalid")
    _validate_create_request(example["request"])
    _validate_authenticated_context(example["authenticated_context"])
    record = _validate_server_record(example["server_record"])
    if record["canonical_response"]["domain"] != "task":
        raise ContractViolation("create canonical response must be a task")
    _assert_uuid4(example["correlation_id"], "mutation example.correlation_id")
    return example


def _validate_edit_state_transition(value: Any) -> dict[str, Any]:
    example = _require_mapping(value, "edit/state transition example")
    _assert_keys(
        example,
        {"schemaVersion", "kind", "operation", "request", "authenticated_context", "server_record", "correlation_id"},
        {"schemaVersion", "kind", "operation", "request", "authenticated_context", "server_record", "correlation_id"},
        "edit/state transition example",
    )
    if example["schemaVersion"] != "planning.v1" or example["kind"] != "mutation_example" or example["operation"] != "complete":
        raise ContractViolation("edit/state transition example version, kind or operation is invalid")
    request = _require_mapping(example["request"], "edit/state transition request")
    _assert_keys(request, {"route", "headers", "body"}, {"route", "headers", "body"}, "edit/state transition request")
    match = re.fullmatch(r"/internal/planning/v1/tasks/([0-9a-f-]{36})/complete", request["route"])
    if match is None:
        raise ContractViolation("edit/state transition route must identify a task and fixed action")
    _assert_uuid4(match.group(1), "edit/state transition route object id")
    headers = _require_mapping(request["headers"], "edit/state transition headers")
    _assert_keys(headers, {"Idempotency-Key", "If-Match"}, {"Idempotency-Key", "If-Match"}, "edit/state transition headers")
    _assert_nonempty_string(headers["Idempotency-Key"], "edit/state transition Idempotency-Key")
    if not isinstance(headers["If-Match"], str) or not headers["If-Match"].isdigit() or int(headers["If-Match"]) < 1:
        raise ContractViolation("edit/state transition If-Match must be a positive version")
    body = _require_mapping(request["body"], "edit/state transition body")
    _assert_keys(body, set(), set(), "edit/state transition body")
    _validate_authenticated_context(example["authenticated_context"], "edit/state transition authenticated_context")
    record = _validate_server_record(example["server_record"], "edit/state transition server record")
    canonical = record["canonical_response"]
    if canonical["domain"] != "task" or canonical["object"]["status"] != "completed":
        raise ContractViolation("edit/state transition must return a completed task")
    if canonical["object"]["id"] != match.group(1):
        raise ContractViolation("edit/state transition response must target the route object")
    if canonical["object"]["version"] <= int(headers["If-Match"]):
        raise ContractViolation("edit/state transition response must advance the version")
    _assert_uuid4(example["correlation_id"], "edit/state transition correlation_id")
    return example


def _validate_error(value: Any) -> dict[str, Any]:
    envelope = _require_mapping(value, "error envelope")
    _assert_keys(
        envelope,
        {"schemaVersion", "kind", "http_status", "error", "sourceStatus", "lastSyncedAt", "staleAfter", "actor", "correlation_id"},
        {"schemaVersion", "kind", "http_status", "error", "sourceStatus", "lastSyncedAt", "staleAfter", "actor", "correlation_id"},
        "error envelope",
    )
    if envelope["schemaVersion"] != "planning.v1" or envelope["kind"] != "error":
        raise ContractViolation("error envelope version or kind is invalid")
    if envelope["http_status"] != 409:
        raise ContractViolation("conflict fixture must be HTTP 409")
    error = _require_mapping(envelope["error"], "error envelope.error")
    _assert_keys(error, {"code", "message", "details", "retryable"}, {"code", "message", "details", "retryable"}, "error envelope.error")
    if error["code"] != "version_conflict" or not isinstance(error["message"], str) or not error["message"]:
        raise ContractViolation("error code or message is invalid")
    if not isinstance(error["retryable"], bool):
        raise ContractViolation("error.retryable must be boolean")
    details = _require_mapping(error["details"], "error envelope.error.details")
    _assert_keys(details, {"domain", "object_id", "expected_version", "actual_version"}, {"domain", "object_id", "expected_version", "actual_version"}, "error details")
    if details["domain"] not in {"reminder", "task", "calendar_event"}:
        raise ContractViolation("error details domain is invalid")
    _assert_uuid4(details["object_id"], "error details.object_id")
    _assert_positive_version(details["expected_version"], "error details.expected_version")
    _assert_positive_version(details["actual_version"], "error details.actual_version")
    if details["actual_version"] <= details["expected_version"]:
        raise ContractViolation("error details actual version must be newer")
    if envelope["sourceStatus"] not in SOURCE_STATUS_VALUES:
        raise ContractViolation("error sourceStatus has an invalid enum")
    if envelope["lastSyncedAt"] is not None:
        _assert_utc_timestamp(envelope["lastSyncedAt"], "error lastSyncedAt")
    _assert_utc_timestamp(envelope["staleAfter"], "error staleAfter")
    _validate_actor(envelope["actor"], "error actor")
    _assert_uuid4(envelope["correlation_id"], "error correlation_id")
    return envelope


def _validate_alice_response(value: Any) -> dict[str, Any]:
    envelope = _require_mapping(value, "Alice response")
    _assert_keys(
        envelope,
        {"schemaVersion", "kind", "speech", "end_session", "pending_confirmation_id", "object", "correlation_id", "actor"},
        {"schemaVersion", "kind", "speech", "end_session", "pending_confirmation_id", "object", "correlation_id", "actor"},
        "Alice response",
    )
    if envelope["schemaVersion"] != "planning.v1" or envelope["kind"] not in {"answer", "confirmation_required", "created", "query_result", "error"}:
        raise ContractViolation("Alice response version or kind is invalid")
    _assert_nonempty_string(envelope["speech"], "Alice response.speech")
    if not isinstance(envelope["end_session"], bool):
        raise ContractViolation("Alice response.end_session must be boolean")
    if envelope["pending_confirmation_id"] is not None:
        _assert_uuid4(envelope["pending_confirmation_id"], "Alice response.pending_confirmation_id")
    if envelope["object"] is not None:
        domain = envelope["object"].get("domain") if isinstance(envelope["object"], dict) else None
        validator = {"reminder": _validate_reminder, "task": _validate_task, "calendar_event": _validate_event}.get(domain)
        if validator is None:
            raise ContractViolation("Alice response.object domain is invalid")
        validator(envelope["object"], "Alice response.object")
    _assert_uuid4(envelope["correlation_id"], "Alice response.correlation_id")
    _validate_actor(envelope["actor"], "Alice response.actor")
    return envelope


class PlanningV1ContractTests(unittest.TestCase):
    def test_valid_reminder_contract(self) -> None:
        reminder = _validate_reminder(_load("valid_reminder.json"))
        self.assertEqual(reminder["domain"], "reminder")
        self.assertEqual(reminder["status"], "pending")
        self.assertEqual(reminder["delivery_state"], "not_due")

    def test_valid_date_only_task_is_not_midnight(self) -> None:
        task = _validate_task(_load("valid_date_only_task.json"))
        self.assertEqual(task["domain"], "task")
        self.assertIn("due_date", task)
        self.assertNotIn("due_time", task)
        self.assertNotIn("T00:00:00", json.dumps(task))

    def test_valid_timed_task_contract(self) -> None:
        task = _validate_task(_load("valid_timed_task.json"))
        self.assertEqual(task["due_time"], "14:05")
        self.assertEqual(task["timezone"], "Europe/Moscow")

    def test_valid_event_contracts_preserve_distinct_representations(self) -> None:
        all_day = _validate_event(_load("valid_all_day_event.json"))
        timed = _validate_event(_load("valid_timed_event.json"))
        self.assertTrue(all_day["all_day"])
        self.assertIn("end_date_exclusive", all_day)
        self.assertFalse(timed["all_day"])
        self.assertIn("end_at_utc", timed)
        self.assertEqual(timed["provider_calendar_id"], "fixture-calendar")

    def test_source_and_freshness_metadata_contract(self) -> None:
        envelope = _validate_freshness_envelope(_load("source_freshness_metadata.json"))
        self.assertEqual(envelope["sourceStatus"], "current")
        self.assertEqual(envelope["lastSyncedAt"], "2026-08-19T08:59:00Z")
        self.assertEqual(envelope["staleAfter"], "2026-08-19T09:05:00Z")

    def test_versioned_mutation_and_idempotency_contract(self) -> None:
        mutation = _validate_mutation(_load("mutation_idempotency.json"))
        request = mutation["request"]
        self.assertEqual(request["headers"]["Idempotency-Key"], "fixture-idempotency-task-001")
        self.assertNotIn("request_hash", request["headers"])
        self.assertNotIn("audience", request["body"])
        for field in SERVER_MANAGED_CLIENT_FIELDS:
            self.assertNotIn(field, request["body"])
        self.assertEqual(mutation["authenticated_context"]["audience"], "panel-agent")
        self.assertIn("request_hash", mutation["server_record"])
        canonical = mutation["server_record"]["canonical_response"]["object"]
        self.assertEqual(canonical["version"], 1)
        for field in ("id", "version", "created_at", "updated_at", "audit_correlation_id", "source"):
            self.assertIn(field, canonical)

    def test_create_request_rejects_server_owned_fields(self) -> None:
        with self.assertRaises(ContractViolation):
            _validate_create_request(_load("invalid_create_server_fields.json"))

        base_request = _load("mutation_idempotency.json")["request"]
        for field in SERVER_MANAGED_CLIENT_FIELDS | {"source", "source_ref", "created_by"}:
            candidate = deepcopy(base_request)
            candidate["body"][field] = "client-supplied"
            with self.subTest(field=field):
                with self.assertRaises(ContractViolation):
                    _validate_create_request(candidate)

    def test_idempotency_key_and_audience_are_external_and_request_hash_is_internal(self) -> None:
        with self.assertRaises(ContractViolation):
            _validate_create_request(_load("invalid_client_request_hash.json"))

        candidate = deepcopy(_load("mutation_idempotency.json")["request"])
        candidate["body"]["audience"] = "operator"
        with self.assertRaises(ContractViolation):
            _validate_create_request(candidate)

    def test_edit_state_transition_requires_expected_version_and_returns_canonical_object(self) -> None:
        transition = _validate_edit_state_transition(_load("edit_state_transition.json"))
        request = transition["request"]
        self.assertEqual(request["headers"]["If-Match"], "3")
        self.assertEqual(request["body"], {})
        canonical = transition["server_record"]["canonical_response"]["object"]
        self.assertEqual(canonical["status"], "completed")
        self.assertEqual(canonical["version"], 4)

        missing_version = deepcopy(request)
        missing_version["headers"].pop("If-Match")
        invalid_transition = deepcopy(transition)
        invalid_transition["request"] = missing_version
        with self.assertRaises(ContractViolation):
            _validate_edit_state_transition(invalid_transition)

        full_object_request = deepcopy(request)
        full_object_request["body"] = canonical
        invalid_transition["request"] = full_object_request
        with self.assertRaises(ContractViolation):
            _validate_edit_state_transition(invalid_transition)

    def test_conflict_error_envelope_contract(self) -> None:
        error = _validate_error(_load("conflict_error_envelope.json"))
        self.assertEqual(error["http_status"], 409)
        self.assertEqual(error["error"]["code"], "version_conflict")

    def test_alice_response_envelope_contract(self) -> None:
        response = _validate_alice_response(_load("alice_created_response.json"))
        self.assertEqual(response["kind"], "created")
        self.assertIsNotNone(response["object"])

    def test_unknown_fields_are_rejected(self) -> None:
        with self.assertRaises(ContractViolation):
            _validate_reminder(_load("invalid_unknown_field.json"))

    def test_arbitrary_execution_fields_are_rejected(self) -> None:
        with self.assertRaises(ContractViolation):
            _validate_reminder(_load("invalid_ha_service_entity.json"))
        with self.assertRaises(ContractViolation):
            _validate_task(_load("invalid_command_path_url.json"))

    def test_invalid_enum_is_rejected(self) -> None:
        with self.assertRaises(ContractViolation):
            _validate_task(_load("invalid_enum.json"))

    def test_timed_and_all_day_fields_are_mutually_exclusive(self) -> None:
        with self.assertRaises(ContractViolation):
            _validate_event(_load("invalid_event_representation.json"))

    def test_invalid_timestamp_and_timezone_are_rejected(self) -> None:
        with self.assertRaises(ContractViolation):
            _validate_reminder(_load("invalid_timestamp_timezone.json"))


if __name__ == "__main__":
    unittest.main()
