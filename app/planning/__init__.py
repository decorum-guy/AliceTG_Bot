"""Internal durable Planning storage foundation.

The generic A1 repositories remain provider-neutral. A2's legacy reminder
import and compatibility adapter live in the separate ``legacy_import``
boundary and are enabled only by an explicit cutover gate.
"""

from app.planning.audit import AuditWriter, bounded_redacted_json, reject_secret_fields
from app.planning.db import (
    DEFAULT_PLANNING_DB_PATH,
    PlanningDatabase,
    PlanningDatabaseConfig,
)
from app.planning.errors import (
    PlanningConfigurationError,
    PlanningError,
    PlanningIdempotencyConflictError,
    PlanningIdempotencyInProgressError,
    PlanningMigrationError,
    PlanningNewerSchemaError,
    PlanningNotFoundError,
    PlanningTransactionRequiredError,
    PlanningValidationError,
    PlanningVersionConflictError,
)
from app.planning.models import (
    CalendarEvent,
    IdempotencyClaim,
    MutationContext,
    OutboxJob,
    Project,
    ProviderMapping,
    Reminder,
    SyncConflict,
    SyncCursor,
    Task,
)
from app.planning.repositories import PlanningRepository

__all__ = [
    "AuditWriter",
    "CalendarEvent",
    "DEFAULT_PLANNING_DB_PATH",
    "IdempotencyClaim",
    "MutationContext",
    "OutboxJob",
    "PlanningConfigurationError",
    "PlanningDatabase",
    "PlanningDatabaseConfig",
    "PlanningError",
    "PlanningIdempotencyConflictError",
    "PlanningIdempotencyInProgressError",
    "PlanningMigrationError",
    "PlanningNewerSchemaError",
    "PlanningNotFoundError",
    "PlanningRepository",
    "PlanningTransactionRequiredError",
    "PlanningValidationError",
    "PlanningVersionConflictError",
    "Project",
    "ProviderMapping",
    "Reminder",
    "SyncConflict",
    "SyncCursor",
    "Task",
    "bounded_redacted_json",
    "reject_secret_fields",
]
