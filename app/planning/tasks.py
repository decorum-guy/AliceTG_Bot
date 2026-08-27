"""Provider-neutral task domain service."""

from __future__ import annotations

from typing import Any, Callable

from app.planning.errors import PlanningValidationError
from app.planning.models import TASK_VIEWS, MutationContext, Task, utc_now, validate_timezone
from app.planning.repositories import PlanningRepository
from app.planning.service_time import UtcReference, caller_local_date


_UNSET = object()


def _repository(database: Any, repository: PlanningRepository | None, now_fn: Callable[[], str]) -> PlanningRepository:
    if repository is not None:
        return repository
    if isinstance(database, PlanningRepository):
        return database
    return PlanningRepository(database, now_fn=now_fn)


def _page(limit: int, offset: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1001:
        raise PlanningValidationError("task.list limit is out of range")
    if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 10_000:
        raise PlanningValidationError("task.list offset is out of range")


class TaskService:
    """Canonical task operations and explicit caller-timezone views."""

    def __init__(
        self,
        database: Any,
        *,
        repository: PlanningRepository | None = None,
        now_fn: Callable[[], str] = utc_now,
    ) -> None:
        self.repository = _repository(database, repository, now_fn)

    def create(self, *, context: MutationContext, **fields: Any) -> Task:
        allowed = {
            "title",
            "notes",
            "due_date",
            "due_time",
            "timezone",
            "priority",
            "project_id",
            "source_ref",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise PlanningValidationError(f"task service received unknown fields: {sorted(unknown)}")
        return self.repository.create_task(context=context, **fields)

    def update(
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
    ) -> Task:
        fields = {
            name: value
            for name, value in {
                "title": title,
                "notes": notes,
                "due_date": due_date,
                "due_time": due_time,
                "timezone": timezone,
                "priority": priority,
                "project_id": project_id,
            }.items()
            if value is not _UNSET
        }
        return self.repository.update_task(
            task_id,
            expected_version=expected_version,
            context=context,
            **fields,
        )

    def complete(self, task_id: str, *, expected_version: int, context: MutationContext) -> Task:
        return self.repository.complete_task(task_id, expected_version=expected_version, context=context)

    def archive(self, task_id: str, *, expected_version: int, context: MutationContext) -> Task:
        return self.repository.archive_task(task_id, expected_version=expected_version, context=context)

    def get(self, task_id: str) -> Task:
        return self.repository.get_task(task_id)

    def list_view(
        self,
        *,
        view: str,
        reference_time_utc: UtcReference,
        caller_timezone: str,
        project_id: str | None = None,
        limit: int = 1001,
        offset: int = 0,
    ) -> list[Task]:
        if view not in TASK_VIEWS:
            raise PlanningValidationError("task.view has an invalid enum")
        validate_timezone(caller_timezone, "caller_timezone")
        _page(limit, offset)
        today = caller_local_date(reference_time_utc, caller_timezone).isoformat()
        return self.repository.list_tasks(
            view=view,
            today=today,
            project_id=project_id,
            limit=limit,
            offset=offset,
        )

    def undated(
        self,
        *,
        reference_time_utc: UtcReference,
        caller_timezone: str,
        project_id: str | None = None,
        limit: int = 1001,
        offset: int = 0,
    ) -> list[Task]:
        return self.list_view(
            view="undated",
            reference_time_utc=reference_time_utc,
            caller_timezone=caller_timezone,
            project_id=project_id,
            limit=limit,
            offset=offset,
        )

    def today(
        self,
        *,
        reference_time_utc: UtcReference,
        caller_timezone: str,
        project_id: str | None = None,
        limit: int = 1001,
        offset: int = 0,
    ) -> list[Task]:
        return self.list_view(
            view="today",
            reference_time_utc=reference_time_utc,
            caller_timezone=caller_timezone,
            project_id=project_id,
            limit=limit,
            offset=offset,
        )

    def overdue(
        self,
        *,
        reference_time_utc: UtcReference,
        caller_timezone: str,
        project_id: str | None = None,
        limit: int = 1001,
        offset: int = 0,
    ) -> list[Task]:
        return self.list_view(
            view="overdue",
            reference_time_utc=reference_time_utc,
            caller_timezone=caller_timezone,
            project_id=project_id,
            limit=limit,
            offset=offset,
        )

    def upcoming(
        self,
        *,
        reference_time_utc: UtcReference,
        caller_timezone: str,
        project_id: str | None = None,
        limit: int = 1001,
        offset: int = 0,
    ) -> list[Task]:
        return self.list_view(
            view="upcoming",
            reference_time_utc=reference_time_utc,
            caller_timezone=caller_timezone,
            project_id=project_id,
            limit=limit,
            offset=offset,
        )
