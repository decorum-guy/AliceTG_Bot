from __future__ import annotations

import hashlib
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, TypeVar

from app.planning.db import PlanningDatabase
from app.planning.errors import (
    TelegramActionTokenBindingError,
    TelegramActionTokenConsumedError,
    TelegramActionTokenExpiredError,
    TelegramActionTokenUnknownError,
)
from app.planning.models import validate_utc_timestamp, validate_uuid4, utc_now


TELEGRAM_ACTION_CALLBACK_PREFIX = "planning:a:"
TELEGRAM_ACTIONS = frozenset(
    {"reminder_complete", "reminder_cancel", "reminder_retry", "task_complete"}
)
TELEGRAM_ACTION_DOMAINS = frozenset({"reminder", "task"})
TELEGRAM_ACTION_CALLBACK_MAX_BYTES = 64
TELEGRAM_ACTION_TOKEN_MIN_TTL_SECONDS = 60
TELEGRAM_ACTION_TOKEN_MAX_TTL_SECONDS = 3_600
TELEGRAM_ACTION_TOKEN_BYTES = 32

T = TypeVar("T")


def validate_action_token_ttl(seconds: int) -> int:
    if (
        isinstance(seconds, bool)
        or not isinstance(seconds, int)
        or not TELEGRAM_ACTION_TOKEN_MIN_TTL_SECONDS <= seconds <= TELEGRAM_ACTION_TOKEN_MAX_TTL_SECONDS
    ):
        raise ValueError(
            "PLANNING_TELEGRAM_ACTION_TOKEN_TTL_SECONDS must be between "
            f"{TELEGRAM_ACTION_TOKEN_MIN_TTL_SECONDS} and {TELEGRAM_ACTION_TOKEN_MAX_TTL_SECONDS}"
        )
    return seconds


def _parse_utc(value: str) -> datetime:
    validate_utc_timestamp(value, "telegram_action.timestamp")
    return datetime.fromisoformat(value[:-1] + "+00:00")


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _digest(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("ascii")).hexdigest()


def _raw_token_from_callback(callback_data: str) -> str:
    if not isinstance(callback_data, str) or not callback_data.startswith(TELEGRAM_ACTION_CALLBACK_PREFIX):
        raise TelegramActionTokenUnknownError()
    raw_token = callback_data[len(TELEGRAM_ACTION_CALLBACK_PREFIX) :]
    # token_urlsafe(32) is 43 ASCII characters.  Rejecting other shapes
    # bounds parsing and makes malformed/tampered callback data cheap to fail.
    if len(raw_token) != 43 or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-" for character in raw_token):
        raise TelegramActionTokenUnknownError()
    return raw_token


@dataclass(frozen=True)
class TelegramActionToken:
    token_digest: str
    action: str
    domain: str
    object_id: str
    expected_version: int
    telegram_user_id: int
    telegram_chat_id: int | None
    created_at: str
    expires_at: str


@dataclass(frozen=True)
class IssuedTelegramAction:
    callback_data: str
    expires_at: str


class TelegramActionTokenStore:
    """Persistent opaque capabilities with transactional consumption.

    ``consume`` deliberately invokes the domain mutation before marking the
    token consumed, inside the same SQLite transaction.  A raised mutation
    rolls back both the domain update and consumption, while a second click
    waits for the first transaction and then observes ``consumed_at``.
    """

    def __init__(
        self,
        database: PlanningDatabase,
        *,
        ttl_seconds: int = 900,
        now_fn: Callable[[], str] = utc_now,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        self.database = database
        self.ttl_seconds = validate_action_token_ttl(ttl_seconds)
        self._now_fn = now_fn
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(TELEGRAM_ACTION_TOKEN_BYTES))

    def issue(
        self,
        *,
        action: str,
        domain: str,
        object_id: str,
        expected_version: int,
        telegram_user_id: int,
        telegram_chat_id: int | None,
        now: str | None = None,
    ) -> IssuedTelegramAction:
        if action not in TELEGRAM_ACTIONS:
            raise ValueError("Telegram action is not allowlisted")
        if domain not in TELEGRAM_ACTION_DOMAINS:
            raise ValueError("Telegram action domain is not allowlisted")
        if (action.startswith("reminder_") and domain != "reminder") or (
            action == "task_complete" and domain != "task"
        ):
            raise ValueError("Telegram action/domain pair is not allowlisted")
        validate_uuid4(object_id, "telegram_action.object_id")
        if isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("Telegram action expected version must be positive")
        if isinstance(telegram_user_id, bool) or not isinstance(telegram_user_id, int):
            raise ValueError("Telegram action user ID must be an integer")
        if telegram_chat_id is not None and (
            isinstance(telegram_chat_id, bool) or not isinstance(telegram_chat_id, int)
        ):
            raise ValueError("Telegram action chat ID must be an integer")

        created_dt = _parse_utc(now or self._now_fn())
        created_at = _timestamp(created_dt)
        expires_at = _timestamp(created_dt + timedelta(seconds=self.ttl_seconds))

        for _ in range(3):
            raw_token = self._token_factory()
            if not isinstance(raw_token, str) or len(raw_token) != 43:
                raise ValueError("Telegram action token factory returned an invalid token")
            try:
                callback_data = encode_action_callback(raw_token)
            except ValueError:
                raise
            token_digest = _digest(raw_token)
            with self.database.transaction():
                self._cleanup_expired_in_transaction(now=created_at, limit=100)
                try:
                    self.database.connection.execute(
                        """
                        INSERT INTO telegram_action_tokens(
                            token_digest, action, domain, object_id, expected_version,
                            telegram_user_id, telegram_chat_id, created_at, expires_at, consumed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                        """,
                        (
                            token_digest,
                            action,
                            domain,
                            object_id,
                            expected_version,
                            telegram_user_id,
                            telegram_chat_id,
                            created_at,
                            expires_at,
                        ),
                    )
                except Exception as exc:
                    # A cryptographically improbable digest collision is the
                    # only expected insertion conflict.  Retry that one case;
                    # surface all other database failures unchanged.
                    if "UNIQUE constraint failed: telegram_action_tokens.token_digest" not in str(exc):
                        raise
                    continue
            return IssuedTelegramAction(callback_data=callback_data, expires_at=expires_at)
        raise RuntimeError("could not issue a unique Telegram action token")

    def consume(
        self,
        callback_data: str,
        *,
        telegram_user_id: int,
        telegram_chat_id: int | None,
        mutation: Callable[[TelegramActionToken], T],
        expected_action: str | None = None,
        expected_domain: str | None = None,
        now: str | None = None,
    ) -> T:
        raw_token = _raw_token_from_callback(callback_data)
        digest = _digest(raw_token)
        now_dt = _parse_utc(now or self._now_fn())
        now_value = _timestamp(now_dt)
        with self.database.transaction():
            row = self.database.connection.execute(
                "SELECT * FROM telegram_action_tokens WHERE token_digest = ?",
                (digest,),
            ).fetchone()
            if row is None:
                raise TelegramActionTokenUnknownError()
            if row["consumed_at"] is not None:
                raise TelegramActionTokenConsumedError()
            if _parse_utc(str(row["expires_at"])) <= now_dt:
                raise TelegramActionTokenExpiredError()
            if int(row["telegram_user_id"]) != telegram_user_id:
                raise TelegramActionTokenBindingError("wrong_user")
            stored_chat_id = row["telegram_chat_id"]
            if stored_chat_id is not None and telegram_chat_id != int(stored_chat_id):
                raise TelegramActionTokenBindingError("wrong_chat")
            action = str(row["action"])
            domain = str(row["domain"])
            if expected_action is not None and action != expected_action:
                raise TelegramActionTokenBindingError("wrong_action")
            if expected_domain is not None and domain != expected_domain:
                raise TelegramActionTokenBindingError("wrong_domain")

            token = TelegramActionToken(
                token_digest=digest,
                action=action,
                domain=domain,
                object_id=str(row["object_id"]),
                expected_version=int(row["expected_version"]),
                telegram_user_id=int(row["telegram_user_id"]),
                telegram_chat_id=None if stored_chat_id is None else int(stored_chat_id),
                created_at=str(row["created_at"]),
                expires_at=str(row["expires_at"]),
            )
            result = mutation(token)
            cursor = self.database.connection.execute(
                """
                UPDATE telegram_action_tokens
                SET consumed_at = ?
                WHERE token_digest = ? AND consumed_at IS NULL
                """,
                (now_value, digest),
            )
            if cursor.rowcount != 1:
                raise TelegramActionTokenConsumedError()
            return result

    def cleanup_expired(self, *, now: str | None = None, limit: int = 100) -> int:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("Telegram action token cleanup limit is out of range")
        now_value = _timestamp(_parse_utc(now or self._now_fn()))
        with self.database.transaction():
            return self._cleanup_expired_in_transaction(now=now_value, limit=limit)

    def _cleanup_expired_in_transaction(self, *, now: str, limit: int) -> int:
        cursor = self.database.connection.execute(
            """
            DELETE FROM telegram_action_tokens
            WHERE token_digest IN (
                SELECT token_digest
                FROM telegram_action_tokens
                WHERE expires_at <= ?
                ORDER BY expires_at, token_digest
                LIMIT ?
            )
            """,
            (now, limit),
        )
        return cursor.rowcount


def encode_action_callback(raw_token: str) -> str:
    if not isinstance(raw_token, str) or len(raw_token) != 43:
        raise ValueError("Telegram action token has an invalid length")
    if any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-" for character in raw_token):
        raise ValueError("Telegram action token has invalid characters")
    callback_data = f"{TELEGRAM_ACTION_CALLBACK_PREFIX}{raw_token}"
    if len(callback_data.encode("utf-8")) > TELEGRAM_ACTION_CALLBACK_MAX_BYTES:
        raise ValueError("Telegram action callback exceeds 64 bytes")
    return callback_data


class TelegramMutationRateLimiter:
    """Small process-local limiter for mutation callbacks."""

    def __init__(
        self,
        *,
        limit: int = 30,
        window_seconds: float = 60.0,
        now_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("Telegram mutation rate limit must be positive")
        if window_seconds <= 0:
            raise ValueError("Telegram mutation rate window must be positive")
        self.limit = limit
        self.window_seconds = float(window_seconds)
        self._now_fn = now_fn
        self._calls: dict[int, deque[float]] = defaultdict(deque)

    def allow(self, telegram_user_id: int) -> bool:
        now = self._now_fn()
        calls = self._calls[telegram_user_id]
        cutoff = now - self.window_seconds
        while calls and calls[0] <= cutoff:
            calls.popleft()
        if len(calls) >= self.limit:
            return False
        calls.append(now)
        return True
