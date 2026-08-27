from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import datetime
from typing import Any, Callable, Mapping, TypeVar

from app.planning.audit import AuditWriter, reject_secret_fields
from app.planning.db import PlanningDatabase
from app.planning.errors import (
    PlanningIdempotencyConflictError,
    PlanningIdempotencyInProgressError,
    PlanningLeaseLostError,
    PlanningNotFoundError,
    PlanningTransactionRequiredError,
    PlanningValidationError,
    PlanningVersionConflictError,
)
from app.planning.models import (
    AUDIENCES,
    CalendarEvent,
    DeliveryAttempt,
    IdempotencyClaim,
    MutationContext,
    OutboxJob,
    Project,
    ProviderMapping,
    Reminder,
    REMINDER_DELIVERY_JOB_TYPE,
    REMINDER_OUTBOX_DEDUPE_PREFIX,
    SyncConflict,
    SyncCursor,
    Task,
    TASK_VIEWS,
    new_uuid4,
    utc_now,
    validate_date,
    validate_event_shape,
    validate_request_hash,
    validate_text,
    validate_utc_timestamp,
    validate_uuid4,
    validate_reminder_shape,
    validate_task_shape,
)


_UNSET = object()
T = TypeVar("T")
_TABLES = {
    "projects": "projects",
    "reminders": "reminders",
    "tasks": "tasks",
    "calendar_events": "calendar_events",
}
_DELIVERY_TERMINAL_CHANNELS = frozenset({"alice", "jarvis", "telegram", "home_assistant", "iphone"})


def _validate_delivery_terminal_channel(channel: str, error_code: str) -> None:
    if channel not in _DELIVERY_TERMINAL_CHANNELS:
        raise PlanningValidationError("outbox terminal channel is not supported")
    validate_text(error_code, "outbox.delivery_terminal_channels.error_code", max_length=128)
    if not error_code or error_code[0] not in "abcdefghijklmnopqrstuvwxyz0123456789":
        raise PlanningValidationError("outbox terminal error code is invalid")
    if any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_.:-" for character in error_code):
        raise PlanningValidationError("outbox terminal error code is invalid")


def _optional_timestamp(value: str | None, field: str) -> None:
    if value is not None:
        validate_utc_timestamp(value, field)


def _row_to_reminder(row: sqlite3.Row) -> Reminder:
    return Reminder(
        id=str(row["id"]),
        title=str(row["title"]),
        due_at_utc=str(row["due_at_utc"]),
        timezone=str(row["timezone"]),
        status=str(row["status"]),  # type: ignore[arg-type]
        source=str(row["source"]),
        created_by=str(row["created_by"]),
        delivery_state=str(row["delivery_state"]),  # type: ignore[arg-type]
        version=int(row["version"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        audit_correlation_id=str(row["audit_correlation_id"]),
        notes=row["notes"],
        source_ref=row["source_ref"],
        completed_at=row["completed_at"],
        cancelled_at=row["cancelled_at"],
        next_attempt_at=row["next_attempt_at"],
        final_failure_at=row["final_failure_at"],
        deleted_at=row["deleted_at"],
    )


def _row_to_task(row: sqlite3.Row) -> Task:
    return Task(
        id=str(row["id"]),
        title=str(row["title"]),
        priority=str(row["priority"]),  # type: ignore[arg-type]
        status=str(row["status"]),  # type: ignore[arg-type]
        source=str(row["source"]),
        version=int(row["version"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        audit_correlation_id=str(row["audit_correlation_id"]),
        notes=row["notes"],
        due_date=row["due_date"],
        due_time=row["due_time"],
        timezone=row["timezone"],
        project_id=row["project_id"],
        source_ref=row["source_ref"],
        completed_at=row["completed_at"],
        archived_at=row["archived_at"],
        deleted_at=row["deleted_at"],
    )


def _row_to_event(row: sqlite3.Row) -> CalendarEvent:
    return CalendarEvent(
        id=str(row["id"]),
        title=str(row["title"]),
        all_day=bool(row["all_day"]),
        timezone=str(row["timezone"]),
        sync_state=str(row["sync_state"]),  # type: ignore[arg-type]
        source=str(row["source"]),
        version=int(row["version"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        audit_correlation_id=str(row["audit_correlation_id"]),
        notes=row["notes"],
        location=row["location"],
        start_at_utc=row["start_at_utc"],
        end_at_utc=row["end_at_utc"],
        start_date=row["start_date"],
        end_date_exclusive=row["end_date_exclusive"],
        recurrence_rule=row["recurrence_rule"],
        provider_id=row["provider_id"],
        provider_calendar_id=row["provider_calendar_id"],
        source_ref=row["source_ref"],
        deleted_at=row["deleted_at"],
    )


def _row_to_project(row: sqlite3.Row) -> Project:
    return Project(
        id=str(row["id"]),
        name=str(row["name"]),
        source=str(row["source"]),
        version=int(row["version"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        audit_correlation_id=str(row["audit_correlation_id"]),
        notes=row["notes"],
        source_ref=row["source_ref"],
        deleted_at=row["deleted_at"],
    )


def _decode_json(value: str, field: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise PlanningValidationError(f"stored {field} is not valid JSON") from exc


def _json_ready(value: Any) -> Any:
    if hasattr(value, "to_dict"):
        return _json_ready(value.to_dict())
    if isinstance(value, Mapping):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    return value


def _timestamp_order(start: str, end: str, field: str) -> None:
    start_dt = datetime.fromisoformat(start[:-1] + "+00:00")
    end_dt = datetime.fromisoformat(end[:-1] + "+00:00")
    if end_dt <= start_dt:
        raise PlanningValidationError(f"{field} must be later than its start")


class PlanningRepository:
    """Typed storage operations. Mutations always use guarded versions and audit."""

    def __init__(
        self,
        database: PlanningDatabase,
        audit: AuditWriter | None = None,
        *,
        now_fn: Callable[[], str] = utc_now,
    ) -> None:
        self.database = database
        self._now_fn = now_fn
        self.audit = audit or AuditWriter(database, now_fn=now_fn)

    def _now(self) -> str:
        return self._now_fn()

    @property
    def connection(self) -> sqlite3.Connection:
        return self.database.connection

    @staticmethod
    def _context(context: MutationContext) -> MutationContext:
        return context.validate()

    @staticmethod
    def _source_ref(context: MutationContext, source_ref: str | None) -> str | None:
        chosen = context.source_ref if source_ref is None else source_ref
        if chosen is not None:
            validate_text(chosen, "source_ref", max_length=256, allow_empty=True)
        return chosen

    def _record_audit(
        self,
        *,
        context: MutationContext,
        action: str,
        domain: str,
        object_id: str,
        old_version: int | None,
        new_version: int | None,
        before: Any,
        after: Any,
    ) -> None:
        self.audit.record(
            context=context,
            action=action,
            object_domain=domain,
            object_id=object_id,
            old_version=old_version,
            new_version=new_version,
            before=before,
            after=after,
            correlation_id=context.correlation_id,
        )

    def _require_row(self, table: str, object_id: str, domain: str) -> sqlite3.Row:
        table_name = _TABLES.get(table)
        if table_name is None:
            raise PlanningValidationError("repository table is not allowlisted")
        row = self.connection.execute(f"SELECT * FROM {table_name} WHERE id = ?", (object_id,)).fetchone()
        if row is None:
            raise PlanningNotFoundError(f"{domain} {object_id} was not found")
        return row

    def _raise_version_conflict(self, domain: str, object_id: str, expected_version: int, table: str) -> None:
        table_name = _TABLES.get(table)
        if table_name is None:
            raise PlanningValidationError("repository table is not allowlisted")
        row = self.connection.execute(f"SELECT version FROM {table_name} WHERE id = ?", (object_id,)).fetchone()
        actual = None if row is None else int(row[0])
        if actual is None:
            raise PlanningNotFoundError(f"{domain} {object_id} was not found")
        raise PlanningVersionConflictError(domain, object_id, expected_version, actual)

    def create_reminder(
        self,
        *,
        title: str,
        due_at_utc: str,
        timezone: str,
        context: MutationContext,
        notes: str | None = None,
        source_ref: str | None = None,
        outbox_job_type: str | None = None,
        outbox_payload: Mapping[str, Any] | None = None,
        outbox_dedupe_key: str | None = None,
    ) -> Reminder:
        context = self._context(context)
        validate_reminder_shape(
            title=title,
            due_at_utc=due_at_utc,
            timezone_name=timezone,
            status="pending",
            delivery_state="not_due",
            notes=notes,
        )
        source_ref = self._source_ref(context, source_ref)
        if outbox_payload is not None and outbox_job_type is None:
            raise PlanningValidationError("durable reminder delivery requires outbox_job_type")
        if outbox_payload is None and (outbox_job_type is not None or outbox_dedupe_key is not None):
            raise PlanningValidationError(
                "outbox_job_type and outbox_dedupe_key require an outbox payload"
            )
        object_id = new_uuid4()
        timestamp = self._now()
        reminder = Reminder(
            id=object_id,
            title=title,
            due_at_utc=due_at_utc,
            timezone=timezone,
            status="pending",
            source=context.canonical_source,
            created_by=context.actor_id,
            delivery_state="not_due",
            version=1,
            created_at=timestamp,
            updated_at=timestamp,
            audit_correlation_id=new_uuid4(),
            notes=notes,
            source_ref=source_ref,
        )
        with self.database.transaction():
            self.connection.execute(
                """
                INSERT INTO reminders(
                    id, title, notes, due_at_utc, timezone, status, source, source_ref,
                    created_by, completed_at, cancelled_at, delivery_state,
                    next_attempt_at, final_failure_at, version, created_at, updated_at,
                    audit_correlation_id, deleted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    reminder.id,
                    reminder.title,
                    reminder.notes,
                    reminder.due_at_utc,
                    reminder.timezone,
                    reminder.status,
                    reminder.source,
                    reminder.source_ref,
                    reminder.created_by,
                    reminder.completed_at,
                    reminder.cancelled_at,
                    reminder.delivery_state,
                    reminder.next_attempt_at,
                    reminder.final_failure_at,
                    reminder.version,
                    reminder.created_at,
                    reminder.updated_at,
                    reminder.audit_correlation_id,
                    reminder.deleted_at,
                ),
            )
            self._record_audit(
                context=context,
                action="create",
                domain=reminder.domain,
                object_id=reminder.id,
                old_version=None,
                new_version=reminder.version,
                before=None,
                after=reminder.to_dict(),
            )
            if outbox_payload is not None:
                payload = dict(outbox_payload)
                payload.setdefault("reminder_id", reminder.id)
                self.enqueue_outbox(
                    job_type=outbox_job_type or "",
                    payload=payload,
                    available_at=reminder.due_at_utc,
                    correlation_id=reminder.audit_correlation_id,
                    dedupe_key=outbox_dedupe_key or f"{REMINDER_OUTBOX_DEDUPE_PREFIX}{reminder.id}",
                    reminder_id=reminder.id,
                )
                self._record_audit(
                    context=context,
                    action="delivery_queued",
                    domain=reminder.domain,
                    object_id=reminder.id,
                    old_version=reminder.version,
                    new_version=reminder.version,
                    before=None,
                    after={"available_at": reminder.due_at_utc, "job_type": outbox_job_type},
                )
        return reminder

    def get_reminder(self, reminder_id: str) -> Reminder:
        validate_uuid4(reminder_id, "reminder.id")
        return _row_to_reminder(self._require_row("reminders", reminder_id, "reminder"))

    def list_reminders(
        self,
        *,
        state: str | None = None,
        from_utc: str | None = None,
        to_utc: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Reminder]:
        """Return a bounded, stable reminder view without tombstones."""

        if state is not None and state not in {"pending", "due", "completed", "cancelled"}:
            raise PlanningValidationError("reminder.state has an invalid enum")
        if from_utc is not None:
            validate_utc_timestamp(from_utc, "reminder.from")
        if to_utc is not None:
            validate_utc_timestamp(to_utc, "reminder.to")
        if from_utc is not None and to_utc is not None:
            _timestamp_order(from_utc, to_utc, "reminder.to")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1001:
            raise PlanningValidationError("reminder.list limit is out of range")
        if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 10_000:
            raise PlanningValidationError("reminder.list offset is out of range")

        # The ordinary/upcoming views hide tombstones.  An explicit
        # state=cancelled request is the approved audit view for cancelled
        # reminders, so it may include their logical tombstones.
        clauses = ["status = 'cancelled'"] if state == "cancelled" else ["deleted_at IS NULL"]
        values: list[Any] = []
        if state is not None:
            clauses.append("status = ?")
            values.append(state)
        if from_utc is not None:
            clauses.append("due_at_utc >= ?")
            values.append(from_utc)
        if to_utc is not None:
            clauses.append("due_at_utc < ?")
            values.append(to_utc)
        values.extend((limit, offset))
        rows = self.connection.execute(
            f"""
            SELECT * FROM reminders
            WHERE {' AND '.join(clauses)}
            ORDER BY due_at_utc, id
            LIMIT ? OFFSET ?
            """,
            values,
        ).fetchall()
        return [_row_to_reminder(row) for row in rows]

    def list_active_reminders(self, *, limit: int = 100, offset: int = 0) -> list[Reminder]:
        """Return pending/due reminders, including delivered-but-open rows."""

        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1001:
            raise PlanningValidationError("reminder.list limit is out of range")
        if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 10_000:
            raise PlanningValidationError("reminder.list offset is out of range")
        rows = self.connection.execute(
            """
            SELECT * FROM reminders
            WHERE deleted_at IS NULL
              AND status IN ('pending', 'due')
            ORDER BY due_at_utc, id
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
        return [_row_to_reminder(row) for row in rows]

    def list_due_reminders(self, *, as_of_utc: str) -> list[Reminder]:
        validate_utc_timestamp(as_of_utc, "as_of_utc")
        rows = self.connection.execute(
            """
            SELECT * FROM reminders
            WHERE deleted_at IS NULL
              AND status IN ('pending', 'due')
              AND due_at_utc <= ?
            ORDER BY due_at_utc, id
            """,
            (as_of_utc,),
        ).fetchall()
        return [_row_to_reminder(row) for row in rows]

    def update_reminder(
        self,
        reminder_id: str,
        *,
        expected_version: int,
        context: MutationContext,
        title: str | object = _UNSET,
        notes: str | None | object = _UNSET,
        due_at_utc: str | object = _UNSET,
        timezone: str | object = _UNSET,
        status: str | object = _UNSET,
        delivery_state: str | object = _UNSET,
        completed_at: str | None | object = _UNSET,
        cancelled_at: str | None | object = _UNSET,
        next_attempt_at: str | None | object = _UNSET,
        final_failure_at: str | None | object = _UNSET,
    ) -> Reminder:
        validate_uuid4(reminder_id, "reminder.id")
        if expected_version < 1:
            raise PlanningValidationError("expected_version must be positive")
        context = self._context(context)
        with self.database.transaction():
            current = _row_to_reminder(self._require_row("reminders", reminder_id, "reminder"))
            if current.version != expected_version:
                raise PlanningVersionConflictError("reminder", reminder_id, expected_version, current.version)
            new_status = current.status if status is _UNSET else str(status)
            new_delivery_state = current.delivery_state if delivery_state is _UNSET else str(delivery_state)
            new_completed_at = current.completed_at if completed_at is _UNSET else completed_at
            new_cancelled_at = current.cancelled_at if cancelled_at is _UNSET else cancelled_at
            new_deleted_at = current.deleted_at
            timestamp = self._now()
            if new_status == "completed" and new_completed_at is None:
                new_completed_at = timestamp
            if new_status == "cancelled" and new_cancelled_at is None:
                new_cancelled_at = timestamp
                new_deleted_at = timestamp
            updated = replace(
                current,
                title=current.title if title is _UNSET else str(title),
                notes=current.notes if notes is _UNSET else notes,
                due_at_utc=current.due_at_utc if due_at_utc is _UNSET else str(due_at_utc),
                timezone=current.timezone if timezone is _UNSET else str(timezone),
                status=new_status,  # type: ignore[arg-type]
                delivery_state=new_delivery_state,  # type: ignore[arg-type]
                completed_at=new_completed_at,
                cancelled_at=new_cancelled_at,
                next_attempt_at=current.next_attempt_at if next_attempt_at is _UNSET else next_attempt_at,
                final_failure_at=current.final_failure_at if final_failure_at is _UNSET else final_failure_at,
                version=current.version + 1,
                updated_at=timestamp,
                deleted_at=new_deleted_at,
            )
            validate_reminder_shape(
                title=updated.title,
                due_at_utc=updated.due_at_utc,
                timezone_name=updated.timezone,
                status=updated.status,
                delivery_state=updated.delivery_state,
                notes=updated.notes,
            )
            _optional_timestamp(updated.completed_at, "reminder.completed_at")
            _optional_timestamp(updated.cancelled_at, "reminder.cancelled_at")
            _optional_timestamp(updated.next_attempt_at, "reminder.next_attempt_at")
            _optional_timestamp(updated.final_failure_at, "reminder.final_failure_at")
            cursor = self.connection.execute(
                """
                UPDATE reminders SET
                    title = ?, notes = ?, due_at_utc = ?, timezone = ?, status = ?,
                    completed_at = ?, cancelled_at = ?, delivery_state = ?,
                    next_attempt_at = ?, final_failure_at = ?, version = ?, updated_at = ?,
                    deleted_at = ?
                WHERE id = ? AND version = ?
                """,
                (
                    updated.title,
                    updated.notes,
                    updated.due_at_utc,
                    updated.timezone,
                    updated.status,
                    updated.completed_at,
                    updated.cancelled_at,
                    updated.delivery_state,
                    updated.next_attempt_at,
                    updated.final_failure_at,
                    updated.version,
                    updated.updated_at,
                    updated.deleted_at,
                    reminder_id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                self._raise_version_conflict("reminder", reminder_id, expected_version, "reminders")
            if updated.status in {"completed", "cancelled"}:
                suppressed = self.connection.execute(
                    """
                    UPDATE outbox
                    SET status = 'cancelled', lease_owner = NULL, lease_expires_at = NULL,
                        lease_token = NULL, updated_at = ?
                    WHERE reminder_id = ? AND status IN ('queued', 'leased')
                    """,
                    (updated.updated_at, updated.id),
                )
                if suppressed.rowcount:
                    self._record_audit(
                        context=context,
                        action="delivery_suppressed",
                        domain=updated.domain,
                        object_id=updated.id,
                        old_version=current.version,
                        new_version=updated.version,
                        before={"status": current.status, "delivery_state": current.delivery_state},
                        after={"status": updated.status, "suppressed_jobs": suppressed.rowcount},
                    )
            self._record_audit(
                context=context,
                action="update" if updated.status == current.status else updated.status,
                domain=updated.domain,
                object_id=updated.id,
                old_version=current.version,
                new_version=updated.version,
                before=current.to_dict(),
                after=updated.to_dict(),
            )
            return updated

    def cancel_reminder(self, reminder_id: str, *, expected_version: int, context: MutationContext) -> Reminder:
        return self.update_reminder(
            reminder_id,
            expected_version=expected_version,
            context=context,
            status="cancelled",
        )

    def complete_reminder(self, reminder_id: str, *, expected_version: int, context: MutationContext) -> Reminder:
        return self.update_reminder(
            reminder_id,
            expected_version=expected_version,
            context=context,
            status="completed",
        )

    def create_project(
        self,
        *,
        name: str,
        context: MutationContext,
        notes: str | None = None,
        source_ref: str | None = None,
    ) -> Project:
        context = self._context(context)
        validate_text(name, "project.name", max_length=200)
        if notes is not None:
            validate_text(notes, "project.notes", max_length=4000, allow_empty=True)
        source_ref = self._source_ref(context, source_ref)
        timestamp = self._now()
        project = Project(
            id=new_uuid4(),
            name=name,
            source=context.canonical_source,
            version=1,
            created_at=timestamp,
            updated_at=timestamp,
            audit_correlation_id=new_uuid4(),
            notes=notes,
            source_ref=source_ref,
        )
        with self.database.transaction():
            self.connection.execute(
                """
                INSERT INTO projects(
                    id, name, notes, source, source_ref, version, created_at,
                    updated_at, audit_correlation_id, deleted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project.id,
                    project.name,
                    project.notes,
                    project.source,
                    project.source_ref,
                    project.version,
                    project.created_at,
                    project.updated_at,
                    project.audit_correlation_id,
                    project.deleted_at,
                ),
            )
            self._record_audit(
                context=context,
                action="create",
                domain=project.domain,
                object_id=project.id,
                old_version=None,
                new_version=project.version,
                before=None,
                after=project.to_dict(),
            )
        return project

    def get_project(self, project_id: str) -> Project:
        validate_uuid4(project_id, "project.id")
        return _row_to_project(self._require_row("projects", project_id, "project"))

    def list_projects(self, *, limit: int = 100, offset: int = 0) -> list[Project]:
        """Return a bounded, stable project view without tombstones."""

        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1001:
            raise PlanningValidationError("project.list limit is out of range")
        if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 10_000:
            raise PlanningValidationError("project.list offset is out of range")
        rows = self.connection.execute(
            """
            SELECT * FROM projects
            WHERE deleted_at IS NULL
            ORDER BY name COLLATE NOCASE, id
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
        return [_row_to_project(row) for row in rows]

    def update_project(
        self,
        project_id: str,
        *,
        expected_version: int,
        context: MutationContext,
        name: str | object = _UNSET,
        notes: str | None | object = _UNSET,
    ) -> Project:
        validate_uuid4(project_id, "project.id")
        context = self._context(context)
        if expected_version < 1:
            raise PlanningValidationError("expected_version must be positive")
        with self.database.transaction():
            current = _row_to_project(self._require_row("projects", project_id, "project"))
            if current.version != expected_version:
                raise PlanningVersionConflictError("project", project_id, expected_version, current.version)
            updated = replace(
                current,
                name=current.name if name is _UNSET else str(name),
                notes=current.notes if notes is _UNSET else notes,
                version=current.version + 1,
                updated_at=self._now(),
            )
            validate_text(updated.name, "project.name", max_length=200)
            if updated.notes is not None:
                validate_text(updated.notes, "project.notes", max_length=4000, allow_empty=True)
            cursor = self.connection.execute(
                "UPDATE projects SET name = ?, notes = ?, version = ?, updated_at = ? WHERE id = ? AND version = ?",
                (updated.name, updated.notes, updated.version, updated.updated_at, project_id, expected_version),
            )
            if cursor.rowcount != 1:
                self._raise_version_conflict("project", project_id, expected_version, "projects")
            self._record_audit(
                context=context,
                action="update",
                domain=updated.domain,
                object_id=updated.id,
                old_version=current.version,
                new_version=updated.version,
                before=current.to_dict(),
                after=updated.to_dict(),
            )
            return updated

    def delete_project(
        self,
        project_id: str,
        *,
        expected_version: int,
        context: MutationContext,
    ) -> Project:
        validate_uuid4(project_id, "project.id")
        if expected_version < 1:
            raise PlanningValidationError("expected_version must be positive")
        context = self._context(context)
        with self.database.transaction():
            current = _row_to_project(self._require_row("projects", project_id, "project"))
            if current.version != expected_version:
                raise PlanningVersionConflictError("project", project_id, expected_version, current.version)
            deleted_at = self._now()
            updated = replace(current, version=current.version + 1, updated_at=deleted_at, deleted_at=deleted_at)
            cursor = self.connection.execute(
                "UPDATE projects SET version = ?, updated_at = ?, deleted_at = ? WHERE id = ? AND version = ?",
                (updated.version, updated.updated_at, updated.deleted_at, project_id, expected_version),
            )
            if cursor.rowcount != 1:
                self._raise_version_conflict("project", project_id, expected_version, "projects")
            self._record_audit(
                context=context,
                action="delete",
                domain=updated.domain,
                object_id=updated.id,
                old_version=current.version,
                new_version=updated.version,
                before=current.to_dict(),
                after=updated.to_dict(),
            )
            return updated

    def create_task(
        self,
        *,
        title: str,
        context: MutationContext,
        notes: str | None = None,
        due_date: str | None = None,
        due_time: str | None = None,
        timezone: str | None = None,
        priority: str = "none",
        project_id: str | None = None,
        source_ref: str | None = None,
    ) -> Task:
        context = self._context(context)
        validate_task_shape(
            title=title,
            priority=priority,
            status="open",
            notes=notes,
            due_date=due_date,
            due_time=due_time,
            timezone_name=timezone,
            project_id=project_id,
        )
        source_ref = self._source_ref(context, source_ref)
        timestamp = self._now()
        task = Task(
            id=new_uuid4(),
            title=title,
            priority=priority,  # type: ignore[arg-type]
            status="open",
            source=context.canonical_source,
            version=1,
            created_at=timestamp,
            updated_at=timestamp,
            audit_correlation_id=new_uuid4(),
            notes=notes,
            due_date=due_date,
            due_time=due_time,
            timezone=timezone,
            project_id=project_id,
            source_ref=source_ref,
        )
        with self.database.transaction():
            self.connection.execute(
                """
                INSERT INTO tasks(
                    id, title, notes, due_date, due_time, timezone, priority, project_id,
                    status, source, source_ref, completed_at, archived_at, version,
                    created_at, updated_at, audit_correlation_id, deleted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.id,
                    task.title,
                    task.notes,
                    task.due_date,
                    task.due_time,
                    task.timezone,
                    task.priority,
                    task.project_id,
                    task.status,
                    task.source,
                    task.source_ref,
                    task.completed_at,
                    task.archived_at,
                    task.version,
                    task.created_at,
                    task.updated_at,
                    task.audit_correlation_id,
                    task.deleted_at,
                ),
            )
            self._record_audit(
                context=context,
                action="create",
                domain=task.domain,
                object_id=task.id,
                old_version=None,
                new_version=task.version,
                before=None,
                after=task.to_dict(),
            )
        return task

    def get_task(self, task_id: str) -> Task:
        validate_uuid4(task_id, "task.id")
        return _row_to_task(self._require_row("tasks", task_id, "task"))

    def list_tasks_due(self, *, on_or_before: str, include_completed: bool = False) -> list[Task]:
        validate_date(on_or_before, "on_or_before")
        statuses = ("open", "completed") if include_completed else ("open",)
        placeholders = ",".join("?" for _ in statuses)
        rows = self.connection.execute(
            f"""
            SELECT * FROM tasks
            WHERE deleted_at IS NULL
              AND status IN ({placeholders})
              AND due_date IS NOT NULL
              AND due_date <= ?
            ORDER BY due_date, COALESCE(due_time, '99:99:99'), id
            """,
            (*statuses, on_or_before),
        ).fetchall()
        return [_row_to_task(row) for row in rows]

    def list_tasks(
        self,
        *,
        view: str,
        today: str,
        project_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Task]:
        """Return one of the explicit canonical task list views."""

        if view not in TASK_VIEWS:
            raise PlanningValidationError("task.view has an invalid enum")
        validate_date(today, "task.today")
        if project_id is not None:
            validate_uuid4(project_id, "task.project_id")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1001:
            raise PlanningValidationError("task.list limit is out of range")
        if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 10_000:
            raise PlanningValidationError("task.list offset is out of range")

        if view == "undated":
            clauses = ["deleted_at IS NULL", "status = 'open'", "due_date IS NULL"]
            values: list[Any] = []
            ordering = "created_at, id"
        else:
            if view == "today":
                date_clause = "due_date = ?"
            elif view == "overdue":
                date_clause = "due_date < ?"
            else:
                date_clause = "due_date > ?"
            clauses = ["deleted_at IS NULL", "status = 'open'", "due_date IS NOT NULL", date_clause]
            values = [today]
            ordering = "due_date, CASE WHEN due_time IS NULL THEN 1 ELSE 0 END, due_time, id"
        if project_id is not None:
            clauses.append("project_id = ?")
            values.append(project_id)
        values.extend((limit, offset))
        rows = self.connection.execute(
            f"""
            SELECT * FROM tasks
            WHERE {' AND '.join(clauses)}
            ORDER BY {ordering}
            LIMIT ? OFFSET ?
            """,
            values,
        ).fetchall()
        return [_row_to_task(row) for row in rows]

    def update_task(
        self,
        task_id: str,
        *,
        expected_version: int,
        context: MutationContext,
        title: str | object = _UNSET,
        notes: str | None | object = _UNSET,
        due_date: str | None | object = _UNSET,
        due_time: str | None | object = _UNSET,
        timezone: str | None | object = _UNSET,
        priority: str | object = _UNSET,
        project_id: str | None | object = _UNSET,
        status: str | object = _UNSET,
    ) -> Task:
        validate_uuid4(task_id, "task.id")
        if expected_version < 1:
            raise PlanningValidationError("expected_version must be positive")
        context = self._context(context)
        with self.database.transaction():
            current = _row_to_task(self._require_row("tasks", task_id, "task"))
            if current.version != expected_version:
                raise PlanningVersionConflictError("task", task_id, expected_version, current.version)
            new_status = current.status if status is _UNSET else str(status)
            if current.status == "archived" and new_status != "archived":
                raise PlanningValidationError("archived tasks cannot be reopened")
            timestamp = self._now()
            completed_at = current.completed_at
            archived_at = current.archived_at
            deleted_at = current.deleted_at
            if new_status == "completed" and completed_at is None:
                completed_at = timestamp
            if new_status == "archived" and archived_at is None:
                archived_at = timestamp
                deleted_at = timestamp
            updated = replace(
                current,
                title=current.title if title is _UNSET else str(title),
                notes=current.notes if notes is _UNSET else notes,
                due_date=current.due_date if due_date is _UNSET else due_date,
                due_time=current.due_time if due_time is _UNSET else due_time,
                timezone=current.timezone if timezone is _UNSET else timezone,
                priority=current.priority if priority is _UNSET else str(priority),  # type: ignore[arg-type]
                project_id=current.project_id if project_id is _UNSET else project_id,
                status=new_status,  # type: ignore[arg-type]
                completed_at=completed_at,
                archived_at=archived_at,
                deleted_at=deleted_at,
                version=current.version + 1,
                updated_at=timestamp,
            )
            validate_task_shape(
                title=updated.title,
                priority=updated.priority,
                status=updated.status,
                notes=updated.notes,
                due_date=updated.due_date,
                due_time=updated.due_time,
                timezone_name=updated.timezone,
                project_id=updated.project_id,
            )
            _optional_timestamp(updated.completed_at, "task.completed_at")
            _optional_timestamp(updated.archived_at, "task.archived_at")
            cursor = self.connection.execute(
                """
                UPDATE tasks SET
                    title = ?, notes = ?, due_date = ?, due_time = ?, timezone = ?,
                    priority = ?, project_id = ?, status = ?, completed_at = ?,
                    archived_at = ?, version = ?, updated_at = ?, deleted_at = ?
                WHERE id = ? AND version = ?
                """,
                (
                    updated.title,
                    updated.notes,
                    updated.due_date,
                    updated.due_time,
                    updated.timezone,
                    updated.priority,
                    updated.project_id,
                    updated.status,
                    updated.completed_at,
                    updated.archived_at,
                    updated.version,
                    updated.updated_at,
                    updated.deleted_at,
                    task_id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                self._raise_version_conflict("task", task_id, expected_version, "tasks")
            self._record_audit(
                context=context,
                action="update" if updated.status == current.status else updated.status,
                domain=updated.domain,
                object_id=updated.id,
                old_version=current.version,
                new_version=updated.version,
                before=current.to_dict(),
                after=updated.to_dict(),
            )
            return updated

    def complete_task(self, task_id: str, *, expected_version: int, context: MutationContext) -> Task:
        return self.update_task(task_id, expected_version=expected_version, context=context, status="completed")

    def archive_task(self, task_id: str, *, expected_version: int, context: MutationContext) -> Task:
        return self.update_task(task_id, expected_version=expected_version, context=context, status="archived")

    def create_calendar_event(
        self,
        *,
        title: str,
        all_day: bool,
        timezone: str,
        context: MutationContext,
        notes: str | None = None,
        location: str | None = None,
        start_at_utc: str | None = None,
        end_at_utc: str | None = None,
        start_date: str | None = None,
        end_date_exclusive: str | None = None,
        recurrence_rule: str | None = None,
        provider_id: str | None = None,
        provider_calendar_id: str | None = None,
        sync_state: str = "local_only",
        source_ref: str | None = None,
    ) -> CalendarEvent:
        context = self._context(context)
        validate_event_shape(
            all_day=all_day,
            timezone_name=timezone,
            start_at_utc=start_at_utc,
            end_at_utc=end_at_utc,
            start_date=start_date,
            end_date_exclusive=end_date_exclusive,
            sync_state=sync_state,
            title=title,
            notes=notes,
            location=location,
            recurrence_rule=recurrence_rule,
            provider_id=provider_id,
            provider_calendar_id=provider_calendar_id,
        )
        if not all_day:
            assert start_at_utc is not None and end_at_utc is not None
            _timestamp_order(start_at_utc, end_at_utc, "calendar_event.end_at_utc")
        source_ref = self._source_ref(context, source_ref)
        timestamp = self._now()
        event = CalendarEvent(
            id=new_uuid4(),
            title=title,
            all_day=all_day,
            timezone=timezone,
            sync_state=sync_state,  # type: ignore[arg-type]
            source=context.canonical_source,
            version=1,
            created_at=timestamp,
            updated_at=timestamp,
            audit_correlation_id=new_uuid4(),
            notes=notes,
            location=location,
            start_at_utc=start_at_utc,
            end_at_utc=end_at_utc,
            start_date=start_date,
            end_date_exclusive=end_date_exclusive,
            recurrence_rule=recurrence_rule,
            provider_id=provider_id,
            provider_calendar_id=provider_calendar_id,
            source_ref=source_ref,
        )
        with self.database.transaction():
            self.connection.execute(
                """
                INSERT INTO calendar_events(
                    id, title, notes, location, all_day, start_at_utc, end_at_utc,
                    start_date, end_date_exclusive, timezone, recurrence_rule,
                    provider_id, provider_calendar_id, sync_state, source, source_ref,
                    version, created_at, updated_at, audit_correlation_id, deleted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.id,
                    event.title,
                    event.notes,
                    event.location,
                    int(event.all_day),
                    event.start_at_utc,
                    event.end_at_utc,
                    event.start_date,
                    event.end_date_exclusive,
                    event.timezone,
                    event.recurrence_rule,
                    event.provider_id,
                    event.provider_calendar_id,
                    event.sync_state,
                    event.source,
                    event.source_ref,
                    event.version,
                    event.created_at,
                    event.updated_at,
                    event.audit_correlation_id,
                    event.deleted_at,
                ),
            )
            self._record_audit(
                context=context,
                action="create",
                domain=event.domain,
                object_id=event.id,
                old_version=None,
                new_version=event.version,
                before=None,
                after=event.to_dict(),
            )
        return event

    def get_calendar_event(self, event_id: str) -> CalendarEvent:
        validate_uuid4(event_id, "calendar_event.id")
        return _row_to_event(self._require_row("calendar_events", event_id, "calendar_event"))

    def list_calendar_events(
        self,
        *,
        from_utc: str,
        to_utc: str,
        from_local_date: str | None = None,
        to_local_date: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[CalendarEvent]:
        validate_utc_timestamp(from_utc, "from_utc")
        validate_utc_timestamp(to_utc, "to_utc")
        _timestamp_order(from_utc, to_utc, "to_utc")
        from_local_date = from_local_date or from_utc[:10]
        to_local_date = to_local_date or to_utc[:10]
        validate_date(from_local_date, "from_local_date")
        validate_date(to_local_date, "to_local_date")
        if to_local_date <= from_local_date:
            raise PlanningValidationError("to_local_date must be later than from_local_date")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1001:
            raise PlanningValidationError("calendar_event.list limit is out of range")
        if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 10_000:
            raise PlanningValidationError("calendar_event.list offset is out of range")
        rows = self.connection.execute(
            """
            SELECT * FROM calendar_events
            WHERE deleted_at IS NULL
              AND (
                    (all_day = 0 AND start_at_utc < ? AND end_at_utc > ?)
                    OR
                    (all_day = 1 AND start_date < ? AND end_date_exclusive > ?)
                  )
            ORDER BY all_day DESC, COALESCE(start_date, start_at_utc), id
            LIMIT ? OFFSET ?
            """,
            (to_utc, from_utc, to_local_date, from_local_date, limit, offset),
        ).fetchall()
        return [_row_to_event(row) for row in rows]

    def update_calendar_event(
        self,
        event_id: str,
        *,
        expected_version: int,
        context: MutationContext,
        title: str | object = _UNSET,
        notes: str | None | object = _UNSET,
        location: str | None | object = _UNSET,
        all_day: bool | object = _UNSET,
        timezone: str | object = _UNSET,
        start_at_utc: str | None | object = _UNSET,
        end_at_utc: str | None | object = _UNSET,
        start_date: str | None | object = _UNSET,
        end_date_exclusive: str | None | object = _UNSET,
        recurrence_rule: str | None | object = _UNSET,
        provider_id: str | None | object = _UNSET,
        provider_calendar_id: str | None | object = _UNSET,
        sync_state: str | object = _UNSET,
    ) -> CalendarEvent:
        validate_uuid4(event_id, "calendar_event.id")
        if expected_version < 1:
            raise PlanningValidationError("expected_version must be positive")
        context = self._context(context)
        with self.database.transaction():
            current = _row_to_event(self._require_row("calendar_events", event_id, "calendar_event"))
            if current.version != expected_version:
                raise PlanningVersionConflictError("calendar_event", event_id, expected_version, current.version)
            updated = replace(
                current,
                title=current.title if title is _UNSET else str(title),
                notes=current.notes if notes is _UNSET else notes,
                location=current.location if location is _UNSET else location,
                all_day=current.all_day if all_day is _UNSET else bool(all_day),
                timezone=current.timezone if timezone is _UNSET else str(timezone),
                start_at_utc=current.start_at_utc if start_at_utc is _UNSET else start_at_utc,
                end_at_utc=current.end_at_utc if end_at_utc is _UNSET else end_at_utc,
                start_date=current.start_date if start_date is _UNSET else start_date,
                end_date_exclusive=current.end_date_exclusive if end_date_exclusive is _UNSET else end_date_exclusive,
                recurrence_rule=current.recurrence_rule if recurrence_rule is _UNSET else recurrence_rule,
                provider_id=current.provider_id if provider_id is _UNSET else provider_id,
                provider_calendar_id=current.provider_calendar_id
                if provider_calendar_id is _UNSET
                else provider_calendar_id,
                sync_state=current.sync_state if sync_state is _UNSET else str(sync_state),  # type: ignore[arg-type]
                version=current.version + 1,
                updated_at=self._now(),
            )
            validate_event_shape(
                all_day=updated.all_day,
                timezone_name=updated.timezone,
                start_at_utc=updated.start_at_utc,
                end_at_utc=updated.end_at_utc,
                start_date=updated.start_date,
                end_date_exclusive=updated.end_date_exclusive,
                sync_state=updated.sync_state,
                title=updated.title,
                notes=updated.notes,
                location=updated.location,
                recurrence_rule=updated.recurrence_rule,
                provider_id=updated.provider_id,
                provider_calendar_id=updated.provider_calendar_id,
            )
            if not updated.all_day:
                assert updated.start_at_utc is not None and updated.end_at_utc is not None
                _timestamp_order(updated.start_at_utc, updated.end_at_utc, "calendar_event.end_at_utc")
            cursor = self.connection.execute(
                """
                UPDATE calendar_events SET
                    title = ?, notes = ?, location = ?, all_day = ?, start_at_utc = ?,
                    end_at_utc = ?, start_date = ?, end_date_exclusive = ?, timezone = ?,
                    recurrence_rule = ?, provider_id = ?, provider_calendar_id = ?,
                    sync_state = ?, version = ?, updated_at = ?
                WHERE id = ? AND version = ?
                """,
                (
                    updated.title,
                    updated.notes,
                    updated.location,
                    int(updated.all_day),
                    updated.start_at_utc,
                    updated.end_at_utc,
                    updated.start_date,
                    updated.end_date_exclusive,
                    updated.timezone,
                    updated.recurrence_rule,
                    updated.provider_id,
                    updated.provider_calendar_id,
                    updated.sync_state,
                    updated.version,
                    updated.updated_at,
                    event_id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                self._raise_version_conflict("calendar_event", event_id, expected_version, "calendar_events")
            self._record_audit(
                context=context,
                action="update",
                domain=updated.domain,
                object_id=updated.id,
                old_version=current.version,
                new_version=updated.version,
                before=current.to_dict(),
                after=updated.to_dict(),
            )
            return updated

    def delete_calendar_event(
        self,
        event_id: str,
        *,
        expected_version: int,
        context: MutationContext,
    ) -> CalendarEvent:
        validate_uuid4(event_id, "calendar_event.id")
        context = self._context(context)
        with self.database.transaction():
            current = _row_to_event(self._require_row("calendar_events", event_id, "calendar_event"))
            if current.version != expected_version:
                raise PlanningVersionConflictError("calendar_event", event_id, expected_version, current.version)
            deleted_at = self._now()
            updated = replace(current, version=current.version + 1, updated_at=deleted_at, deleted_at=deleted_at)
            cursor = self.connection.execute(
                "UPDATE calendar_events SET version = ?, updated_at = ?, deleted_at = ? WHERE id = ? AND version = ?",
                (updated.version, updated.updated_at, updated.deleted_at, event_id, expected_version),
            )
            if cursor.rowcount != 1:
                self._raise_version_conflict("calendar_event", event_id, expected_version, "calendar_events")
            self._record_audit(
                context=context,
                action="delete",
                domain=updated.domain,
                object_id=updated.id,
                old_version=current.version,
                new_version=updated.version,
                before=current.to_dict(),
                after=updated.to_dict(),
            )
            return updated

    def claim_idempotency(self, *, audience: str, key: str, request_hash: str) -> IdempotencyClaim:
        """Claim a key inside the caller's transaction before mutating domain state."""

        if not self.database.in_transaction:
            raise PlanningTransactionRequiredError(
                "idempotency claims require a surrounding transaction so the response is atomic"
            )
        if audience not in AUDIENCES:
            raise PlanningValidationError("idempotency.audience has an invalid enum")
        validate_text(audience, "idempotency.audience", max_length=64)
        validate_text(key, "idempotency.key", max_length=256)
        validate_request_hash(request_hash)
        timestamp = self._now()
        cursor = self.connection.execute(
            """
            INSERT INTO idempotency_keys(
                audience, key, request_hash, response_json, response_status,
                created_at, updated_at, expires_at, correlation_id
            ) VALUES (?, ?, ?, NULL, NULL, ?, ?, NULL, NULL)
            ON CONFLICT(audience, key) DO NOTHING
            """,
            (audience, key, request_hash, timestamp, timestamp),
        )
        if cursor.rowcount == 1:
            return IdempotencyClaim(audience, key, request_hash, True, None, None, None)
        row = self.connection.execute(
            "SELECT request_hash, response_json, response_status, correlation_id FROM idempotency_keys WHERE audience = ? AND key = ?",
            (audience, key),
        ).fetchone()
        if row is None:
            raise PlanningNotFoundError("idempotency claim disappeared during transaction")
        if str(row["request_hash"]) != request_hash:
            raise PlanningIdempotencyConflictError(audience, key)
        return IdempotencyClaim(
            audience=audience,
            key=key,
            request_hash=request_hash,
            is_new=False,
            response_json=row["response_json"],
            response_status=row["response_status"],
            correlation_id=row["correlation_id"],
        )

    def store_idempotency_response(
        self,
        *,
        audience: str,
        key: str,
        request_hash: str,
        response: Any,
        response_status: int = 200,
        correlation_id: str | None = None,
        expires_at: str | None = None,
    ) -> str:
        if not self.database.in_transaction:
            raise PlanningTransactionRequiredError("idempotency response requires a surrounding transaction")
        if audience not in AUDIENCES:
            raise PlanningValidationError("idempotency.audience has an invalid enum")
        validate_text(key, "idempotency.key", max_length=256)
        validate_request_hash(request_hash)
        if response_status < 100 or response_status > 599:
            raise PlanningValidationError("idempotency.response_status is out of range")
        if correlation_id is not None:
            validate_uuid4(correlation_id, "idempotency.correlation_id")
        if expires_at is not None:
            validate_utc_timestamp(expires_at, "idempotency.expires_at")
        try:
            canonical_response = _json_ready(response)
            reject_secret_fields(canonical_response, field="idempotency.response")
            response_json = json.dumps(canonical_response, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise PlanningValidationError("idempotency response must be JSON-serializable") from exc
        if len(response_json) > 1_000_000:
            raise PlanningValidationError("idempotency response is too large")
        cursor = self.connection.execute(
            """
            UPDATE idempotency_keys SET
                response_json = ?, response_status = ?, updated_at = ?,
                expires_at = ?, correlation_id = ?
            WHERE audience = ? AND key = ? AND request_hash = ? AND response_json IS NULL
            """,
            (
                response_json,
                response_status,
                self._now(),
                expires_at,
                correlation_id,
                audience,
                key,
                request_hash,
            ),
        )
        if cursor.rowcount != 1:
            row = self.connection.execute(
                "SELECT request_hash, response_json FROM idempotency_keys WHERE audience = ? AND key = ?",
                (audience, key),
            ).fetchone()
            if row is None:
                raise PlanningNotFoundError("idempotency key was not claimed")
            if str(row["request_hash"]) != request_hash:
                raise PlanningIdempotencyConflictError(audience, key)
            if row["response_json"] is not None:
                return str(row["response_json"])
            raise PlanningIdempotencyInProgressError(audience, key)
        return response_json

    def execute_idempotent(
        self,
        *,
        audience: str,
        key: str,
        request_hash: str,
        mutation: Callable[[], T],
        response_status: int = 200,
        correlation_id: str | None = None,
        expires_at: str | None = None,
    ) -> T | Any:
        """Run a trusted mutation and persist its canonical response atomically."""

        with self.database.transaction():
            claim = self.claim_idempotency(audience=audience, key=key, request_hash=request_hash)
            if claim.is_replay:
                assert claim.response_json is not None
                return _decode_json(claim.response_json, "idempotency response")
            if not claim.is_new:
                raise PlanningIdempotencyInProgressError(audience, key)
            result = mutation()
            canonical_response = _json_ready(result)
            self.store_idempotency_response(
                audience=audience,
                key=key,
                request_hash=request_hash,
                response=canonical_response,
                response_status=response_status,
                correlation_id=correlation_id,
                expires_at=expires_at,
            )
            return canonical_response

    def enqueue_outbox(
        self,
        *,
        job_type: str,
        payload: Mapping[str, Any],
        payload_version: int = 1,
        available_at: str | None = None,
        lease_owner: str | None = None,
        lease_expires_at: str | None = None,
        correlation_id: str | None = None,
        dedupe_key: str | None = None,
        reminder_id: str | None = None,
    ) -> OutboxJob:
        if not self.database.in_transaction:
            raise PlanningTransactionRequiredError("outbox enqueue requires a surrounding transaction")
        validate_text(job_type, "outbox.job_type", max_length=128)
        if payload_version < 1:
            raise PlanningValidationError("outbox.payload_version must be positive")
        if not isinstance(payload, Mapping):
            raise PlanningValidationError("outbox.payload must be an object")
        try:
            safe_payload = _json_ready(payload)
            reject_secret_fields(safe_payload, field="outbox.payload")
            payload_json = json.dumps(safe_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise PlanningValidationError("outbox.payload must be JSON-serializable") from exc
        if len(payload_json) > 1_048_576:
            raise PlanningValidationError("outbox.payload is too large")
        available_at = available_at or self._now()
        validate_utc_timestamp(available_at, "outbox.available_at")
        if lease_expires_at is not None:
            validate_utc_timestamp(lease_expires_at, "outbox.lease_expires_at")
        if correlation_id is not None:
            validate_uuid4(correlation_id, "outbox.correlation_id")
        if dedupe_key is not None:
            validate_text(dedupe_key, "outbox.dedupe_key", max_length=256)
        if reminder_id is not None:
            validate_uuid4(reminder_id, "outbox.reminder_id")
        timestamp = self._now()
        job = OutboxJob(
            id=new_uuid4(),
            job_type=job_type,
            payload_version=payload_version,
            payload=payload,
            status="queued",
            available_at=available_at,
            lease_owner=lease_owner,
            lease_expires_at=lease_expires_at,
            attempt_count=0,
            created_at=timestamp,
            updated_at=timestamp,
            correlation_id=correlation_id,
            dedupe_key=dedupe_key,
            reminder_id=reminder_id,
            delivery_cycle_id=None,
        )
        self.connection.execute(
            """
            INSERT INTO outbox(
                id, job_type, payload_version, payload_json, status, available_at,
                lease_owner, lease_expires_at, attempt_count, created_at, updated_at,
                last_error, correlation_id, dedupe_key, reminder_id, lease_token,
                attempt_window_started_at, last_error_code, delivery_cycle_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, NULL, NULL, NULL, ?)
            """,
            (
                job.id,
                job.job_type,
                job.payload_version,
                payload_json,
                job.status,
                job.available_at,
                job.lease_owner,
                job.lease_expires_at,
                job.attempt_count,
                job.created_at,
                job.updated_at,
                job.correlation_id,
                job.dedupe_key,
                job.reminder_id,
                job.delivery_cycle_id,
            ),
        )
        return job

    def get_outbox(self, job_id: str) -> OutboxJob:
        validate_uuid4(job_id, "outbox.id")
        row = self.connection.execute("SELECT * FROM outbox WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise PlanningNotFoundError(f"outbox job {job_id} was not found")
        payload = _decode_json(str(row["payload_json"]), "outbox payload")
        if not isinstance(payload, Mapping):
            raise PlanningValidationError("stored outbox payload is not an object")
        return OutboxJob(
            id=str(row["id"]),
            job_type=str(row["job_type"]),
            payload_version=int(row["payload_version"]),
            payload=payload,
            status=str(row["status"]),  # type: ignore[arg-type]
            available_at=str(row["available_at"]),
            lease_owner=row["lease_owner"],
            lease_expires_at=row["lease_expires_at"],
            attempt_count=int(row["attempt_count"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            last_error=row["last_error"],
            correlation_id=row["correlation_id"],
            dedupe_key=row["dedupe_key"],
            reminder_id=row["reminder_id"],
            lease_token=row["lease_token"],
            attempt_window_started_at=row["attempt_window_started_at"],
            last_error_code=row["last_error_code"],
            delivery_cycle_id=row["delivery_cycle_id"],
        )

    def ensure_reminder_outbox(
        self,
        *,
        reminder_id: str,
        job_type: str,
        payload: Mapping[str, Any],
        available_at: str,
        dedupe_key: str,
        correlation_id: str | None = None,
        requeue_cancelled: bool = False,
    ) -> OutboxJob:
        """Create or recover one logical reminder delivery job inside a transaction."""

        if not self.database.in_transaction:
            raise PlanningTransactionRequiredError("reminder outbox reconciliation requires a transaction")
        validate_uuid4(reminder_id, "outbox.reminder_id")
        validate_text(dedupe_key, "outbox.dedupe_key", max_length=256)
        validate_utc_timestamp(available_at, "outbox.available_at")
        row = self.connection.execute(
            "SELECT id, status FROM outbox WHERE dedupe_key = ?",
            (dedupe_key,),
        ).fetchone()
        if row is not None:
            job_id = str(row["id"])
            if requeue_cancelled and str(row["status"]) == "cancelled":
                self.connection.execute(
                    """
                    UPDATE outbox
                    SET status = 'queued', available_at = ?, lease_owner = NULL,
                        lease_expires_at = NULL, lease_token = NULL, last_error = NULL,
                        last_error_code = NULL, attempt_count = 0,
                        attempt_window_started_at = NULL, delivery_cycle_id = NULL,
                        updated_at = ?
                    WHERE id = ? AND status = 'cancelled'
                    """,
                    (available_at, self._now(), job_id),
                )
            return self.get_outbox(job_id)

        try:
            return self.enqueue_outbox(
                job_type=job_type,
                payload=payload,
                available_at=available_at,
                correlation_id=correlation_id,
                dedupe_key=dedupe_key,
                reminder_id=reminder_id,
            )
        except sqlite3.IntegrityError:
            # A second process can only reach this branch after the unique
            # logical-job index wins a race.  Return that winner rather than
            # manufacturing another delivery job.
            existing = self.connection.execute(
                "SELECT id FROM outbox WHERE dedupe_key = ?",
                (dedupe_key,),
            ).fetchone()
            if existing is None:
                raise
            return self.get_outbox(str(existing["id"]))

    def claim_outbox(
        self,
        *,
        job_type: str,
        lease_owner: str,
        now: str,
        lease_expires_at: str,
    ) -> OutboxJob | None:
        """Atomically claim one queued or expired reminder job."""

        validate_text(job_type, "outbox.job_type", max_length=128)
        validate_text(lease_owner, "outbox.lease_owner", max_length=128)
        validate_utc_timestamp(now, "outbox.now")
        validate_utc_timestamp(lease_expires_at, "outbox.lease_expires_at")
        with self.database.transaction():
            candidate = self.connection.execute(
                """
                SELECT o.id
                FROM outbox AS o
                JOIN reminders AS r ON r.id = o.reminder_id
                WHERE o.job_type = ?
                  AND r.deleted_at IS NULL
                  AND r.status IN ('pending', 'due')
                  AND r.delivery_state IN ('not_due', 'queued', 'retrying')
                  AND (
                        (o.status = 'queued' AND o.available_at <= ?)
                        OR
                        (o.status = 'leased' AND o.lease_expires_at IS NOT NULL
                            AND o.lease_expires_at <= ?)
                  )
                ORDER BY o.available_at, o.id
                LIMIT 1
                """,
                (job_type, now, now),
            ).fetchone()
            if candidate is None:
                return None
            job_id = str(candidate["id"])
            lease_token = new_uuid4()
            cursor = self.connection.execute(
                """
                UPDATE outbox
                SET status = 'leased', lease_owner = ?, lease_expires_at = ?,
                    lease_token = ?, attempt_count = attempt_count + 1,
                    updated_at = ?
                WHERE id = ?
                  AND job_type = ?
                  AND (
                        (status = 'queued' AND available_at <= ?)
                        OR
                        (status = 'leased' AND lease_expires_at IS NOT NULL
                            AND lease_expires_at <= ?)
                  )
                """,
                (
                    lease_owner,
                    lease_expires_at,
                    lease_token,
                    now,
                    job_id,
                    job_type,
                    now,
                    now,
                ),
            )
            if cursor.rowcount != 1:
                return None
            return self.get_outbox(job_id)

    def snapshot_outbox_delivery_policy(
        self,
        *,
        job_id: str,
        lease_owner: str,
        lease_token: str,
        policy: Mapping[str, Any],
        now: str,
    ) -> OutboxJob:
        """Persist the policy used by this logical delivery job before sending."""

        validate_uuid4(job_id, "outbox.id")
        validate_text(lease_owner, "outbox.lease_owner", max_length=128)
        validate_uuid4(lease_token, "outbox.lease_token")
        validate_utc_timestamp(now, "outbox.updated_at")
        safe_policy = _json_ready(dict(policy))
        reject_secret_fields(safe_policy, field="outbox.delivery_policy")
        with self.database.transaction():
            row = self.connection.execute(
                "SELECT payload_json FROM outbox WHERE id = ? AND status = 'leased' "
                "AND lease_owner = ? AND lease_token = ?",
                (job_id, lease_owner, lease_token),
            ).fetchone()
            if row is None:
                raise PlanningLeaseLostError(f"lease lost while snapshotting outbox {job_id}")
            payload = _decode_json(str(row["payload_json"]), "outbox payload")
            if not isinstance(payload, Mapping):
                raise PlanningValidationError("stored outbox payload is not an object")
            if isinstance(payload.get("delivery_policy"), Mapping):
                return self.get_outbox(job_id)
            updated_payload = dict(payload)
            updated_payload["delivery_policy"] = safe_policy
            payload_json = json.dumps(updated_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if len(payload_json) > 1_048_576:
                raise PlanningValidationError("outbox.payload is too large")
            self.connection.execute(
                "UPDATE outbox SET payload_json = ?, updated_at = ? WHERE id = ? "
                "AND status = 'leased' AND lease_owner = ? AND lease_token = ?",
                (payload_json, now, job_id, lease_owner, lease_token),
            )
            return self.get_outbox(job_id)

    def record_outbox_terminal_channel(
        self,
        *,
        job_id: str,
        lease_owner: str,
        lease_token: str,
        channel: str,
        error_code: str,
        now: str,
    ) -> OutboxJob:
        """Remember one permanent channel result for this delivery cycle."""

        validate_uuid4(job_id, "outbox.id")
        validate_text(lease_owner, "outbox.lease_owner", max_length=128)
        validate_uuid4(lease_token, "outbox.lease_token")
        validate_utc_timestamp(now, "outbox.updated_at")
        _validate_delivery_terminal_channel(channel, error_code)
        with self.database.transaction():
            row = self.connection.execute(
                "SELECT payload_json FROM outbox WHERE id = ? AND status = 'leased' "
                "AND lease_owner = ? AND lease_token = ?",
                (job_id, lease_owner, lease_token),
            ).fetchone()
            if row is None:
                raise PlanningLeaseLostError(f"lease lost while recording outbox channel {job_id}")
            payload = _decode_json(str(row["payload_json"]), "outbox payload")
            if not isinstance(payload, Mapping):
                raise PlanningValidationError("stored outbox payload is not an object")
            existing = payload.get("delivery_terminal_channels")
            if existing is None:
                terminal_channels: dict[str, str] = {}
            elif isinstance(existing, Mapping):
                terminal_channels = {}
                for existing_channel, existing_code in existing.items():
                    if not isinstance(existing_channel, str) or not isinstance(existing_code, str):
                        raise PlanningValidationError("stored outbox terminal channel state is invalid")
                    _validate_delivery_terminal_channel(existing_channel, existing_code)
                    terminal_channels[existing_channel] = existing_code
            else:
                raise PlanningValidationError("stored outbox terminal channel state is invalid")
            terminal_channels[channel] = error_code
            updated_payload = dict(payload)
            updated_payload["delivery_terminal_channels"] = terminal_channels
            safe_payload = _json_ready(updated_payload)
            reject_secret_fields(safe_payload, field="outbox.payload")
            payload_json = json.dumps(safe_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if len(payload_json) > 1_048_576:
                raise PlanningValidationError("outbox.payload is too large")
            cursor = self.connection.execute(
                "UPDATE outbox SET payload_json = ?, updated_at = ? WHERE id = ? "
                "AND status = 'leased' AND lease_owner = ? AND lease_token = ?",
                (payload_json, now, job_id, lease_owner, lease_token),
            )
            if cursor.rowcount != 1:
                raise PlanningLeaseLostError(f"lease lost while recording outbox channel {job_id}")
            return self.get_outbox(job_id)

    def transition_outbox(
        self,
        *,
        job_id: str,
        lease_owner: str,
        lease_token: str,
        status: str,
        now: str,
        available_at: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> bool:
        """Commit a lease-owned job result; stale workers cannot commit."""

        validate_uuid4(job_id, "outbox.id")
        validate_text(lease_owner, "outbox.lease_owner", max_length=128)
        validate_uuid4(lease_token, "outbox.lease_token")
        if status not in {"queued", "succeeded", "failed", "cancelled"}:
            raise PlanningValidationError("outbox.status has an invalid transition")
        validate_utc_timestamp(now, "outbox.updated_at")
        if status == "queued":
            if available_at is None:
                raise PlanningValidationError("queued outbox transition requires available_at")
            validate_utc_timestamp(available_at, "outbox.available_at")
        elif available_at is not None:
            validate_utc_timestamp(available_at, "outbox.available_at")
        if error_code is not None:
            validate_text(error_code, "outbox.last_error_code", max_length=128, allow_empty=True)
        if error_message is not None:
            validate_text(error_message, "outbox.last_error", max_length=512, allow_empty=True)
        with self.database.transaction():
            cursor = self.connection.execute(
                """
                UPDATE outbox
                SET status = ?, available_at = COALESCE(?, available_at),
                    lease_owner = NULL, lease_expires_at = NULL, lease_token = NULL,
                    last_error_code = ?, last_error = ?, updated_at = ?
                WHERE id = ? AND status = 'leased' AND lease_owner = ? AND lease_token = ?
                """,
                (
                    status,
                    available_at,
                    error_code,
                    error_message,
                    now,
                    job_id,
                    lease_owner,
                    lease_token,
                ),
            )
            return cursor.rowcount == 1

    def commit_reminder_delivery(
        self,
        *,
        reminder_id: str,
        expected_version: int,
        job_id: str,
        lease_owner: str,
        lease_token: str,
        delivery_state: str,
        outbox_status: str,
        now: str,
        context: MutationContext,
        audit_action: str,
        available_at: str | None = None,
        next_attempt_at: str | None = None,
        final_failure_at: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> Reminder | None:
        """Atomically persist reminder state and the lease-owned job result."""

        validate_uuid4(reminder_id, "reminder.id")
        validate_uuid4(job_id, "outbox.id")
        validate_uuid4(lease_token, "outbox.lease_token")
        if expected_version < 1:
            raise PlanningValidationError("expected_version must be positive")
        if delivery_state not in {"delivered", "retrying", "failed"}:
            raise PlanningValidationError("reminder.delivery_state is not a delivery outcome")
        if outbox_status not in {"queued", "succeeded", "failed"}:
            raise PlanningValidationError("outbox.status is not a delivery outcome")
        context = self._context(context)
        validate_utc_timestamp(now, "delivery.now")
        if available_at is not None:
            validate_utc_timestamp(available_at, "outbox.available_at")
        _optional_timestamp(next_attempt_at, "reminder.next_attempt_at")
        _optional_timestamp(final_failure_at, "reminder.final_failure_at")
        if error_code is not None:
            validate_text(error_code, "outbox.last_error_code", max_length=128, allow_empty=True)
        if error_message is not None:
            validate_text(error_message, "outbox.last_error", max_length=512, allow_empty=True)
        with self.database.transaction():
            current = _row_to_reminder(self._require_row("reminders", reminder_id, "reminder"))
            if current.status in {"completed", "cancelled"}:
                self.connection.execute(
                    """
                    UPDATE outbox
                    SET status = 'cancelled', lease_owner = NULL, lease_expires_at = NULL,
                        lease_token = NULL, updated_at = ?
                    WHERE id = ? AND status = 'leased' AND lease_owner = ? AND lease_token = ?
                    """,
                    (now, job_id, lease_owner, lease_token),
                )
                return None
            if current.version != expected_version:
                raise PlanningVersionConflictError("reminder", reminder_id, expected_version, current.version)

            updated = replace(
                current,
                status="due",
                delivery_state=delivery_state,  # type: ignore[arg-type]
                next_attempt_at=next_attempt_at,
                final_failure_at=final_failure_at,
                version=current.version + 1,
                updated_at=now,
            )
            validate_reminder_shape(
                title=updated.title,
                due_at_utc=updated.due_at_utc,
                timezone_name=updated.timezone,
                status=updated.status,
                delivery_state=updated.delivery_state,
                notes=updated.notes,
            )
            cursor = self.connection.execute(
                """
                UPDATE reminders
                SET status = ?, delivery_state = ?, next_attempt_at = ?,
                    final_failure_at = ?, version = ?, updated_at = ?
                WHERE id = ? AND version = ?
                """,
                (
                    updated.status,
                    updated.delivery_state,
                    updated.next_attempt_at,
                    updated.final_failure_at,
                    updated.version,
                    updated.updated_at,
                    reminder_id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                self._raise_version_conflict("reminder", reminder_id, expected_version, "reminders")
            outbox_cursor = self.connection.execute(
                """
                UPDATE outbox
                SET status = ?, available_at = COALESCE(?, available_at),
                    lease_owner = NULL, lease_expires_at = NULL, lease_token = NULL,
                    last_error_code = ?, last_error = ?, updated_at = ?
                WHERE id = ? AND status = 'leased' AND lease_owner = ? AND lease_token = ?
                """,
                (
                    outbox_status,
                    available_at,
                    error_code,
                    error_message,
                    now,
                    job_id,
                    lease_owner,
                    lease_token,
                ),
            )
            if outbox_cursor.rowcount != 1:
                raise PlanningLeaseLostError(f"lease lost while committing reminder {reminder_id}")
            self._record_audit(
                context=context,
                action=audit_action,
                domain=updated.domain,
                object_id=updated.id,
                old_version=current.version,
                new_version=updated.version,
                before={
                    "status": current.status,
                    "delivery_state": current.delivery_state,
                    "next_attempt_at": current.next_attempt_at,
                },
                after={
                    "status": updated.status,
                    "delivery_state": updated.delivery_state,
                    "next_attempt_at": updated.next_attempt_at,
                    "final_failure_at": updated.final_failure_at,
                    "error_code": error_code,
                },
            )
            return updated

    def start_delivery_attempt(
        self,
        *,
        reminder_id: str,
        channel: str,
        started_at: str | None = None,
    ) -> DeliveryAttempt:
        """Persist the attempt identity before calling a remote provider."""

        validate_uuid4(reminder_id, "delivery_attempt.reminder_id")
        validate_text(channel, "delivery_attempt.channel", max_length=64)
        started_at = started_at or self._now()
        validate_utc_timestamp(started_at, "delivery_attempt.started_at")
        with self.database.transaction():
            cycle_row = self.connection.execute(
                """
                SELECT delivery_cycle_id
                FROM outbox
                WHERE reminder_id = ? AND job_type = ?
                ORDER BY created_at, id
                LIMIT 1
                """,
                (reminder_id, REMINDER_DELIVERY_JOB_TYPE),
            ).fetchone()
            delivery_cycle_id = cycle_row["delivery_cycle_id"] if cycle_row is not None else None
            if delivery_cycle_id is None:
                delivery_cycle_id = new_uuid4()
                if cycle_row is not None:
                    self.connection.execute(
                        """
                        UPDATE outbox
                        SET delivery_cycle_id = ?, attempt_window_started_at = ?
                        WHERE reminder_id = ? AND job_type = ?
                        """,
                        (
                            delivery_cycle_id,
                            started_at,
                            reminder_id,
                            REMINDER_DELIVERY_JOB_TYPE,
                        ),
                    )

            row = self.connection.execute(
                """
                SELECT COALESCE(MAX(attempt_number), 0) + 1
                FROM delivery_attempts
                WHERE reminder_id = ? AND channel = ?
                """,
                (reminder_id, channel),
            ).fetchone()
            attempt_number = int(row[0])
            attempt = DeliveryAttempt(
                id=new_uuid4(),
                reminder_id=reminder_id,
                channel=channel,
                attempt_number=attempt_number,
                status="started",
                started_at=started_at,
                finished_at=None,
                error_code=None,
                error_message=None,
                provider_receipt=None,
                correlation_id=new_uuid4(),
                created_at=self._now(),
                delivery_cycle_id=delivery_cycle_id,
            )
            self.connection.execute(
                """
                INSERT INTO delivery_attempts(
                    id, reminder_id, channel, attempt_number, status, started_at,
                    finished_at, error_code, error_message, provider_receipt,
                    created_at, correlation_id, delivery_cycle_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt.id,
                    attempt.reminder_id,
                    attempt.channel,
                    attempt.attempt_number,
                    attempt.status,
                    attempt.started_at,
                    attempt.finished_at,
                    attempt.error_code,
                    attempt.error_message,
                    attempt.provider_receipt,
                    attempt.created_at,
                    attempt.correlation_id,
                    attempt.delivery_cycle_id,
                ),
            )
            return attempt

    def count_delivery_attempts(
        self,
        *,
        reminder_id: str,
        channel: str,
        delivery_cycle_id: str | None,
    ) -> int:
        """Count semantic channel attempts in one durable delivery cycle."""

        validate_uuid4(reminder_id, "delivery_attempt.reminder_id")
        validate_text(channel, "delivery_attempt.channel", max_length=64)
        if delivery_cycle_id is None:
            return 0
        validate_uuid4(delivery_cycle_id, "delivery_attempt.delivery_cycle_id")
        row = self.connection.execute(
            """
            SELECT COUNT(*)
            FROM delivery_attempts
            WHERE reminder_id = ? AND channel = ? AND delivery_cycle_id = ?
            """,
            (reminder_id, channel, delivery_cycle_id),
        ).fetchone()
        return int(row[0])

    def has_successful_delivery_attempt(self, *, reminder_id: str, channel: str) -> bool:
        """Return whether a channel has ever succeeded for this reminder."""

        validate_uuid4(reminder_id, "delivery_attempt.reminder_id")
        validate_text(channel, "delivery_attempt.channel", max_length=64)
        row = self.connection.execute(
            """
            SELECT 1
            FROM delivery_attempts
            WHERE reminder_id = ? AND channel = ? AND status = 'succeeded'
            LIMIT 1
            """,
            (reminder_id, channel),
        ).fetchone()
        return row is not None

    def finish_delivery_attempt(
        self,
        *,
        attempt_id: str,
        status: str,
        finished_at: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        provider_receipt: str | None = None,
    ) -> bool:
        validate_uuid4(attempt_id, "delivery_attempt.id")
        if status not in {"succeeded", "failed"}:
            raise PlanningValidationError("delivery_attempt.status must be succeeded or failed")
        finished_at = finished_at or self._now()
        validate_utc_timestamp(finished_at, "delivery_attempt.finished_at")
        if error_code is not None:
            validate_text(error_code, "delivery_attempt.error_code", max_length=128, allow_empty=True)
        if error_message is not None:
            validate_text(error_message, "delivery_attempt.error_message", max_length=512, allow_empty=True)
        if provider_receipt is not None:
            validate_text(provider_receipt, "delivery_attempt.provider_receipt", max_length=512, allow_empty=True)
        with self.database.transaction():
            cursor = self.connection.execute(
                """
                UPDATE delivery_attempts
                SET status = ?, finished_at = ?, error_code = ?, error_message = ?,
                    provider_receipt = ?
                WHERE id = ? AND status IN ('queued', 'started')
                """,
                (status, finished_at, error_code, error_message, provider_receipt, attempt_id),
            )
            return cursor.rowcount == 1

    def record_delivery_attempt(
        self,
        *,
        reminder_id: str,
        channel: str,
        attempt_number: int,
        status: str,
        started_at: str | None = None,
        finished_at: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        provider_receipt: str | None = None,
        correlation_id: str | None = None,
        delivery_cycle_id: str | None = None,
    ) -> str:
        if not self.database.in_transaction:
            raise PlanningTransactionRequiredError("delivery attempt writes require a surrounding transaction")
        validate_uuid4(reminder_id, "delivery_attempt.reminder_id")
        validate_text(channel, "delivery_attempt.channel", max_length=64)
        if attempt_number < 1:
            raise PlanningValidationError("delivery_attempt.attempt_number must be positive")
        if status not in {"queued", "started", "succeeded", "failed"}:
            raise PlanningValidationError("delivery_attempt.status has an invalid enum")
        _optional_timestamp(started_at, "delivery_attempt.started_at")
        _optional_timestamp(finished_at, "delivery_attempt.finished_at")
        if error_code is not None:
            validate_text(error_code, "delivery_attempt.error_code", max_length=128, allow_empty=True)
        if error_message is not None:
            validate_text(error_message, "delivery_attempt.error_message", max_length=2000, allow_empty=True)
        if provider_receipt is not None:
            validate_text(provider_receipt, "delivery_attempt.provider_receipt", max_length=512, allow_empty=True)
        if correlation_id is None:
            correlation_id = new_uuid4()
        validate_uuid4(correlation_id, "delivery_attempt.correlation_id")
        if delivery_cycle_id is not None:
            validate_uuid4(delivery_cycle_id, "delivery_attempt.delivery_cycle_id")
        attempt_id = new_uuid4()
        self.connection.execute(
            """
            INSERT INTO delivery_attempts(
                id, reminder_id, channel, attempt_number, status, started_at,
                finished_at, error_code, error_message, provider_receipt, created_at,
                correlation_id, delivery_cycle_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                attempt_id,
                reminder_id,
                channel,
                attempt_number,
                status,
                started_at,
                finished_at,
                error_code,
                error_message,
                provider_receipt,
                self._now(),
                correlation_id,
                delivery_cycle_id,
            ),
        )
        return attempt_id

    def manual_retry_reminder(
        self,
        reminder_id: str,
        *,
        expected_version: int,
        context: MutationContext,
        now: str | None = None,
        delivery_payload: Mapping[str, Any] | None = None,
    ) -> Reminder:
        """Requeue a terminal delivery failure for a trusted operator only."""

        validate_uuid4(reminder_id, "reminder.id")
        if expected_version < 1:
            raise PlanningValidationError("expected_version must be positive")
        context = self._context(context)
        trusted_telegram_surface = context.surface == "telegram" and context.actor_id.startswith("telegram:")
        if (
            context.audience != "operator"
            or context.actor_type not in {"operator", "service"}
            or not (context.surface in {"operator", "system"} or trusted_telegram_surface)
        ):
            raise PlanningValidationError("manual reminder retry requires an operator context")
        now = now or self._now()
        validate_utc_timestamp(now, "manual_retry.now")
        dedupe_key = f"{REMINDER_OUTBOX_DEDUPE_PREFIX}{reminder_id}"
        with self.database.transaction():
            current = _row_to_reminder(self._require_row("reminders", reminder_id, "reminder"))
            if current.version != expected_version:
                raise PlanningVersionConflictError("reminder", reminder_id, expected_version, current.version)
            if current.status in {"completed", "cancelled"}:
                raise PlanningValidationError("completed or cancelled reminders cannot be manually retried")
            if current.delivery_state != "failed":
                # A retry request replayed with the current version is a safe
                # no-op and cannot create another active job.
                return current

            job_row = self.connection.execute(
                "SELECT * FROM outbox WHERE dedupe_key = ?",
                (dedupe_key,),
            ).fetchone()
            if job_row is None:
                if delivery_payload is None:
                    raise PlanningValidationError("terminal reminder has no durable delivery job to retry")
                payload = dict(delivery_payload)
                payload.setdefault("reminder_id", reminder_id)
                payload.pop("delivery_policy", None)
                payload.pop("delivery_terminal_channels", None)
                self.enqueue_outbox(
                    job_type=REMINDER_DELIVERY_JOB_TYPE,
                    payload=payload,
                    available_at=now,
                    correlation_id=current.audit_correlation_id,
                    dedupe_key=dedupe_key,
                    reminder_id=reminder_id,
                )
            else:
                job_status = str(job_row["status"])
                if job_status == "leased" and job_row["lease_expires_at"] is not None:
                    if str(job_row["lease_expires_at"]) > now:
                        raise PlanningValidationError("terminal reminder delivery job still has a live lease")
                existing_payload = _decode_json(str(job_row["payload_json"]), "outbox payload")
                if not isinstance(existing_payload, Mapping):
                    raise PlanningValidationError("stored outbox payload is not an object")
                # A manual retry starts a new logical delivery cycle.  Do not
                # silently reuse the policy snapshot from the failed cycle.
                retry_payload = dict(existing_payload)
                retry_payload.pop("delivery_policy", None)
                retry_payload.pop("delivery_terminal_channels", None)
                retry_payload_json = json.dumps(
                    retry_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                self.connection.execute(
                    """
                    UPDATE outbox
                    SET status = 'queued', available_at = ?, payload_json = ?, lease_owner = NULL,
                        lease_expires_at = NULL, lease_token = NULL, attempt_count = 0,
                        attempt_window_started_at = NULL, last_error = NULL,
                        last_error_code = NULL, delivery_cycle_id = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (now, retry_payload_json, now, str(job_row["id"])),
                )

            updated = replace(
                current,
                status="due",
                delivery_state="queued",
                next_attempt_at=None,
                final_failure_at=None,
                version=current.version + 1,
                updated_at=now,
            )
            cursor = self.connection.execute(
                """
                UPDATE reminders
                SET status = ?, delivery_state = ?, next_attempt_at = ?,
                    final_failure_at = ?, version = ?, updated_at = ?
                WHERE id = ? AND version = ?
                """,
                (
                    updated.status,
                    updated.delivery_state,
                    updated.next_attempt_at,
                    updated.final_failure_at,
                    updated.version,
                    updated.updated_at,
                    reminder_id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                self._raise_version_conflict("reminder", reminder_id, expected_version, "reminders")
            self._record_audit(
                context=context,
                action="manual_retry",
                domain=updated.domain,
                object_id=updated.id,
                old_version=current.version,
                new_version=updated.version,
                before={"delivery_state": current.delivery_state, "final_failure_at": current.final_failure_at},
                after={"delivery_state": updated.delivery_state, "next_attempt_at": None},
            )
            return updated

    def create_provider_mapping(
        self,
        *,
        domain: str,
        object_id: str,
        provider: str,
        external_id: str,
        external_calendar_id: str | None = None,
        external_version: str | None = None,
        external_etag: str | None = None,
        last_exported_hash: str | None = None,
        last_imported_hash: str | None = None,
    ) -> ProviderMapping:
        if domain not in {"task", "calendar_event", "project"}:
            raise PlanningValidationError("provider_mapping.domain has an invalid enum")
        validate_uuid4(object_id, "provider_mapping.object_id")
        validate_text(provider, "provider_mapping.provider", max_length=64)
        validate_text(external_id, "provider_mapping.external_id", max_length=512)
        for field, value, max_length in (
            ("external_calendar_id", external_calendar_id, 512),
            ("external_version", external_version, 256),
            ("external_etag", external_etag, 512),
            ("last_exported_hash", last_exported_hash, 256),
            ("last_imported_hash", last_imported_hash, 256),
        ):
            if value is not None:
                validate_text(value, f"provider_mapping.{field}", max_length=max_length, allow_empty=True)
        timestamp = self._now()
        mapping = ProviderMapping(
            id=new_uuid4(),
            domain=domain,
            object_id=object_id,
            provider=provider,
            external_id=external_id,
            external_calendar_id=external_calendar_id,
            external_version=external_version,
            external_etag=external_etag,
            last_exported_hash=last_exported_hash,
            last_imported_hash=last_imported_hash,
            deleted_at=None,
            created_at=timestamp,
            updated_at=timestamp,
        )
        with self.database.transaction():
            self.connection.execute(
                """
                INSERT INTO provider_mappings(
                    id, domain, object_id, provider, external_id, external_calendar_id,
                    external_version, external_etag, last_exported_hash, last_imported_hash,
                    deleted_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    mapping.id,
                    mapping.domain,
                    mapping.object_id,
                    mapping.provider,
                    mapping.external_id,
                    mapping.external_calendar_id,
                    mapping.external_version,
                    mapping.external_etag,
                    mapping.last_exported_hash,
                    mapping.last_imported_hash,
                    mapping.deleted_at,
                    mapping.created_at,
                    mapping.updated_at,
                ),
            )
        return mapping

    def create_sync_cursor(self, *, provider: str, scope: str, cursor: str | None = None) -> SyncCursor:
        validate_text(provider, "sync_cursor.provider", max_length=64)
        validate_text(scope, "sync_cursor.scope", max_length=512)
        if cursor is not None:
            validate_text(cursor, "sync_cursor.cursor", max_length=2000, allow_empty=True)
        timestamp = self._now()
        result = SyncCursor(provider, scope, cursor, None, timestamp, timestamp)
        with self.database.transaction():
            self.connection.execute(
                """
                INSERT INTO sync_cursors(provider, scope, cursor, last_synced_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (result.provider, result.scope, result.cursor, result.last_synced_at, result.created_at, result.updated_at),
            )
        return result

    def create_sync_conflict(
        self,
        *,
        domain: str,
        object_id: str,
        provider: str,
        external_id: str,
        details: Mapping[str, Any],
        local_hash: str | None = None,
        remote_hash: str | None = None,
    ) -> SyncConflict:
        if domain not in {"task", "calendar_event", "project"}:
            raise PlanningValidationError("sync_conflict.domain has an invalid enum")
        validate_uuid4(object_id, "sync_conflict.object_id")
        validate_text(provider, "sync_conflict.provider", max_length=64)
        validate_text(external_id, "sync_conflict.external_id", max_length=512)
        for field, value in (("local_hash", local_hash), ("remote_hash", remote_hash)):
            if value is not None:
                validate_text(value, f"sync_conflict.{field}", max_length=256, allow_empty=True)
        try:
            safe_details = _json_ready(details)
            reject_secret_fields(safe_details, field="sync_conflict.details")
            details_json = json.dumps(safe_details, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise PlanningValidationError("sync_conflict.details must be JSON-serializable") from exc
        if len(details_json) > 8192:
            raise PlanningValidationError("sync_conflict.details is too large")
        conflict = SyncConflict(
            id=new_uuid4(),
            domain=domain,
            object_id=object_id,
            provider=provider,
            external_id=external_id,
            local_hash=local_hash,
            remote_hash=remote_hash,
            details=details,
            status="open",
            created_at=self._now(),
            resolved_at=None,
        )
        with self.database.transaction():
            self.connection.execute(
                """
                INSERT INTO sync_conflicts(
                    id, domain, object_id, provider, external_id, local_hash, remote_hash,
                    details_json, status, created_at, resolved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    conflict.id,
                    conflict.domain,
                    conflict.object_id,
                    conflict.provider,
                    conflict.external_id,
                    conflict.local_hash,
                    conflict.remote_hash,
                    details_json,
                    conflict.status,
                    conflict.created_at,
                    conflict.resolved_at,
                ),
            )
        return conflict
