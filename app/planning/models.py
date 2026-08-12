from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import Any, Literal, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.planning.errors import PlanningLocalTimeError, PlanningValidationError


AUDIENCES = frozenset({"ha", "panel-agent", "operator"})
ACTOR_TYPES = frozenset({"user", "service", "operator"})
SURFACES = frozenset({"ha", "panel-agent", "telegram", "operator", "system"})
SOURCE_VALUES = frozenset(
    {"alice", "telegram", "panel-agent", "operator", "ticktick", "calendar-provider", "system"}
)
SOURCE_BY_SURFACE = {
    "ha": "alice",
    "panel-agent": "panel-agent",
    "telegram": "telegram",
    "operator": "operator",
    "system": "system",
}
REMINDER_STATUSES = frozenset({"pending", "due", "completed", "cancelled"})
DELIVERY_STATES = frozenset({"not_due", "queued", "retrying", "delivered", "failed"})
TASK_PRIORITIES = frozenset({"none", "low", "normal", "high"})
TASK_STATUSES = frozenset({"open", "completed", "archived"})
EVENT_SYNC_STATES = frozenset({"local_only", "pending", "synced", "stale", "conflict", "error"})
OUTBOX_STATUSES = frozenset({"queued", "leased", "succeeded", "failed", "cancelled"})
REMINDER_DELIVERY_JOB_TYPE = "planning.reminder.delivery.v1"
REMINDER_OUTBOX_DEDUPE_PREFIX = "planning.reminder:"

UTC_TIMESTAMP_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"
)
LOCAL_TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?$")
SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


def new_uuid4() -> str:
    return str(uuid.uuid4())


def validate_uuid4(value: str, field: str = "id") -> str:
    if not isinstance(value, str):
        raise PlanningValidationError(f"{field} must be a UUIDv4")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise PlanningValidationError(f"{field} must be a UUIDv4") from exc
    if parsed.version != 4 or str(parsed) != value.lower():
        raise PlanningValidationError(f"{field} must be a UUIDv4")
    return value


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def validate_utc_timestamp(value: str, field: str = "timestamp") -> str:
    if not isinstance(value, str) or UTC_TIMESTAMP_PATTERN.fullmatch(value) is None:
        raise PlanningValidationError(f"{field} must be an RFC3339 UTC timestamp with Z suffix")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise PlanningValidationError(f"{field} must be a valid UTC timestamp") from exc
    if parsed.tzinfo != timezone.utc:
        raise PlanningValidationError(f"{field} must be UTC")
    return value


def validate_timezone(value: str, field: str = "timezone") -> str:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise PlanningValidationError(f"{field} must be an IANA timezone")
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise PlanningValidationError(f"{field} must be an IANA timezone") from exc
    return value


def validate_date(value: str, field: str = "date") -> str:
    if not isinstance(value, str):
        raise PlanningValidationError(f"{field} must be YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise PlanningValidationError(f"{field} must be YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise PlanningValidationError(f"{field} must be YYYY-MM-DD")
    return value


def validate_local_time(value: str, field: str = "time") -> str:
    if not isinstance(value, str) or LOCAL_TIME_PATTERN.fullmatch(value) is None:
        raise PlanningValidationError(f"{field} must be HH:MM or HH:MM:SS")
    return value


def resolve_local_datetime(
    *,
    local_date: str | date,
    local_time: str,
    timezone_name: str,
    field: str = "local_datetime",
) -> datetime:
    """Resolve one local wall-clock value without guessing across DST.

    ``zoneinfo`` accepts both sides of a fall-back transition through its
    ``fold`` flag and silently normalises spring-forward gaps.  Planning must
    not choose either side or normalise a nonexistent value, so both folds are
    round-tripped and exactly one valid instant is required.
    """

    if isinstance(local_date, date) and not isinstance(local_date, datetime):
        selected_date = local_date
    else:
        selected_date = date.fromisoformat(validate_date(str(local_date), f"{field}.date"))
    validate_local_time(local_time, f"{field}.time")
    validate_timezone(timezone_name, f"{field}.timezone")
    parts = [int(part) for part in local_time.split(":")]
    selected_time = time(parts[0], parts[1], parts[2] if len(parts) == 3 else 0)
    naive = datetime.combine(selected_date, selected_time)
    zone = ZoneInfo(timezone_name)
    candidates: list[datetime] = []
    for fold in (0, 1):
        local = naive.replace(tzinfo=zone, fold=fold)
        utc_value = local.astimezone(timezone.utc)
        round_trip = utc_value.astimezone(zone).replace(tzinfo=None)
        if round_trip == naive and utc_value not in candidates:
            candidates.append(utc_value)
    if not candidates:
        raise PlanningLocalTimeError(
            "nonexistent_local_time",
            f"{field} does not exist in {timezone_name} because of a DST transition",
        )
    if len(candidates) > 1:
        raise PlanningLocalTimeError(
            "ambiguous_local_time",
            f"{field} is ambiguous in {timezone_name}; an explicit disambiguation is required",
        )
    return candidates[0]


def validate_text(value: str, field: str, *, max_length: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()) or len(value) > max_length:
        qualifier = "non-empty " if not allow_empty else ""
        raise PlanningValidationError(f"{field} must be {qualifier}text of at most {max_length} characters")
    return value


def validate_optional_text(value: str | None, field: str, *, max_length: int) -> str | None:
    if value is None:
        return None
    return validate_text(value, field, max_length=max_length, allow_empty=True)


def validate_request_hash(value: str) -> str:
    if not isinstance(value, str) or SHA256_PATTERN.fullmatch(value) is None:
        raise PlanningValidationError("request_hash must be a trusted sha256: hexadecimal value")
    return value


def _copy_optional(data: dict[str, Any], **values: Any) -> dict[str, Any]:
    data.update(values)
    return data


@dataclass(frozen=True)
class MutationContext:
    """Trusted internal caller metadata; never populated from a canonical client object."""

    audience: str
    actor_id: str
    actor_type: str
    surface: str
    correlation_id: str | None = None
    source_ref: str | None = None

    def validate(self) -> "MutationContext":
        if self.audience not in AUDIENCES:
            raise PlanningValidationError("context.audience has an invalid enum")
        validate_text(self.actor_id, "context.actor_id", max_length=128)
        if self.actor_type not in ACTOR_TYPES:
            raise PlanningValidationError("context.actor_type has an invalid enum")
        if self.surface not in SURFACES:
            raise PlanningValidationError("context.surface has an invalid enum")
        if self.correlation_id is not None:
            validate_uuid4(self.correlation_id, "context.correlation_id")
        validate_optional_text(self.source_ref, "context.source_ref", max_length=256)
        return self

    @property
    def canonical_source(self) -> str:
        self.validate()
        return SOURCE_BY_SURFACE[self.surface]

    @property
    def audit_correlation_id(self) -> str:
        self.validate()
        return self.correlation_id or new_uuid4()


@dataclass(frozen=True)
class Reminder:
    id: str
    title: str
    due_at_utc: str
    timezone: str
    status: Literal["pending", "due", "completed", "cancelled"]
    source: str
    created_by: str
    delivery_state: Literal["not_due", "queued", "retrying", "delivered", "failed"]
    version: int
    created_at: str
    updated_at: str
    audit_correlation_id: str
    notes: str | None = None
    source_ref: str | None = None
    completed_at: str | None = None
    cancelled_at: str | None = None
    next_attempt_at: str | None = None
    final_failure_at: str | None = None
    deleted_at: str | None = None

    @property
    def domain(self) -> str:
        return "reminder"

    def to_dict(self) -> dict[str, Any]:
        return _copy_optional(
            {
                "id": self.id,
                "domain": self.domain,
                "title": self.title,
                "due_at_utc": self.due_at_utc,
                "timezone": self.timezone,
                "status": self.status,
                "source": self.source,
                "created_by": self.created_by,
                "delivery_state": self.delivery_state,
                "version": self.version,
                "created_at": self.created_at,
                "updated_at": self.updated_at,
                "audit_correlation_id": self.audit_correlation_id,
            },
            notes=self.notes,
            source_ref=self.source_ref,
            completed_at=self.completed_at,
            cancelled_at=self.cancelled_at,
            next_attempt_at=self.next_attempt_at,
            final_failure_at=self.final_failure_at,
            deleted_at=self.deleted_at,
        )


@dataclass(frozen=True)
class Task:
    id: str
    title: str
    priority: Literal["none", "low", "normal", "high"]
    status: Literal["open", "completed", "archived"]
    source: str
    version: int
    created_at: str
    updated_at: str
    audit_correlation_id: str
    notes: str | None = None
    due_date: str | None = None
    due_time: str | None = None
    timezone: str | None = None
    project_id: str | None = None
    source_ref: str | None = None
    completed_at: str | None = None
    archived_at: str | None = None
    deleted_at: str | None = None

    @property
    def domain(self) -> str:
        return "task"

    def to_dict(self) -> dict[str, Any]:
        return _copy_optional(
            {
                "id": self.id,
                "domain": self.domain,
                "title": self.title,
                "priority": self.priority,
                "status": self.status,
                "source": self.source,
                "version": self.version,
                "created_at": self.created_at,
                "updated_at": self.updated_at,
                "audit_correlation_id": self.audit_correlation_id,
            },
            notes=self.notes,
            due_date=self.due_date,
            due_time=self.due_time,
            timezone=self.timezone,
            project_id=self.project_id,
            source_ref=self.source_ref,
            completed_at=self.completed_at,
            archived_at=self.archived_at,
            deleted_at=self.deleted_at,
        )


@dataclass(frozen=True)
class CalendarEvent:
    id: str
    title: str
    all_day: bool
    timezone: str
    sync_state: Literal["local_only", "pending", "synced", "stale", "conflict", "error"]
    source: str
    version: int
    created_at: str
    updated_at: str
    audit_correlation_id: str
    notes: str | None = None
    location: str | None = None
    start_at_utc: str | None = None
    end_at_utc: str | None = None
    start_date: str | None = None
    end_date_exclusive: str | None = None
    recurrence_rule: str | None = None
    provider_id: str | None = None
    provider_calendar_id: str | None = None
    source_ref: str | None = None
    deleted_at: str | None = None

    @property
    def domain(self) -> str:
        return "calendar_event"

    def to_dict(self) -> dict[str, Any]:
        return _copy_optional(
            {
                "id": self.id,
                "domain": self.domain,
                "title": self.title,
                "all_day": self.all_day,
                "timezone": self.timezone,
                "sync_state": self.sync_state,
                "source": self.source,
                "version": self.version,
                "created_at": self.created_at,
                "updated_at": self.updated_at,
                "audit_correlation_id": self.audit_correlation_id,
            },
            notes=self.notes,
            location=self.location,
            start_at_utc=self.start_at_utc,
            end_at_utc=self.end_at_utc,
            start_date=self.start_date,
            end_date_exclusive=self.end_date_exclusive,
            recurrence_rule=self.recurrence_rule,
            provider_id=self.provider_id,
            provider_calendar_id=self.provider_calendar_id,
            source_ref=self.source_ref,
            deleted_at=self.deleted_at,
        )


@dataclass(frozen=True)
class Project:
    id: str
    name: str
    source: str
    version: int
    created_at: str
    updated_at: str
    audit_correlation_id: str
    notes: str | None = None
    source_ref: str | None = None
    deleted_at: str | None = None

    @property
    def domain(self) -> str:
        return "project"

    def to_dict(self) -> dict[str, Any]:
        return _copy_optional(
            {
                "id": self.id,
                "domain": self.domain,
                "name": self.name,
                "source": self.source,
                "version": self.version,
                "created_at": self.created_at,
                "updated_at": self.updated_at,
                "audit_correlation_id": self.audit_correlation_id,
            },
            notes=self.notes,
            source_ref=self.source_ref,
            deleted_at=self.deleted_at,
        )


@dataclass(frozen=True)
class IdempotencyClaim:
    audience: str
    key: str
    request_hash: str
    is_new: bool
    response_json: str | None
    response_status: int | None
    correlation_id: str | None

    @property
    def is_replay(self) -> bool:
        return not self.is_new and self.response_json is not None


@dataclass(frozen=True)
class OutboxJob:
    id: str
    job_type: str
    payload_version: int
    payload: Mapping[str, Any]
    status: Literal["queued", "leased", "succeeded", "failed", "cancelled"]
    available_at: str
    lease_owner: str | None
    lease_expires_at: str | None
    # Worker lease-claim count; required delivery attempts live in delivery_attempts.
    attempt_count: int
    created_at: str
    updated_at: str
    last_error: str | None = None
    correlation_id: str | None = None
    dedupe_key: str | None = None
    reminder_id: str | None = None
    lease_token: str | None = None
    attempt_window_started_at: str | None = None
    last_error_code: str | None = None
    delivery_cycle_id: str | None = None


@dataclass(frozen=True)
class DeliveryAttempt:
    id: str
    reminder_id: str
    channel: str
    attempt_number: int
    status: Literal["queued", "started", "succeeded", "failed"]
    started_at: str | None
    finished_at: str | None
    error_code: str | None
    error_message: str | None
    provider_receipt: str | None
    correlation_id: str
    created_at: str
    delivery_cycle_id: str | None = None


@dataclass(frozen=True)
class ProviderMapping:
    id: str
    domain: str
    object_id: str
    provider: str
    external_id: str
    external_calendar_id: str | None
    external_version: str | None
    external_etag: str | None
    last_exported_hash: str | None
    last_imported_hash: str | None
    deleted_at: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class SyncCursor:
    provider: str
    scope: str
    cursor: str | None
    last_synced_at: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class SyncConflict:
    id: str
    domain: str
    object_id: str
    provider: str
    external_id: str
    local_hash: str | None
    remote_hash: str | None
    details: Mapping[str, Any]
    status: Literal["open", "resolved", "ignored"]
    created_at: str
    resolved_at: str | None


def validate_reminder_shape(
    *,
    title: str,
    due_at_utc: str,
    timezone_name: str,
    status: str,
    delivery_state: str,
    notes: str | None,
) -> None:
    validate_text(title, "reminder.title", max_length=500)
    validate_utc_timestamp(due_at_utc, "reminder.due_at_utc")
    validate_timezone(timezone_name, "reminder.timezone")
    if status not in REMINDER_STATUSES:
        raise PlanningValidationError("reminder.status has an invalid enum")
    if delivery_state not in DELIVERY_STATES:
        raise PlanningValidationError("reminder.delivery_state has an invalid enum")
    validate_optional_text(notes, "reminder.notes", max_length=4000)


def validate_task_shape(
    *,
    title: str,
    priority: str,
    status: str,
    notes: str | None,
    due_date: str | None,
    due_time: str | None,
    timezone_name: str | None,
    project_id: str | None,
) -> None:
    validate_text(title, "task.title", max_length=500)
    validate_optional_text(notes, "task.notes", max_length=4000)
    if priority not in TASK_PRIORITIES:
        raise PlanningValidationError("task.priority has an invalid enum")
    if status not in TASK_STATUSES:
        raise PlanningValidationError("task.status has an invalid enum")
    if due_date is None:
        if due_time is not None or timezone_name is not None:
            raise PlanningValidationError("task due_time/timezone require due_date")
    elif due_time is None:
        validate_date(due_date, "task.due_date")
        if timezone_name is not None:
            raise PlanningValidationError("date-only task must not contain timezone")
    else:
        validate_date(due_date, "task.due_date")
        if timezone_name is None:
            raise PlanningValidationError("timed task requires timezone")
        resolve_local_datetime(
            local_date=due_date,
            local_time=due_time,
            timezone_name=timezone_name,
            field="task.due",
        )
    if project_id is not None:
        validate_uuid4(project_id, "task.project_id")


def validate_event_shape(
    *,
    all_day: bool,
    timezone_name: str,
    start_at_utc: str | None,
    end_at_utc: str | None,
    start_date: str | None,
    end_date_exclusive: str | None,
    sync_state: str,
    title: str,
    notes: str | None,
    location: str | None,
    recurrence_rule: str | None,
    provider_id: str | None,
    provider_calendar_id: str | None,
) -> None:
    validate_text(title, "calendar_event.title", max_length=500)
    validate_optional_text(notes, "calendar_event.notes", max_length=4000)
    validate_optional_text(location, "calendar_event.location", max_length=1000)
    validate_timezone(timezone_name, "calendar_event.timezone")
    if sync_state not in EVENT_SYNC_STATES:
        raise PlanningValidationError("calendar_event.sync_state has an invalid enum")
    if recurrence_rule is not None:
        raise PlanningValidationError("calendar_event.recurrence_rule is disabled in Planning v1")
    validate_optional_text(provider_id, "calendar_event.provider_id", max_length=256)
    validate_optional_text(provider_calendar_id, "calendar_event.provider_calendar_id", max_length=256)
    if all_day:
        if start_date is None or end_date_exclusive is None:
            raise PlanningValidationError("all-day event requires an exclusive date range")
        if any(value is not None for value in (start_at_utc, end_at_utc)):
            raise PlanningValidationError("all-day event cannot contain timed fields")
        validate_date(start_date, "calendar_event.start_date")
        validate_date(end_date_exclusive, "calendar_event.end_date_exclusive")
        if date.fromisoformat(end_date_exclusive) <= date.fromisoformat(start_date):
            raise PlanningValidationError("calendar_event.end_date_exclusive must be later than start_date")
    else:
        if start_at_utc is None or end_at_utc is None:
            raise PlanningValidationError("timed event requires start and end timestamps")
        if any(value is not None for value in (start_date, end_date_exclusive)):
            raise PlanningValidationError("timed event cannot contain all-day fields")
        validate_utc_timestamp(start_at_utc, "calendar_event.start_at_utc")
        validate_utc_timestamp(end_at_utc, "calendar_event.end_at_utc")
        start_dt = datetime.fromisoformat(start_at_utc[:-1] + "+00:00")
        end_dt = datetime.fromisoformat(end_at_utc[:-1] + "+00:00")
        if end_dt <= start_dt:
            raise PlanningValidationError("calendar_event.end_at_utc must be later than start_at_utc")
