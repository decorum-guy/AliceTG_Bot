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
from app.planning.events import EventService, require_native_local_only_event
from app.planning.errors import (
    PlanningIdempotencyInProgressError,
    PlanningVersionConflictError,
)
from app.planning.models import REMINDER_DELIVERY_JOB_TYPE, MutationContext, new_uuid4, utc_now, validate_timezone
from app.planning.providers.contracts import ExternalCalendar, ProviderAdapterError, ProviderFailureCode
from app.planning.providers.icloud import ICloudEventDraft, ICloudEventWriteResult
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
        health_service: Any | None = None,
        provider_cache: Any | None = None,
        icloud_writes_enabled: bool = False,
    ) -> None:
        validate_timezone(default_timezone, "planning.default_timezone")
        self.database = database
        self.repository = repository or PlanningRepository(database, now_fn=now_fn)
        self.provider_cache = provider_cache
        self.icloud_writes_enabled = bool(icloud_writes_enabled)
        self.envelopes = FreshnessEnvelopeBuilder(
            now_fn=now_fn,
            stale_after_seconds=stale_after_seconds,
            sources_fn=None if provider_cache is None else provider_cache.source_metadata,
        )
        self.default_timezone = default_timezone
        self.health_service = health_service
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

    def get_task(self, *, task_id: str, correlation_id: str) -> dict[str, Any]:
        """Return one canonical task, including terminal and undated rows."""

        task = self.task_service.get(task_id)
        return self.envelopes.object_response(
            domain="task",
            object_value=task.to_dict(),
            correlation_id=correlation_id,
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
        response = self.envelopes.list_response(
            domain="calendar_event",
            items=[item.to_dict() for item in items[:limit]],
            correlation_id=correlation_id,
            limit=limit,
            offset=offset,
            has_more=len(items) > limit,
        )
        response["mutationCapabilities"] = {
            item.id: self._event_mutation_capabilities(item)
            for item in items[:limit]
        }
        return response

    def get_event(self, *, event_id: str, correlation_id: str) -> dict[str, Any]:
        """Return one canonical event, including tombstones and provider identity."""

        event = self.event_service.get(event_id)
        response = self.envelopes.object_response(
            domain="calendar_event",
            object_value=event.to_dict(),
            correlation_id=correlation_id,
        )
        response["mutationCapabilities"] = self._event_mutation_capabilities(event)
        return response

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
        response = self.envelopes.status_response(
            capabilities=capabilities_for_audience(audience),
            capability_metadata=planning_capability_metadata().to_dict(),
            storage_status=storage_status,
            correlation_id=correlation_id,
        )
        response["providerCapabilities"] = self._provider_capabilities()
        if self.health_service is not None:
            response["planningHealth"] = self.health_service.snapshot(correlation_id=correlation_id)
        return response

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
        def precondition() -> None:
            current = self.event_service.get(event_id)
            require_native_local_only_event(current)
            self._require_active(current, "Calendar event is not editable.")

        def operation(context: MutationContext) -> Any:
            current = self.event_service.get(event_id)
            require_native_local_only_event(current)
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
            precondition=precondition,
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
        def precondition() -> None:
            current = self.event_service.get(event_id)
            require_native_local_only_event(current)
            self._require_active(current, "Calendar event is already deleted.")

        def operation(context: MutationContext) -> Any:
            current = self.event_service.get(event_id)
            require_native_local_only_event(current)
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
            precondition=precondition,
            operation=operation,
        )

    def list_calendar_destinations(self, *, correlation_id: str) -> dict[str, Any]:
        cache = self._require_provider_cache()
        items = cache.calendar_destinations(writes_enabled=self.icloud_writes_enabled)
        return {
            "schemaVersion": "planning.v1",
            "kind": "calendar_destinations",
            "domain": "calendar_destination",
            "items": items,
            "capabilities": {
                "canCreateCalendar": self._provider_can_attempt(),
            },
            "generatedAt": self.envelopes.now(),
            **self.envelopes.freshness(),
            "correlation_id": correlation_id,
        }

    async def create_provider_event(
        self,
        *,
        auth: AuthenticatedPlanningContext,
        key: str,
        payload: Mapping[str, Any],
    ) -> StoredMutationResponse:
        self._require_provider_write_gate()
        cache = self._require_provider_cache()
        calendar_id = str(payload["calendar_id"])
        self._require_writable_destination(cache, calendar_id)
        draft = self._provider_event_draft(payload)
        route_key = "POST /provider-events"
        request_hash = self._request_hash(
            auth=auth,
            route_key=route_key,
            object_id=None,
            body=payload,
            expected_version=None,
        )
        replay = self._claim_provider_idempotency(auth=auth, key=key, request_hash=request_hash)
        if replay is not None:
            return replay
        correlation_id = new_uuid4()
        try:
            await cache.prepare_provider_write(calendar_id=calendar_id)
            result = await self._provider(cache).create_event(calendar_id, draft)
            if not isinstance(result, ICloudEventWriteResult):
                raise RuntimeError("provider returned an invalid event write result")
        except ProviderAdapterError as exc:
            return self._persist_provider_failure(
                auth=auth,
                key=key,
                request_hash=request_hash,
                correlation_id=correlation_id,
                exc=exc,
            )
        except Exception:
            return self._persist_uncertain_provider_failure(
                auth=auth,
                key=key,
                request_hash=request_hash,
                correlation_id=correlation_id,
            )
        try:
            with self.database.transaction() as connection:
                cache.reconcile_confirmed_event(result.event, connection=connection)
                canonical_id = self._canonical_event_id_for_provider(result.event.provider_event_id, connection=connection)
                canonical = self.repository.get_calendar_event(canonical_id)
                response = self._event_response(canonical, correlation_id=correlation_id)
                response_json = self.repository.store_idempotency_response(
                    audience=auth.audience,
                    key=key,
                    request_hash=request_hash,
                    response=response,
                    response_status=200,
                    correlation_id=correlation_id,
                )
            return StoredMutationResponse(response_json=response_json, status=200, replay=False)
        except Exception:
            return self._persist_uncertain_provider_failure(
                auth=auth,
                key=key,
                request_hash=request_hash,
                correlation_id=correlation_id,
            )

    async def update_provider_event(
        self,
        *,
        auth: AuthenticatedPlanningContext,
        key: str,
        event_id: str,
        expected_version: int,
        payload: Mapping[str, Any],
    ) -> StoredMutationResponse:
        self._require_provider_write_gate()
        cache = self._require_provider_cache()
        current, metadata = self._provider_event_preflight(
            cache,
            event_id=event_id,
            expected_version=expected_version,
        )
        draft = self._provider_event_draft(self._merge_provider_event_payload(current, payload))
        route_key = "PATCH /provider-events/{id}"
        request_hash = self._request_hash(
            auth=auth,
            route_key=route_key,
            object_id=event_id,
            body=payload,
            expected_version=expected_version,
        )
        replay = self._claim_provider_idempotency(auth=auth, key=key, request_hash=request_hash)
        if replay is not None:
            return replay
        correlation_id = new_uuid4()
        try:
            await cache.prepare_provider_write(
                calendar_id=str(metadata["provider_calendar_id"]),
                canonical_event_id=event_id,
            )
            result = await self._provider(cache).update_event(
                str(metadata["provider_event_id"]),
                etag=str(metadata["provider_etag"]),
                draft=draft,
            )
            if not isinstance(result, ICloudEventWriteResult):
                raise RuntimeError("provider returned an invalid event write result")
        except ProviderAdapterError as exc:
            return self._persist_provider_failure(
                auth=auth,
                key=key,
                request_hash=request_hash,
                correlation_id=correlation_id,
                exc=exc,
            )
        except Exception:
            return self._persist_uncertain_provider_failure(
                auth=auth,
                key=key,
                request_hash=request_hash,
                correlation_id=correlation_id,
            )
        try:
            with self.database.transaction() as connection:
                cache.reconcile_confirmed_event(result.event, connection=connection)
                canonical = self.repository.get_calendar_event(event_id)
                if canonical.provider_id != result.event.provider_event_id:
                    raise RuntimeError("provider identity changed during reconciliation")
                response = self._event_response(canonical, correlation_id=correlation_id)
                response_json = self.repository.store_idempotency_response(
                    audience=auth.audience,
                    key=key,
                    request_hash=request_hash,
                    response=response,
                    response_status=200,
                    correlation_id=correlation_id,
                )
            return StoredMutationResponse(response_json=response_json, status=200, replay=False)
        except Exception:
            return self._persist_uncertain_provider_failure(
                auth=auth,
                key=key,
                request_hash=request_hash,
                correlation_id=correlation_id,
            )

    async def delete_provider_event(
        self,
        *,
        auth: AuthenticatedPlanningContext,
        key: str,
        event_id: str,
        expected_version: int,
    ) -> StoredMutationResponse:
        self._require_provider_write_gate()
        cache = self._require_provider_cache()
        _, metadata = self._provider_event_preflight(
            cache,
            event_id=event_id,
            expected_version=expected_version,
        )
        route_key = "DELETE /provider-events/{id}"
        request_hash = self._request_hash(
            auth=auth,
            route_key=route_key,
            object_id=event_id,
            body={},
            expected_version=expected_version,
        )
        replay = self._claim_provider_idempotency(auth=auth, key=key, request_hash=request_hash)
        if replay is not None:
            return replay
        correlation_id = new_uuid4()
        try:
            await cache.prepare_provider_write(
                calendar_id=str(metadata["provider_calendar_id"]),
                canonical_event_id=event_id,
            )
            await self._provider(cache).delete_event(
                str(metadata["provider_event_id"]),
                etag=str(metadata["provider_etag"]),
            )
        except ProviderAdapterError as exc:
            return self._persist_provider_failure(
                auth=auth,
                key=key,
                request_hash=request_hash,
                correlation_id=correlation_id,
                exc=exc,
            )
        except Exception:
            return self._persist_uncertain_provider_failure(
                auth=auth,
                key=key,
                request_hash=request_hash,
                correlation_id=correlation_id,
            )
        try:
            with self.database.transaction() as connection:
                cache.reconcile_confirmed_deleted_event(event_id, connection=connection)
                canonical = self.repository.get_calendar_event(event_id)
                response = self._event_response(canonical, correlation_id=correlation_id)
                response_json = self.repository.store_idempotency_response(
                    audience=auth.audience,
                    key=key,
                    request_hash=request_hash,
                    response=response,
                    response_status=200,
                    correlation_id=correlation_id,
                )
            return StoredMutationResponse(response_json=response_json, status=200, replay=False)
        except Exception:
            return self._persist_uncertain_provider_failure(
                auth=auth,
                key=key,
                request_hash=request_hash,
                correlation_id=correlation_id,
            )

    async def create_provider_calendar(
        self,
        *,
        auth: AuthenticatedPlanningContext,
        key: str,
        payload: Mapping[str, Any],
    ) -> StoredMutationResponse:
        self._require_provider_write_gate()
        cache = self._require_provider_cache()
        if not self._provider_can_attempt():
            raise self._provider_not_configured_error()
        route_key = "POST /provider-calendars"
        request_hash = self._request_hash(
            auth=auth,
            route_key=route_key,
            object_id=None,
            body=payload,
            expected_version=None,
        )
        replay = self._claim_provider_idempotency(auth=auth, key=key, request_hash=request_hash)
        if replay is not None:
            return replay
        correlation_id = new_uuid4()
        try:
            await cache.prepare_provider_write()
            calendar = await self._provider(cache).create_calendar(
                str(payload["display_name"]),
                color=payload.get("color"),
            )
            if not isinstance(calendar, ExternalCalendar):
                raise RuntimeError("provider returned an invalid calendar write result")
        except ProviderAdapterError as exc:
            return self._persist_provider_failure(
                auth=auth,
                key=key,
                request_hash=request_hash,
                correlation_id=correlation_id,
                exc=exc,
            )
        except Exception:
            return self._persist_uncertain_provider_failure(
                auth=auth,
                key=key,
                request_hash=request_hash,
                correlation_id=correlation_id,
            )
        try:
            with self.database.transaction() as connection:
                cache.reconcile_created_calendar(calendar, connection=connection)
                destination = cache.calendar_destination(
                    calendar.provider_calendar_id,
                    writes_enabled=self.icloud_writes_enabled,
                )
                if destination is None:
                    raise RuntimeError("created calendar disappeared during reconciliation")
                response = self._calendar_destination_response(
                    destination,
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
        except Exception:
            return self._persist_uncertain_provider_failure(
                auth=auth,
                key=key,
                request_hash=request_hash,
                correlation_id=correlation_id,
            )

    async def delete_provider_calendar(
        self,
        *,
        auth: AuthenticatedPlanningContext,
        key: str,
        calendar_id: str,
    ) -> StoredMutationResponse:
        self._require_provider_write_gate()
        cache = self._require_provider_cache()
        self._require_writable_destination(cache, calendar_id)
        route_key = "DELETE /provider-calendars/{id}"
        request_hash = self._request_hash(
            auth=auth,
            route_key=route_key,
            object_id=calendar_id,
            body={},
            expected_version=None,
        )
        replay = self._claim_provider_idempotency(auth=auth, key=key, request_hash=request_hash)
        if replay is not None:
            return replay
        correlation_id = new_uuid4()
        try:
            await cache.prepare_provider_write(calendar_id=calendar_id)
            await self._provider(cache).delete_calendar(calendar_id)
        except ProviderAdapterError as exc:
            return self._persist_provider_failure(
                auth=auth,
                key=key,
                request_hash=request_hash,
                correlation_id=correlation_id,
                exc=exc,
            )
        except Exception:
            return self._persist_uncertain_provider_failure(
                auth=auth,
                key=key,
                request_hash=request_hash,
                correlation_id=correlation_id,
            )
        try:
            with self.database.transaction() as connection:
                cache.reconcile_deleted_calendar(calendar_id, connection=connection)
                response = {
                    "schemaVersion": "planning.v1",
                    "kind": "calendar_destination_deleted",
                    "domain": "calendar_destination",
                    "calendarId": calendar_id,
                    "deleted": True,
                    **self.envelopes.freshness(),
                    "correlation_id": correlation_id,
                }
                response_json = self.repository.store_idempotency_response(
                    audience=auth.audience,
                    key=key,
                    request_hash=request_hash,
                    response=response,
                    response_status=200,
                    correlation_id=correlation_id,
                )
            return StoredMutationResponse(response_json=response_json, status=200, replay=False)
        except Exception:
            return self._persist_uncertain_provider_failure(
                auth=auth,
                key=key,
                request_hash=request_hash,
                correlation_id=correlation_id,
            )

    def _require_provider_cache(self) -> Any:
        cache = self.provider_cache
        if cache is None or not self._provider_can_attempt(cache=cache):
            raise self._provider_not_configured_error()
        return cache

    def _provider_can_attempt(self, *, cache: Any | None = None) -> bool:
        selected = self.provider_cache if cache is None else cache
        if selected is None:
            return False
        availability = selected.write_availability()
        return bool(availability["configured"] and availability["providerAvailable"])

    def _require_provider_write_gate(self) -> None:
        if not self.icloud_writes_enabled:
            raise PlanningApiError(
                code="provider_write_disabled",
                message="iCloud calendar writes are disabled on this server.",
                status=503,
                retryable=False,
            )

    @staticmethod
    def _provider_not_configured_error() -> PlanningApiError:
        return PlanningApiError(
            code="provider_not_configured",
            message="The iCloud calendar provider is not configured.",
            status=503,
            retryable=False,
        )

    @staticmethod
    def _provider(cache: Any) -> Any:
        provider = getattr(cache, "provider", None)
        if provider is None:
            raise PlanningApiError(
                code="provider_not_configured",
                message="The iCloud calendar provider is not configured.",
                status=503,
            )
        return provider

    def _require_writable_destination(self, cache: Any, calendar_id: str) -> dict[str, Any]:
        destination = cache.calendar_destination(
            calendar_id,
            writes_enabled=self.icloud_writes_enabled,
        )
        if destination is None:
            raise PlanningApiError(
                code="provider_not_found",
                message="The requested iCloud calendar was not found.",
                status=404,
            )
        if destination["writeState"] == "read_only":
            raise PlanningApiError(
                code="provider_read_only",
                message="The requested iCloud calendar is not writable.",
                status=409,
            )
        return destination

    def _provider_event_preflight(
        self,
        cache: Any,
        *,
        event_id: str,
        expected_version: int,
    ) -> tuple[Any, dict[str, Any]]:
        current = self.event_service.get(event_id)
        if current.version != expected_version:
            raise PlanningVersionConflictError(
                "calendar_event",
                event_id,
                expected_version,
                current.version,
            )
        if current.deleted_at is not None:
            raise PlanningApiError(
                code="provider_not_found",
                message="The requested provider event was not found.",
                status=404,
            )
        metadata = cache.event_write_metadata(event_id)
        if metadata is None or current.source != "calendar-provider":
            raise PlanningApiError(
                code="provider_not_found",
                message="The requested iCloud event was not found.",
                status=404,
            )
        if (
            metadata.get("provider_id") != current.provider_id
            or metadata.get("provider_calendar_id") != current.provider_calendar_id
        ):
            raise PlanningApiError(
                code="provider_not_found",
                message="The requested iCloud event was not found.",
                status=404,
            )
        if (
            int(metadata.get("write_safe") or 0) != 1
            or str(metadata.get("recurrence_instance_key")) != "base"
            or not metadata.get("provider_etag")
        ):
            raise PlanningApiError(
                code="provider_read_only",
                message="This provider event is not safely writable.",
                status=409,
            )
        if metadata.get("can_write") in {False, 0} or int(metadata.get("calendar_enabled") or 0) != 1:
            raise PlanningApiError(
                code="provider_read_only",
                message="The event's iCloud calendar is not writable.",
                status=409,
            )
        return current, metadata

    @staticmethod
    def _provider_event_draft(payload: Mapping[str, Any]) -> ICloudEventDraft:
        draft = ICloudEventDraft(
            title=str(payload["title"]),
            all_day=payload["all_day"],
            timezone=str(payload["timezone"]),
            start_at_utc=payload.get("start_at_utc"),
            end_at_utc=payload.get("end_at_utc"),
            start_date=payload.get("start_date"),
            end_date_exclusive=payload.get("end_date_exclusive"),
            notes=payload.get("notes"),
            location=payload.get("location"),
        )
        try:
            draft.validate()
        except ProviderAdapterError as exc:
            raise PlanningApiError(
                code="validation_error",
                message="Provider event fields are inconsistent.",
                status=400,
            ) from exc
        return draft

    @staticmethod
    def _merge_provider_event_payload(current: Any, patch: Mapping[str, Any]) -> dict[str, Any]:
        values = {
            "title": current.title,
            "notes": current.notes,
            "location": current.location,
            "all_day": current.all_day,
            "timezone": current.timezone,
            "start_at_utc": current.start_at_utc,
            "end_at_utc": current.end_at_utc,
            "start_date": current.start_date,
            "end_date_exclusive": current.end_date_exclusive,
        }
        values.update(dict(patch))
        if values["all_day"]:
            values["start_at_utc"] = None
            values["end_at_utc"] = None
        else:
            values["start_date"] = None
            values["end_date_exclusive"] = None
        return values

    def _event_response(self, event: Any, *, correlation_id: str) -> dict[str, Any]:
        response = self.envelopes.object_response(
            domain="calendar_event",
            object_value=event.to_dict(),
            correlation_id=correlation_id,
        )
        response["mutationCapabilities"] = self._event_mutation_capabilities(event)
        return response

    def _calendar_destination_response(
        self,
        destination: Mapping[str, Any],
        *,
        correlation_id: str,
    ) -> dict[str, Any]:
        return {
            "schemaVersion": "planning.v1",
            "kind": "calendar_destination",
            "domain": "calendar_destination",
            "destination": dict(destination),
            **self.envelopes.freshness(),
            "correlation_id": correlation_id,
        }

    def _event_mutation_capabilities(self, event: Any) -> dict[str, Any]:
        active = event.deleted_at is None
        if event.source == "local_only" or (
            event.sync_state == "local_only"
            and event.provider_id is None
            and event.provider_calendar_id is None
        ):
            return {"canEdit": active, "canDelete": active}
        if self.provider_cache is None or event.provider_id is None or event.provider_calendar_id is None:
            return {"canEdit": False, "canDelete": False, "providerKind": "icloud", "writeState": "read_only"}
        metadata = self.provider_cache.event_write_metadata(event.id)
        can_write = bool(
            active
            and self.icloud_writes_enabled
            and self._provider_can_attempt()
            and metadata is not None
            and int(metadata.get("write_safe") or 0) == 1
            and str(metadata.get("recurrence_instance_key")) == "base"
            and bool(metadata.get("provider_etag"))
            and metadata.get("can_write") not in {False, 0}
            and int(metadata.get("calendar_enabled") or 0) == 1
        )
        if metadata is None or metadata.get("can_write") in {False, 0}:
            write_state = "read_only"
        elif can_write:
            write_state = "writable" if metadata.get("can_write") in {True, 1} else "candidate"
        else:
            write_state = "unavailable"
        return {
            "canEdit": can_write,
            "canDelete": can_write,
            "providerKind": "icloud",
            "writeState": write_state,
        }

    def _provider_capabilities(self) -> dict[str, Any]:
        if self.provider_cache is None:
            return {
                "providerKind": "icloud",
                "readIntegrationEnabled": False,
                "configured": False,
                "writesEnabled": self.icloud_writes_enabled,
                "canCreateCalendar": False,
            }
        availability = self.provider_cache.write_availability()
        return {
            "providerKind": "icloud",
            "readIntegrationEnabled": availability["readEnabled"],
            "configured": availability["configured"],
            "writesEnabled": self.icloud_writes_enabled,
            "canCreateCalendar": bool(self.icloud_writes_enabled and self._provider_can_attempt()),
        }

    def _claim_provider_idempotency(
        self,
        *,
        auth: AuthenticatedPlanningContext,
        key: str,
        request_hash: str,
    ) -> StoredMutationResponse | None:
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
            raise PlanningApiError(
                code="idempotency_in_progress",
                message="The provider mutation has no confirmed stored result; refresh before retrying.",
                status=409,
                details={"mutationState": "uncertain"},
                retryable=False,
            )
        return None

    def _persist_provider_failure(
        self,
        *,
        auth: AuthenticatedPlanningContext,
        key: str,
        request_hash: str,
        correlation_id: str,
        exc: ProviderAdapterError,
    ) -> StoredMutationResponse:
        return self._persist_provider_error_response(
            auth=auth,
            key=key,
            request_hash=request_hash,
            correlation_id=correlation_id,
            error=self._provider_api_error(exc),
        )

    def _persist_uncertain_provider_failure(
        self,
        *,
        auth: AuthenticatedPlanningContext,
        key: str,
        request_hash: str,
        correlation_id: str,
    ) -> StoredMutationResponse:
        return self._persist_provider_error_response(
            auth=auth,
            key=key,
            request_hash=request_hash,
            correlation_id=correlation_id,
            error=PlanningApiError(
                code="provider_mutation_uncertain",
                message="The provider mutation outcome is uncertain; refresh authoritative state before retrying.",
                status=503,
                details={"mutationState": "uncertain"},
                retryable=False,
            ),
        )

    def _persist_provider_error_response(
        self,
        *,
        auth: AuthenticatedPlanningContext,
        key: str,
        request_hash: str,
        correlation_id: str,
        error: PlanningApiError,
    ) -> StoredMutationResponse:
        response = self.envelopes.error_response(
            status=error.status,
            code=error.code,
            message=error.message,
            details=error.details,
            retryable=error.retryable,
            correlation_id=correlation_id,
            actor=auth.actor,
        )
        with self.database.transaction():
            response_json = self.repository.store_idempotency_response(
                audience=auth.audience,
                key=key,
                request_hash=request_hash,
                response=response,
                response_status=error.status,
                correlation_id=correlation_id,
            )
        return StoredMutationResponse(response_json=response_json, status=error.status, replay=False)

    @staticmethod
    def _provider_api_error(exc: ProviderAdapterError) -> PlanningApiError:
        code = str(exc.code)
        if code == ProviderFailureCode.WRITE_FORBIDDEN.value:
            return PlanningApiError("provider_read_only", "The provider rejected this calendar mutation as read-only.", 409)
        if code == ProviderFailureCode.ETAG_CONFLICT.value:
            return PlanningApiError("provider_etag_conflict", "The provider event changed elsewhere.", 409)
        if code == ProviderFailureCode.NOT_FOUND.value:
            return PlanningApiError("provider_not_found", "The provider resource was not found.", 404)
        if code == ProviderFailureCode.RATE_LIMITED.value:
            return PlanningApiError("provider_rate_limited", "The provider rate limit was reached.", 429, retryable=True)
        if code == ProviderFailureCode.AUTHENTICATION_FAILED.value:
            return PlanningApiError("provider_authentication_failed", "The provider authentication failed.", 502)
        if code == ProviderFailureCode.WRITE_INPUT_INVALID.value:
            return PlanningApiError("provider_payload_invalid", "The provider mutation payload is invalid.", 400)
        if code in {
            ProviderFailureCode.PAYLOAD_INVALID.value,
            ProviderFailureCode.CALENDAR_DATA_INVALID.value,
            ProviderFailureCode.XML_INVALID.value,
        }:
            return PlanningApiError("provider_payload_invalid", "The provider returned an invalid response.", 502)
        if code == ProviderFailureCode.METHOD_NOT_ALLOWED.value:
            return PlanningApiError("provider_transient_failure", "The provider write operation is unavailable.", 503, retryable=True)
        if code in {
            ProviderFailureCode.READBACK_UNCERTAIN.value,
            ProviderFailureCode.WRITE_STATUS_UNEXPECTED.value,
            ProviderFailureCode.SERVER_FAILURE.value,
            ProviderFailureCode.TIMEOUT.value,
            ProviderFailureCode.CONNECTION_TIMEOUT.value,
            ProviderFailureCode.READ_TIMEOUT.value,
            ProviderFailureCode.DNS_FAILED.value,
            ProviderFailureCode.CONNECTION_REFUSED.value,
            ProviderFailureCode.CONNECTION_FAILED.value,
            ProviderFailureCode.TLS_FAILED.value,
            ProviderFailureCode.CONNECTION_RESET.value,
            ProviderFailureCode.CONNECTION_ABORTED.value,
            ProviderFailureCode.SERVER_DISCONNECTED.value,
            ProviderFailureCode.TRANSPORT_UNKNOWN.value,
            ProviderFailureCode.READ_FAILED.value,
            ProviderFailureCode.FETCH_FAILED.value,
        }:
            return PlanningApiError(
                "provider_mutation_uncertain",
                "The provider mutation outcome is uncertain; refresh authoritative state before retrying.",
                503,
                details={"mutationState": "uncertain"},
                retryable=False,
            )
        if code == ProviderFailureCode.EVENT_WRITE_UNSUPPORTED.value:
            return PlanningApiError("provider_read_only", "This provider event is not safely writable.", 409)
        return PlanningApiError(
            "provider_mutation_uncertain",
            "The provider mutation outcome is uncertain; refresh authoritative state before retrying.",
            503,
            details={"mutationState": "uncertain"},
            retryable=False,
        )

    def _canonical_event_id_for_provider(
        self,
        provider_event_id: str,
        *,
        connection: Any,
    ) -> str:
        row = connection.execute(
            """
            SELECT canonical_event_id FROM provider_event_cache
            WHERE source_id = ? AND identity_key = ?
            """,
            (self.provider_cache.source_id, provider_event_id),
        ).fetchone()
        if row is None:
            raise RuntimeError("provider event reconciliation did not create a canonical mapping")
        return str(row["canonical_event_id"])

    def _mutate(
        self,
        *,
        auth: AuthenticatedPlanningContext,
        key: str,
        route_key: str,
        object_id: str | None,
        body: Mapping[str, Any],
        expected_version: int | None,
        precondition: Callable[[], None] | None = None,
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

            if precondition is not None:
                precondition()

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
