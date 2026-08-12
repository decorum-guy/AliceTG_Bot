from __future__ import annotations

from dataclasses import dataclass


class PlanningError(Exception):
    """Base class for failures in the internal Planning storage foundation."""


class PlanningConfigurationError(PlanningError):
    pass


class PlanningValidationError(PlanningError, ValueError):
    pass


class PlanningLocalTimeError(PlanningValidationError):
    """A local wall-clock value cannot be resolved deterministically."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class PlanningNotFoundError(PlanningError):
    pass


class PlanningTransactionRequiredError(PlanningError):
    pass


class TelegramActionTokenError(PlanningError):
    """A persisted Telegram mutation capability cannot be used safely."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class TelegramActionTokenUnknownError(TelegramActionTokenError):
    def __init__(self) -> None:
        super().__init__("unknown_token")


class TelegramActionTokenExpiredError(TelegramActionTokenError):
    def __init__(self) -> None:
        super().__init__("expired_token")


class TelegramActionTokenConsumedError(TelegramActionTokenError):
    def __init__(self) -> None:
        super().__init__("consumed_token")


class TelegramActionTokenBindingError(TelegramActionTokenError):
    def __init__(self, reason: str) -> None:
        if reason not in {"wrong_user", "wrong_chat", "wrong_action", "wrong_domain"}:
            raise ValueError("invalid Telegram action token binding reason")
        super().__init__(reason)


class PlanningLeaseLostError(PlanningError):
    """The worker no longer owns the durable job lease."""

    pass


class PlanningMigrationError(PlanningError):
    pass


class PlanningNewerSchemaError(PlanningMigrationError):
    pass


@dataclass
class PlanningVersionConflictError(PlanningError):
    domain: str
    object_id: str
    expected_version: int
    actual_version: int | None

    def __str__(self) -> str:
        actual = "missing" if self.actual_version is None else str(self.actual_version)
        return (
            f"{self.domain} {self.object_id} expected version "
            f"{self.expected_version}, actual version {actual}"
        )


@dataclass
class PlanningIdempotencyConflictError(PlanningError):
    audience: str
    key: str

    def __str__(self) -> str:
        return f"idempotency key is already bound to a different request: {self.audience}/{self.key}"


@dataclass
class PlanningIdempotencyInProgressError(PlanningError):
    audience: str
    key: str

    def __str__(self) -> str:
        return f"idempotency key is currently in progress: {self.audience}/{self.key}"
