from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any, Callable

from app.planning.db import PlanningDatabase
from app.planning.errors import PlanningTransactionRequiredError, PlanningValidationError
from app.planning.models import MutationContext, new_uuid4, utc_now, validate_uuid4


_SENSITIVE_KEY_PARTS = (
    "secret",
    "token",
    "password",
    "authorization",
    "cookie",
    "audio",
    "transcript",
)
_MAX_STRING_LENGTH = 512
_MAX_JSON_LENGTH = 8192
_MAX_COLLECTION_ITEMS = 64


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def _redact(value: Any, *, key: str | None = None, depth: int = 0) -> Any:
    if key is not None and _is_sensitive_key(key):
        return "[REDACTED]"
    if depth > 8:
        return "[TRUNCATED]"
    if isinstance(value, Mapping):
        items = list(value.items())[:_MAX_COLLECTION_ITEMS]
        result = {
            str(item_key): _redact(item_value, key=str(item_key), depth=depth + 1)
            for item_key, item_value in items
        }
        if len(value) > _MAX_COLLECTION_ITEMS:
            result["_truncated_fields"] = len(value) - _MAX_COLLECTION_ITEMS
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        result = [_redact(item, depth=depth + 1) for item in value[:_MAX_COLLECTION_ITEMS]]
        if len(value) > _MAX_COLLECTION_ITEMS:
            result.append(f"[TRUNCATED {len(value) - _MAX_COLLECTION_ITEMS} ITEMS]")
        return result
    if isinstance(value, (bytes, bytearray)):
        return "[REDACTED BYTES]"
    if isinstance(value, str) and len(value) > _MAX_STRING_LENGTH:
        return value[:_MAX_STRING_LENGTH] + "…[TRUNCATED]"
    return value


def bounded_redacted_json(value: Any, *, max_length: int = _MAX_JSON_LENGTH) -> str:
    try:
        redacted = _redact(value)
        encoded = json.dumps(redacted, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise PlanningValidationError("audit representation must be JSON-serializable") from exc
    if len(encoded) <= max_length:
        return encoded
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    bounded = {
        "_truncated": True,
        "sha256": digest,
        "length": len(encoded),
    }
    return json.dumps(bounded, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def reject_secret_fields(value: Any, *, field: str = "value", depth: int = 0) -> None:
    """Reject secret/audio-bearing field names from durable non-audit payloads."""

    if depth > 8:
        raise PlanningValidationError(f"{field} is nested too deeply")
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key)
            if _is_sensitive_key(key_text):
                raise PlanningValidationError(f"{field}.{key_text} is not allowed in SQLite storage")
            reject_secret_fields(item, field=f"{field}.{key_text}", depth=depth + 1)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, item in enumerate(value):
            reject_secret_fields(item, field=f"{field}[{index}]", depth=depth + 1)


class AuditWriter:
    """Writes bounded audit rows using the caller's surrounding transaction."""

    def __init__(
        self,
        database: PlanningDatabase,
        *,
        fail: bool = False,
        now_fn: Callable[[], str] = utc_now,
    ) -> None:
        self.database = database
        self.fail = fail
        self._now_fn = now_fn

    def record(
        self,
        *,
        context: MutationContext | None,
        action: str,
        object_domain: str,
        object_id: str,
        old_version: int | None,
        new_version: int | None,
        before: Any,
        after: Any,
        correlation_id: str | None = None,
    ) -> str:
        if not self.database.in_transaction:
            raise PlanningTransactionRequiredError("audit writes require a surrounding database transaction")
        if self.fail:
            raise RuntimeError("injected audit failure")
        validate_uuid4(object_id, "audit.object_id")
        if context is not None:
            context.validate()
        audit_id = new_uuid4()
        event_correlation_id = correlation_id or (context.audit_correlation_id if context else new_uuid4())
        validate_uuid4(event_correlation_id, "audit.correlation_id")
        if old_version is not None and old_version < 1:
            raise PlanningValidationError("audit.old_version must be positive")
        if new_version is not None and new_version < 1:
            raise PlanningValidationError("audit.new_version must be positive")
        if not action or len(action) > 128:
            raise PlanningValidationError("audit.action is out of bounds")
        before_json = None if before is None else bounded_redacted_json(before)
        after_json = None if after is None else bounded_redacted_json(after)
        self.database.connection.execute(
            """
            INSERT INTO audit_events(
                id, actor_id, actor_type, audience, surface, action,
                object_domain, object_id, old_version, new_version,
                correlation_id, before_json, after_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                audit_id,
                context.actor_id if context else None,
                context.actor_type if context else None,
                context.audience if context else None,
                context.surface if context else None,
                action,
                object_domain,
                object_id,
                old_version,
                new_version,
                event_correlation_id,
                before_json,
                after_json,
                self._now_fn(),
            ),
        )
        return audit_id
