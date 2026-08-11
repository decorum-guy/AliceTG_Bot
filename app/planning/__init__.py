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
    PlanningLeaseLostError,
    PlanningMigrationError,
    PlanningNewerSchemaError,
    PlanningNotFoundError,
    PlanningTransactionRequiredError,
    PlanningValidationError,
    PlanningVersionConflictError,
    TelegramActionTokenBindingError,
    TelegramActionTokenConsumedError,
    TelegramActionTokenError,
    TelegramActionTokenExpiredError,
    TelegramActionTokenUnknownError,
)
from app.planning.models import (
    CalendarEvent,
    DeliveryAttempt,
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
from app.planning.telegram_actions import (
    IssuedTelegramAction,
    TelegramActionToken,
    TelegramActionTokenStore,
    TelegramMutationRateLimiter,
)
from app.planning.telegram_ui import (
    PlanningActionOutcome,
    PlanningButton,
    PlanningTelegramService,
    PlanningTelegramRateLimited,
    PlanningView,
)

__all__ = [
    "AuditWriter",
    "CalendarEvent",
    "DeliveryAttempt",
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
    "PlanningLeaseLostError",
    "PlanningMigrationError",
    "PlanningNewerSchemaError",
    "PlanningNotFoundError",
    "PlanningRepository",
    "PlanningTransactionRequiredError",
    "PlanningValidationError",
    "PlanningVersionConflictError",
    "TelegramActionTokenBindingError",
    "TelegramActionTokenConsumedError",
    "TelegramActionTokenError",
    "TelegramActionTokenExpiredError",
    "TelegramActionTokenUnknownError",
    "Project",
    "ProviderMapping",
    "Reminder",
    "SyncConflict",
    "SyncCursor",
    "Task",
    "IssuedTelegramAction",
    "PlanningActionOutcome",
    "PlanningButton",
    "PlanningTelegramRateLimited",
    "PlanningTelegramService",
    "PlanningView",
    "TelegramActionToken",
    "TelegramActionTokenStore",
    "TelegramMutationRateLimiter",
    "bounded_redacted_json",
    "reject_secret_fields",
]
