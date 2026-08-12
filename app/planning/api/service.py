from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from app.planning.api.auth import (
    AuthenticatedPlanningContext,
    capabilities_for_audience,
)
from app.planning.api.envelopes import FreshnessEnvelopeBuilder
from app.planning.api.errors import PlanningApiError
from app.planning.capabilities import planning_capability_metadata
from app.planning.events import EventService
from app.planning.models import REMINDER_DELIVERY_JOB_TYPE, MutationContext, new_uuid4, utc_now, validate_timezone
from app.planning.errors import (
    PlanningIdempotencyInProgressError,
)
from app.planning.projects import ProjectService
from app.planning.repositories import PlanningRepository
from app.planning.tasks import TaskService


@dataclass(frozen=True)
class StoredMutationResponse:
    response_json: str
    status: int
    replay: bool


def _object_domain(value: Any) -> str:
    domain = getattr(value, "domain", None)
    if not isinstance(domain, str) or not domain:
        raise RuntimeError("Planning mutation did not return a canonical domain object")
    return domain


def _object_dict(value: Any) -> dict[str, Any]:
    to_dict = getattr(value, "to_dict", None)
    if not callable(to_dict):
        raise RuntimeError("Planning mutation did not return a canonical object")
    result = to_dict()
    if not isinstance(result, dict):
        raise RuntimeError("Planning mutation returned an invalid canonical object")
    return result


class PlanningApiService:
    """Domain-facing API service; HTTP routing remains outside this class."""

    def __init__(
        self,
        database: Any,
        *,
        repository: PlanningRepository | None = None,
        now_fn: Callable[[], str] = utc_now,
        default_timezone: str = "Europe/Moscow",
        stale_after_seconds: int = 300,
    ) -> None:
        validate_timezone(default_timezone, "planning.default_timezone")
        self.database = database
        self.repository = repository or PlanningRepository(database, now_fn=now_fn)
        self.envelopes = FreshnessEnvelopeBuilder(now_fn=now_fn, stale_after_seconds=stale_after_seconds)
        self.default_timezone = default_timezone
        self.task_service = TaskService(database, repository=self.repository, now_fn=now_fn)
        self.event_service = EventService(database, repository=self.repository, now_fn=now_fn)
        self.project_service = ProjectService(database, repository=self.repository, now_fn=now_fn)

    def list_reminders(
        self,
        *,
        state: str | None,
        from_utc: str | None,
        to_utc: str | None,
        limit: int,
        offset: int,
        correlation_id: str,
    ) -> dict[str, Any]:
        items = self.repository.list_reminders(
            state=state,
            from_utc=from_utc,
            to_utc=to_utc,
            limit=limit + 1,
            offset=offset,
        )
        return self.envelopes.list_response(
            domain="reminder",
            items=[item.to_dict() for item in items[:limit]],
            correlation_id=correlation_id,
            limit=limit,
            offset=offset,
            has_more=len(items) > limit,
        )

    def list_tasks(
        self,
        *,
        view: str,
        project_id: str | None,
        limit: int,
        offset: int,
        correlation_id: str,
    ) -> dict[str, Any]:
        items = self.task_service.list_view(
            view=view,
            reference_time_utc=self.envelopes.now(),
            caller_timezone=self.default_timezone,
            project_id=project_id,
            limit=limit + 1,
            offset=offset,
        )
        return self.envelopes.list_response(
            domain="task",
            items=[item.to_dict() for item in items[:limit]],
            correlation_id=correlation_id,
            limit=limit,
            offset=offset,
            has_more=len(items) > limit,
        )

    def list_events(
        self,
        *,
        from_utc: str,
        to_utc: str,
        limit: int,
        offset: int,
        correlation_id: str,
    ) -> dict[str, Any]:
        items = self.event_service.query_range(
            from_utc=from_utc,
            to_utc=to_utc,
            caller_timezone=self.default_timezone,
            limit=limit + 1,
            offset=offset,
        )
        return self.envelopes.list_response(
            domain="calendar_event",
            items=[item.to_dict() for item in items[:limit]],
            correlation_id=correlation_id,
            limit=limit,
            offset=offset,
            has_more=len(items) > limit,
        )

    def list_projects(
        self,
        *,
        limit: int,
        offset: int,
        correlation_id: str,
    ) -> dict[str, Any]:
        items = self.project_service.list_active(limit=limit + 1, offset=offset)
        return self.envelopes.list_response(
            domain="project",
            items=[item.to_dict() for item in items[:limit]],
            correlation_id=correlation_id,
            limit=limit,
            offset=offset,
            has_more=len(items) > limit,
        )

    def status(self, *, audience: str, correlation_id: str) -> dict[str, Any]:
        storage_status = "available"
        try:
            self.database.connection.execute("SELECT 1").fetchone()
        except Exception:
            storage_status = "unavailable"
        return self.envelopes.status_response(
            capabilities=capabilities_for_audience(audience),
            capability_metadata=planning_capability_metadata().to_dict(),
            storage_status=storage_status,
            correlation_id=correlation_id,
        )

    def create_reminder(
        self,
        *,
        auth: AuthenticatedPlanningContext,
        key: str,
        payload: Mapping[str, Any],
    ) -> StoredMutationResponse:
        return self._mutate(
            auth=auth,
            key=key,
            route_key="POST /reminders",
            object_id=None,
            body=payload,
            expected_version=None,
            operation=lambda context: self.repository.create_reminder(
                title=payload["title"],
                notes=payload["notes"],
                due_at_utc=payload["due_at_utc"],
                timezone=payload["timezone"],
                context=context,
                outbox_job_type=REMINDER_DELIVERY_JOB_TYPE,
                outbox_payload={},
            ),
        )

    def patch_reminder(
        self,
        *,
        auth: AuthenticatedPlanningContext,
        key: str,
        reminder_id: str,
        expected_version: int,
        payload: Mapping[str, Any],
    ) -> StoredMutationResponse:
        def operation(context: MutationContext) -> Any:
            current = self.repository.get_reminder(reminder_id)
            self._require_active(current, "Reminder is not editable.")
            return self.repository.update_reminder(
                reminder_id,
                expected_version=expected_version,
                context=context,
                **dict(payload),
            )

        return self._mutate(
            auth=auth,
            key=key,
            route_key="PATCH /reminders/{id}",
            object_id=reminder_id,
            body=payload,
            expected_version=expected_version,
            operation=operation,
        )

    def complete_reminder(
        self,
        *,
        auth: AuthenticatedPlanningContext,
        key: str,
        reminder_id: str,
        expected_version: int,
    ) -> StoredMutationResponse:
        return self._reminder_action(
            auth=auth,
            key=key,
            reminder_id=reminder_id,
            expected_version=expected_version,
            route_key="POST /reminders/{id}/complete",
            action="complete",
        )

    def cancel_reminder(
        self,
        *,
        auth: AuthenticatedPlanningContext,
        key: str,
        reminder_id: str,
        expected_version: int,
    ) -> StoredMutationResponse:
        return self._reminder_action(
            auth=auth,
            key=key,
            reminder_id=reminder_id,
            expected_version=expected_version,
            route_key="POST /reminders/{id}/cancel",
            action="cancel",
        )

    def _reminder_action(
        self,
        *,
        auth: AuthenticatedPlanningContext,
        key: str,
        reminder_id: str,
        expected_version: int,
        route_key: str,
        action: str,
    ) -> StoredMutationResponse:
        def operation(context: MutationContext) -> Any:
            current = self.repository.get_reminder(reminder_id)
            self._require_active(current, "Reminder state does not allow this action.")
            if action == "complete":
                return self.repository.complete_reminder(
                    reminder_id,
                    expected_version=expected_version,
                    context=context,
                )
            return self.repository.cancel_reminder(
                reminder_id,
                expected_version=expected_version,
                context=context,
            )

        return self._mutate(
            auth=auth,
            key=key,
            route_key=route_key,
            object_id=reminder_id,
            body={},
            expected_version=expected_version,
            operation=operation,
        )

    def create_task(
        self,
        *,
        auth: AuthenticatedPlanningContext,
        key: str,
        payload: Mapping[str, Any],
    ) -> StoredMutationResponse:
        return self._mutate(
            auth=auth,
            key=key,
            route_key="POST /tasks",
            object_id=None,
            body=payload,
            expected_version=None,
            operation=lambda context: self.task_service.create(context=context, **dict(payload)),
        )

    def patch_task(
        self,
        *,
        auth: AuthenticatedPlanningContext,
        key: str,
        task_id: str,
        expected_version: int,
        payload: Mapping[str, Any],
    ) -> StoredMutationResponse:
        def operation(context: MutationContext) -> Any:
            current = self.repository.get_task(task_id)
            self._require_active(current, "Task is not editable.")
            return self.task_service.update(
                task_id,
                expected_version=expected_version,
                context=context,
                **dict(payload),
            )

        return self._mutate(
            auth=auth,
            key=key,
            route_key="PATCH /tasks/{id}",
            object_id=task_id,
            body=payload,
            expected_version=expected_version,
            operation=operation,
        )

    def complete_task(
        self,
        *,
        auth: AuthenticatedPlanningContext,
        key: str,
        task_id: str,
        expected_version: int,
    ) -> StoredMutationResponse:
        return self._task_action(
            auth=auth,
            key=key,
            task_id=task_id,
            expected_version=expected_version,
            route_key="POST /tasks/{id}/complete",
            action="complete",
        )

    def archive_task(
        self,
        *,
        auth: AuthenticatedPlanningContext,
        key: str,
        task_id: str,
        expected_version: int,
    ) -> StoredMutationResponse:
        return self._task_action(
            auth=auth,
            key=key,
            task_id=task_id,
            expected_version=expected_version,
            route_key="DELETE /tasks/{id}",
            action="archive",
        )

    def _task_action(
        self,
        *,
        auth: AuthenticatedPlanningContext,
        key: str,
        task_id: str,
        expected_version: int,
        route_key: str,
        action: str,
    ) -> StoredMutationResponse:
        def operation(context: MutationContext) -> Any:
            current = self.repository.get_task(task_id)
            self._require_active(current, "Task state does not allow this action.")
            if action == "complete":
                return self.task_service.complete(task_id, expected_version=expected_version, context=context)
            return self.task_service.archive(task_id, expected_version=expected_version, context=context)

        return self._mutate(
            auth=auth,
            key=key,
            route_key=route_key,
            object_id=task_id,
            body={},
            expected_version=expected_version,
            operation=operation,
        )

    def create_event(
        self,
        *,
        auth: AuthenticatedPlanningContext,
        key: str,
        payload: Mapping[str, Any],
    ) -> StoredMutationResponse:
        return self._mutate(
            auth=auth,
            key=key,
            route_key="POST /events",
            object_id=None,
            body=payload,
            expected_version=None,
            operation=lambda context: self.event_service.create(
                context=context,
                **{key: value for key, value in payload.items() if key != "recurrence_rule"},
            ),
        )

    def patch_event(
        self,
        *,
        auth: AuthenticatedPlanningContext,
        key: str,
        event_id: str,
        expected_version: int,
        payload: Mapping[str, Any],
    ) -> StoredMutationResponse:
        def operation(context: MutationContext) -> Any:
            current = self.repository.get_calendar_event(event_id)
            self._require_active(current, "Calendar event is not editable.")
            return self.event_service.update(
                event_id,
                expected_version=expected_version,
                context=context,
                **dict(payload),
            )

        return self._mutate(
            auth=auth,
            key=key,
            route_key="PATCH /events/{id}",
            object_id=event_id,
            body=payload,
            expected_version=expected_version,
            operation=operation,
        )

    def delete_event(
        self,
        *,
        auth: AuthenticatedPlanningContext,
        key: str,
        event_id: str,
        expected_version: int,
    ) -> StoredMutationResponse:
        def operation(context: MutationContext) -> Any:
            current = self.repository.get_calendar_event(event_id)
            self._require_active(current, "Calendar event is already deleted.")
            return self.event_service.delete(
                event_id,
                expected_version=expected_version,
                context=context,
            )

        return self._mutate(
            auth=auth,
            key=key,
            route_key="DELETE /events/{id}",
            object_id=event_id,
            body={},
            expected_version=expected_version,
            operation=operation,
        )

    def _mutate(
        self,
        *,
        auth: AuthenticatedPlanningContext,
        key: str,
        route_key: str,
        object_id: str | None,
        body: Mapping[str, Any],
        expected_version: int | None,
        operation: Callable[[MutationContext], Any],
    ) -> StoredMutationResponse:
        # Keep the thin service facades aligned with the repository attribute.
        # A few deployment/test harnesses intentionally swap the repository
        # after construction to inject audit failures.
        self.task_service.repository = self.repository
        self.event_service.repository = self.repository
        self.project_service.repository = self.repository
        request_hash = self._request_hash(
            auth=auth,
            route_key=route_key,
            object_id=object_id,
            body=body,
            expected_version=expected_version,
        )
        with self.database.transaction():
            claim = self.repository.claim_idempotency(
                audience=auth.audience,
                key=key,
                request_hash=request_hash,
            )
            if claim.is_replay:
                assert claim.response_json is not None
                return StoredMutationResponse(
                    response_json=claim.response_json,
                    status=claim.response_status or 200,
                    replay=True,
                )
            if not claim.is_new:
                raise PlanningIdempotencyInProgressError(auth.audience, key)

            correlation_id = new_uuid4()
            context = auth.mutation_context(correlation_id=correlation_id)
            result = operation(context)
            response = self.envelopes.object_response(
                domain=_object_domain(result),
                object_value=_object_dict(result),
                correlation_id=correlation_id,
            )
            response_json = self.repository.store_idempotency_response(
                audience=auth.audience,
                key=key,
                request_hash=request_hash,
                response=response,
                response_status=200,
                correlation_id=correlation_id,
            )
            return StoredMutationResponse(response_json=response_json, status=200, replay=False)

    @staticmethod
    def _require_active(value: Any, message: str) -> None:
        if getattr(value, "deleted_at", None) is not None:
            raise PlanningApiError(
                code="object_not_active",
                message=message,
                status=409,
            )

    @staticmethod
    def _request_hash(
        *,
        auth: AuthenticatedPlanningContext,
        route_key: str,
        object_id: str | None,
        body: Mapping[str, Any],
        expected_version: int | None,
    ) -> str:
        semantics = {
            "audience": auth.audience,
            "route": route_key,
            "object_id": object_id,
            "body": body,
            "expected_version": expected_version,
            "actor": auth.actor,
        }
        encoded = json.dumps(
            semantics,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"sha256:{hashlib.sha256(encoded).hexdigest()}"
