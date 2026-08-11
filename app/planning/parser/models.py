from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping


Confidence = Literal["high", "medium", "low"]
PlanningDomain = Literal["reminder", "task", "calendar_event"]
PlanningOperation = Literal["create", "query"]


@dataclass(frozen=True)
class ParserInput:
    """The only context the deterministic parser is allowed to consume."""

    utterance: str
    reference_time_utc: str
    timezone: str
    locale: str = "ru-RU"


@dataclass(frozen=True)
class Ambiguity:
    field: str
    candidates: tuple[str, ...]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "candidates": list(self.candidates),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class Candidate:
    """A closed, typed Planning candidate; it is not an executable command."""

    domain: PlanningDomain
    operation: PlanningOperation
    fields: Mapping[str, Any]
    normalized_paraphrase: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "operation": self.operation,
            "fields": dict(self.fields),
            "normalized_paraphrase": self.normalized_paraphrase,
        }


@dataclass(frozen=True)
class ParseResult:
    candidate: Candidate | None
    confidence: Confidence
    ambiguities: tuple[Ambiguity, ...] = ()
    requires_confirmation: bool = False
    normalized_text: str = ""
    error_code: str | None = None
    error_message: str | None = None

    @property
    def can_write(self) -> bool:
        return (
            self.candidate is not None
            and self.candidate.operation == "create"
            and self.confidence == "high"
            and not self.requires_confirmation
            and not self.ambiguities
            and self.error_code is None
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate": None if self.candidate is None else self.candidate.to_dict(),
            "confidence": self.confidence,
            "ambiguities": [item.to_dict() for item in self.ambiguities],
            "requires_confirmation": self.requires_confirmation,
            "normalized_text": self.normalized_text,
            "error_code": self.error_code,
        }
