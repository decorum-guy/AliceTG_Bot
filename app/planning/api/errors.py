from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PlanningApiError(Exception):
    """A safe, machine-readable error at the HTTP adapter boundary."""

    code: str
    message: str
    status: int
    details: dict[str, Any] = field(default_factory=dict)
    retryable: bool = False

    def __post_init__(self) -> None:
        super().__init__(self.message)
