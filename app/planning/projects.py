"""Provider-neutral local project service."""

from __future__ import annotations

from typing import Any, Callable

from app.planning.errors import PlanningValidationError
from app.planning.models import MutationContext, Project, utc_now
from app.planning.repositories import PlanningRepository


_UNSET = object()


class ProjectService:
    """Formalizes project tombstones without reassignment or cascade behavior."""

    def __init__(
        self,
        database: Any,
        *,
        repository: PlanningRepository | None = None,
        now_fn: Callable[[], str] = utc_now,
    ) -> None:
        self.repository = repository or (
            database if isinstance(database, PlanningRepository) else PlanningRepository(database, now_fn=now_fn)
        )

    def get(self, project_id: str) -> Project:
        return self.repository.get_project(project_id)

    def list_active(self, *, limit: int = 1001, offset: int = 0) -> list[Project]:
        return self.repository.list_projects(limit=limit, offset=offset)

    def create(
        self,
        *,
        name: str,
        context: MutationContext,
        notes: str | None = None,
        source_ref: str | None = None,
    ) -> Project:
        return self.repository.create_project(name=name, notes=notes, source_ref=source_ref, context=context)

    def update(
        self,
        project_id: str,
        *,
        expected_version: int,
        context: MutationContext,
        name: str | object = _UNSET,
        notes: str | None | object = _UNSET,
    ) -> Project:
        fields: dict[str, Any] = {}
        if name is not _UNSET:
            fields["name"] = name
        if notes is not _UNSET:
            fields["notes"] = notes
        if not fields:
            raise PlanningValidationError("project update requires a mutable field")
        return self.repository.update_project(
            project_id,
            expected_version=expected_version,
            context=context,
            **fields,
        )

    def tombstone(self, project_id: str, *, expected_version: int, context: MutationContext) -> Project:
        return self.repository.delete_project(project_id, expected_version=expected_version, context=context)

    delete = tombstone
    archive = tombstone
