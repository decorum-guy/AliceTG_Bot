"""Closed provider-neutral Planning capability metadata."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class TaskCapabilityMetadata:
    read: bool = True
    create: bool = True
    update: bool = True
    complete: bool = True
    archive: bool = True
    local_authoritative: bool = True


@dataclass(frozen=True)
class EventCapabilityMetadata:
    read: bool = True
    create: bool = True
    update: bool = True
    delete: bool = True
    recurrence: bool = False
    provider_sync: bool = False
    local_only: bool = True


@dataclass(frozen=True)
class ProjectCapabilityMetadata:
    read: bool = True
    create: bool = True
    update: bool = True
    archive: bool = True
    local_management: bool = True


@dataclass(frozen=True)
class PlanningCapabilityMetadata:
    tasks: TaskCapabilityMetadata = TaskCapabilityMetadata()
    events: EventCapabilityMetadata = EventCapabilityMetadata()
    projects: ProjectCapabilityMetadata = ProjectCapabilityMetadata()

    def to_dict(self) -> dict[str, dict[str, Any]]:
        return {
            "tasks": asdict(self.tasks),
            "events": asdict(self.events),
            "projects": asdict(self.projects),
        }


def planning_capability_metadata() -> PlanningCapabilityMetadata:
    return PlanningCapabilityMetadata()
