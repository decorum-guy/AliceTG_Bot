from __future__ import annotations

import asyncio
import inspect
import logging
import random
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Mapping

from app.planning.delivery import DeliveryResult, ReminderChannelTransport
from app.planning.errors import PlanningLeaseLostError, PlanningVersionConflictError
from app.planning.models import (
    MutationContext,
    REMINDER_DELIVERY_JOB_TYPE,
    REMINDER_OUTBOX_DEDUPE_PREFIX,
    Reminder,
    utc_now,
    validate_utc_timestamp,
)
from app.planning.repositories import PlanningRepository
from app.services.reminder_store import ReminderSettings

LOGGER = logging.getLogger(__name__)

RETRY_DELAYS_SECONDS = (30, 120, 600, 1_800, 7_200, 21_600, 43_200)
MAX_DELIVERY_ATTEMPTS = 8
DELIVERY_RETRY_WINDOW_SECONDS = 24 * 60 * 60
DEFAULT_POLL_INTERVAL_SECONDS = 5.0
DEFAULT_LEASE_SECONDS = 60
DEFAULT_JITTER_SECONDS = 5.0

SCHEDULER_CONTEXT = MutationContext(
    audience="operator",
    actor_id="planning-scheduler",
    actor_type="service",
    surface="system",
)


def _normalise_now(value: str | datetime) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("scheduler clock must be timezone-aware")
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    validate_utc_timestamp(value, "scheduler.now")
    return value


def _as_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value[:-1] + "+00:00")


def _as_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class SchedulerRun:
    processed_jobs: int
    reconciled_reminders: int


Hook = Callable[[Reminder, str], Awaitable[None] | None]


class DurableReminderScheduler:
    """DB-backed reminder scheduler and delivery outbox worker.

    The scheduler owns due detection, leases, retry timing and durable state.
    Channel adapters only perform one provider attempt and return a typed
    result.  ``run_once`` is deterministic when supplied an injected clock and
    jitter function; ``run_forever`` is the production polling lifecycle.
    """

    def __init__(
        self,
        database: Any,
        *,
        telegram_transport: ReminderChannelTransport,
        mobile_transport: ReminderChannelTransport | None,
        default_chat_id: int,
        settings_provider: Callable[[], Awaitable[ReminderSettings]] | None = None,
        interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        batch_size: int = 10,
        worker_id: str | None = None,
        now_fn: Callable[[], str | datetime] = utc_now,
        jitter_fn: Callable[[int], float] | None = None,
        jitter_bound_seconds: float = DEFAULT_JITTER_SECONDS,
        before_provider_send: Hook | None = None,
        after_provider_send: Hook | None = None,
    ) -> None:
        if not 5.0 <= interval_seconds <= 10.0:
            raise ValueError("durable reminder scheduler interval must be between 5 and 10 seconds")
        if lease_seconds <= 0:
            raise ValueError("durable reminder scheduler lease must be positive")
        if batch_size <= 0:
            raise ValueError("durable reminder scheduler batch size must be positive")
        if isinstance(default_chat_id, bool) or not isinstance(default_chat_id, int):
            raise ValueError("durable reminder scheduler default chat id must be an integer")
        if jitter_bound_seconds < 0:
            raise ValueError("durable reminder scheduler jitter bound must not be negative")

        self._database = database
        self._current_now = _normalise_now(now_fn())
        self._clock_fn = now_fn
        self._repository = PlanningRepository(database, now_fn=lambda: self._current_now)
        self._telegram_transport = telegram_transport
        self._mobile_transport = mobile_transport
        self._default_chat_id = default_chat_id
        self._settings_provider = settings_provider
        self._interval_seconds = interval_seconds
        self._lease_seconds = lease_seconds
        self._batch_size = batch_size
        self._worker_id = worker_id or f"planning-scheduler-{id(self)}"
        self._jitter_fn = jitter_fn or (lambda _base: random.uniform(-jitter_bound_seconds, jitter_bound_seconds))
        self._jitter_bound_seconds = jitter_bound_seconds
        self._before_provider_send = before_provider_send
        self._after_provider_send = after_provider_send
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def worker_id(self) -> str:
        return self._worker_id

    @property
    def repository(self) -> PlanningRepository:
        return self._repository

    async def startup(self, now: str | datetime | None = None) -> SchedulerRun:
        """Reconcile and process overdue work immediately on startup."""

        return await self.run_once(now)

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            raise RuntimeError("durable reminder scheduler is already running")
        self._stop_event.clear()
        await self.startup()
        self._task = asyncio.create_task(self.run_forever())

    async def close(self) -> None:
        self._stop_event.set()
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def run_forever(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Durable reminder scheduler iteration failed")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._interval_seconds)
            except asyncio.TimeoutError:
                continue

    async def run_once(self, now: str | datetime | None = None) -> SchedulerRun:
        selected_now = _normalise_now(now if now is not None else self._clock_fn())
        self._current_now = selected_now
        reconciled = self._reconcile(selected_now)
        processed = 0
        for _ in range(self._batch_size):
            lease_expires_at = _as_timestamp(
                _as_datetime(selected_now) + timedelta(seconds=self._lease_seconds)
            )
            job = self._repository.claim_outbox(
                job_type=REMINDER_DELIVERY_JOB_TYPE,
                lease_owner=self._worker_id,
                now=selected_now,
                lease_expires_at=lease_expires_at,
            )
            if job is None:
                break
            processed += 1
            await self._process_job(job, selected_now)
        return SchedulerRun(processed_jobs=processed, reconciled_reminders=reconciled)

    def reconcile_due(self, now: str | datetime | None = None) -> int:
        """Run only deterministic due/job reconciliation without provider sends."""

        selected_now = _normalise_now(now if now is not None else self._clock_fn())
        self._current_now = selected_now
        return self._reconcile(selected_now)

    def _reconcile(self, now: str) -> int:
        now_dt = _as_datetime(now)
        reconciled = 0
        with self._database.transaction():
            rows = self._database.connection.execute(
                """
                SELECT * FROM reminders
                WHERE deleted_at IS NULL AND status IN ('pending', 'due')
                ORDER BY due_at_utc, id
                """
            ).fetchall()
            for row in rows:
                reminder = self._repository.get_reminder(str(row["id"]))
                due_dt = _as_datetime(reminder.due_at_utc)
                if reminder.status == "pending" and due_dt <= now_dt:
                    previous = reminder
                    next_state = "queued" if reminder.delivery_state == "not_due" else reminder.delivery_state
                    reminder = replace(
                        reminder,
                        status="due",
                        delivery_state=next_state,  # type: ignore[arg-type]
                        version=reminder.version + 1,
                        updated_at=now,
                    )
                    self._database.connection.execute(
                        """
                        UPDATE reminders
                        SET status = ?, delivery_state = ?, version = ?, updated_at = ?
                        WHERE id = ? AND version = ?
                        """,
                        (
                            reminder.status,
                            reminder.delivery_state,
                            reminder.version,
                            reminder.updated_at,
                            reminder.id,
                            previous.version,
                        ),
                    )
                    self._repository._record_audit(
                        context=SCHEDULER_CONTEXT,
                        action="reminder_due",
                        domain=reminder.domain,
                        object_id=reminder.id,
                        old_version=previous.version,
                        new_version=reminder.version,
                        before={"status": previous.status, "delivery_state": previous.delivery_state},
                        after={"status": reminder.status, "delivery_state": reminder.delivery_state},
                    )
                    reconciled += 1
                elif reminder.status == "due" and reminder.delivery_state == "not_due":
                    previous = reminder
                    reminder = replace(
                        reminder,
                        delivery_state="queued",
                        version=reminder.version + 1,
                        updated_at=now,
                    )
                    self._database.connection.execute(
                        """
                        UPDATE reminders
                        SET delivery_state = ?, version = ?, updated_at = ?
                        WHERE id = ? AND version = ?
                        """,
                        (
                            reminder.delivery_state,
                            reminder.version,
                            reminder.updated_at,
                            reminder.id,
                            previous.version,
                        ),
                    )
                    reconciled += 1

                dedupe_key = f"{REMINDER_OUTBOX_DEDUPE_PREFIX}{reminder.id}"
                existing = self._database.connection.execute(
                    "SELECT * FROM outbox WHERE dedupe_key = ?",
                    (dedupe_key,),
                ).fetchone()
                if reminder.delivery_state in {"delivered", "failed"}:
                    self._reconcile_terminal_job(reminder, existing, now, dedupe_key)
                    continue

                available_at = reminder.next_attempt_at or (
                    reminder.due_at_utc if reminder.delivery_state == "not_due" else now
                )
                if reminder.delivery_state == "retrying" and reminder.next_attempt_at is None:
                    available_at = now
                payload = self._delivery_payload(reminder.id)
                if existing is None:
                    self._repository.ensure_reminder_outbox(
                        reminder_id=reminder.id,
                        job_type=REMINDER_DELIVERY_JOB_TYPE,
                        payload=payload,
                        available_at=available_at,
                        dedupe_key=dedupe_key,
                        correlation_id=reminder.audit_correlation_id,
                    )
                    self._repository._record_audit(
                        context=SCHEDULER_CONTEXT,
                        action="delivery_queued",
                        domain=reminder.domain,
                        object_id=reminder.id,
                        old_version=reminder.version,
                        new_version=reminder.version,
                        before=None,
                        after={"available_at": available_at, "job_type": REMINDER_DELIVERY_JOB_TYPE},
                    )
                    reconciled += 1
                elif str(existing["status"]) == "cancelled":
                    self._repository.ensure_reminder_outbox(
                        reminder_id=reminder.id,
                        job_type=REMINDER_DELIVERY_JOB_TYPE,
                        payload=payload,
                        available_at=available_at,
                        dedupe_key=dedupe_key,
                        correlation_id=reminder.audit_correlation_id,
                        requeue_cancelled=True,
                    )
                    reconciled += 1
                elif str(existing["status"]) == "succeeded":
                    # A crash after the remote send but before the local
                    # success commit is the documented at-least-once window.
                    # Requeue the same logical job so the reminder is not lost.
                    self._database.connection.execute(
                        """
                        UPDATE outbox
                        SET status = 'queued', available_at = ?, lease_owner = NULL,
                            lease_expires_at = NULL, lease_token = NULL, last_error = NULL,
                            last_error_code = NULL, updated_at = ?
                        WHERE id = ? AND status = 'succeeded'
                        """,
                        (available_at, now, str(existing["id"])),
                    )
                    reconciled += 1
                elif str(existing["status"]) == "failed":
                    # A failed outbox row with an active reminder is an
                    # inconsistent/partially committed state.  Recover the
                    # logical job without resetting its attempt history; the
                    # canonical reminder state still controls whether it is
                    # eligible for delivery.
                    self._database.connection.execute(
                        """
                        UPDATE outbox
                        SET status = 'queued', available_at = ?, lease_owner = NULL,
                            lease_expires_at = NULL, lease_token = NULL, last_error = NULL,
                            last_error_code = NULL, updated_at = ?
                        WHERE id = ? AND status = 'failed'
                        """,
                        (available_at, now, str(existing["id"])),
                    )
                    reconciled += 1
                elif str(existing["status"]) == "leased" and existing["lease_expires_at"] is not None:
                    if str(existing["lease_expires_at"]) <= now:
                        self._database.connection.execute(
                            """
                            UPDATE outbox
                            SET status = 'queued', available_at = ?, lease_owner = NULL,
                                lease_expires_at = NULL, lease_token = NULL, updated_at = ?
                            WHERE id = ? AND status = 'leased' AND lease_expires_at <= ?
                            """,
                            (available_at, now, str(existing["id"]), now),
                        )
                        reconciled += 1
        return reconciled

    def _reconcile_terminal_job(
        self,
        reminder: Reminder,
        existing: Any,
        now: str,
        dedupe_key: str,
    ) -> None:
        desired_status = "succeeded" if reminder.delivery_state == "delivered" else "failed"
        if existing is None:
            job = self._repository.enqueue_outbox(
                job_type=REMINDER_DELIVERY_JOB_TYPE,
                payload=self._delivery_payload(reminder.id),
                available_at=now,
                correlation_id=reminder.audit_correlation_id,
                dedupe_key=dedupe_key,
                reminder_id=reminder.id,
            )
            self._database.connection.execute(
                "UPDATE outbox SET status = ?, updated_at = ? WHERE id = ?",
                (desired_status, now, job.id),
            )
            return
        if str(existing["status"]) != desired_status:
            self._database.connection.execute(
                """
                UPDATE outbox
                SET status = ?, lease_owner = NULL, lease_expires_at = NULL,
                    lease_token = NULL, updated_at = ?
                WHERE id = ?
                """,
                (desired_status, now, str(existing["id"])),
            )

    def _delivery_payload(self, reminder_id: str) -> Mapping[str, Any]:
        mapping = self._database.connection.execute(
            "SELECT legacy_chat_id FROM legacy_reminder_mappings WHERE planning_id = ?",
            (reminder_id,),
        ).fetchone()
        chat_id = self._default_chat_id
        if mapping is not None and mapping["legacy_chat_id"] is not None:
            chat_id = int(mapping["legacy_chat_id"])
        return {"reminder_id": reminder_id, "chat_id": chat_id}

    async def _process_job(self, job: Any, now: str) -> None:
        reminder_id = job.reminder_id
        if not reminder_id:
            self._repository.transition_outbox(
                job_id=job.id,
                lease_owner=self._worker_id,
                lease_token=job.lease_token,
                status="cancelled",
                now=now,
            )
            return
        reminder = self._repository.get_reminder(reminder_id)
        if not self._is_due_and_active(reminder, now):
            self._suppress_job(job, now, reminder)
            return

        attempt_window = job.attempt_window_started_at
        if job.attempt_count > MAX_DELIVERY_ATTEMPTS or (
            attempt_window is not None
            and _as_datetime(now) >= _as_datetime(str(attempt_window)) + timedelta(seconds=DELIVERY_RETRY_WINDOW_SECONDS)
        ):
            self._commit_terminal(job, reminder, now, "retry_window_exhausted", "delivery retry window exhausted")
            return

        settings = ReminderSettings()
        if self._settings_provider is not None:
            settings = await self._settings_provider()

        if settings.notify_telegram_enabled:
            telegram_result = await self._attempt_channel(
                job,
                reminder,
                self._telegram_transport,
                now,
            )
        else:
            telegram_result = await self._attempt_channel(
                job,
                reminder,
                None,
                now,
                forced_result=DeliveryResult.permanent(
                    "telegram_disabled",
                    diagnostic="Telegram required delivery is disabled",
                ),
            )

        optional_result: DeliveryResult | None = None
        if settings.notify_iphone_enabled and self._mobile_transport is not None:
            current = self._repository.get_reminder(reminder.id)
            if self._is_due_and_active(current, now):
                optional_result = await self._attempt_channel(
                    job,
                    current,
                    self._mobile_transport,
                    now,
                )

        current = self._repository.get_reminder(reminder.id)
        if not self._is_due_and_active(current, now):
            self._suppress_job(job, now, current)
            return
        if telegram_result.kind == "success":
            self._commit_success(job, current, now, optional_result)
            return
        if telegram_result.kind == "retryable":
            retry_at = self._next_retry_at(job, now, telegram_result)
            if retry_at is not None:
                self._commit_retry(job, current, now, retry_at, telegram_result)
                return
        self._commit_terminal(job, current, now, telegram_result.code, telegram_result.diagnostic)

    async def _attempt_channel(
        self,
        job: Any,
        reminder: Reminder,
        transport: ReminderChannelTransport | None,
        now: str,
        *,
        forced_result: DeliveryResult | None = None,
    ) -> DeliveryResult:
        channel = "telegram" if transport is None else transport.channel
        attempt = self._repository.start_delivery_attempt(
            reminder_id=reminder.id,
            channel=channel,
            started_at=now,
        )
        if self._before_provider_send is not None:
            await self._call_hook(self._before_provider_send, reminder, channel)

        current = self._repository.get_reminder(reminder.id)
        if not self._is_due_and_active(current, now):
            self._repository.finish_delivery_attempt(
                attempt_id=attempt.id,
                status="failed",
                finished_at=now,
                error_code="delivery_suppressed",
                error_message="delivery suppressed before provider send",
            )
            self._suppress_job(job, now, current)
            return DeliveryResult.permanent("delivery_suppressed", diagnostic="delivery suppressed")

        if forced_result is not None:
            result = forced_result
        else:
            try:
                result = await transport.send(
                    reminder=current,
                    chat_id=self._chat_id(job),
                    correlation_id=attempt.correlation_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                result = DeliveryResult.retryable("transport_exception", diagnostic="channel transport failure")

        if self._after_provider_send is not None and forced_result is None:
            await self._call_hook(self._after_provider_send, current, channel)

        self._repository.finish_delivery_attempt(
            attempt_id=attempt.id,
            status="succeeded" if result.kind == "success" else "failed",
            finished_at=now,
            error_code=None if result.kind == "success" else result.code,
            error_message=None if result.kind == "success" else result.diagnostic,
            provider_receipt=result.provider_receipt,
        )
        return result

    @staticmethod
    async def _call_hook(hook: Hook, reminder: Reminder, channel: str) -> None:
        result = hook(reminder, channel)
        if inspect.isawaitable(result):
            await result

    def _chat_id(self, job: Any) -> int:
        value = job.payload.get("chat_id", self._default_chat_id)
        if isinstance(value, bool) or not isinstance(value, int):
            return self._default_chat_id
        return value

    @staticmethod
    def _is_due_and_active(reminder: Reminder, now: str) -> bool:
        return (
            reminder.status in {"pending", "due"}
            and _as_datetime(reminder.due_at_utc) <= _as_datetime(now)
            and reminder.delivery_state not in {"delivered", "failed"}
        )

    def _suppress_job(self, job: Any, now: str, reminder: Reminder) -> None:
        status = "succeeded" if reminder.delivery_state == "delivered" else "failed" if reminder.delivery_state == "failed" else "cancelled"
        if status == "cancelled":
            self._repository.transition_outbox(
                job_id=job.id,
                lease_owner=self._worker_id,
                lease_token=job.lease_token,
                status="cancelled",
                now=now,
            )
        else:
            self._repository.transition_outbox(
                job_id=job.id,
                lease_owner=self._worker_id,
                lease_token=job.lease_token,
                status=status,
                now=now,
            )

    def _next_retry_at(self, job: Any, now: str, result: DeliveryResult) -> str | None:
        if job.attempt_count >= MAX_DELIVERY_ATTEMPTS:
            return None
        index = job.attempt_count - 1
        if index < 0 or index >= len(RETRY_DELAYS_SECONDS):
            return None
        base_delay = RETRY_DELAYS_SECONDS[index]
        requested_delay = max(base_delay, result.retry_after_seconds or 0)
        jitter = max(-self._jitter_bound_seconds, min(self._jitter_bound_seconds, float(self._jitter_fn(base_delay))))
        delay_seconds = max(0.0, requested_delay + jitter)
        retry_at = _as_datetime(now) + timedelta(seconds=delay_seconds)
        window_start = _as_datetime(str(job.attempt_window_started_at or now))
        if retry_at >= window_start + timedelta(seconds=DELIVERY_RETRY_WINDOW_SECONDS):
            return None
        return _as_timestamp(retry_at)

    def _commit_success(
        self,
        job: Any,
        reminder: Reminder,
        now: str,
        optional_result: DeliveryResult | None,
    ) -> None:
        try:
            self._repository.commit_reminder_delivery(
                reminder_id=reminder.id,
                expected_version=reminder.version,
                job_id=job.id,
                lease_owner=self._worker_id,
                lease_token=job.lease_token,
                delivery_state="delivered",
                outbox_status="succeeded",
                now=now,
                context=SCHEDULER_CONTEXT,
                audit_action="required_channel_delivered",
            )
        except PlanningLeaseLostError:
            LOGGER.warning("Reminder delivery lease lost after provider result: reminder_id=%s", reminder.id)
        except PlanningVersionConflictError:
            LOGGER.warning("Reminder changed while committing delivery: reminder_id=%s", reminder.id)
        if optional_result is not None and optional_result.kind != "success":
            LOGGER.info(
                "Optional reminder channel did not deliver after Telegram success: reminder_id=%s code=%s",
                reminder.id,
                optional_result.code,
            )

    def _commit_retry(
        self,
        job: Any,
        reminder: Reminder,
        now: str,
        retry_at: str,
        result: DeliveryResult,
    ) -> None:
        try:
            self._repository.commit_reminder_delivery(
                reminder_id=reminder.id,
                expected_version=reminder.version,
                job_id=job.id,
                lease_owner=self._worker_id,
                lease_token=job.lease_token,
                delivery_state="retrying",
                outbox_status="queued",
                now=now,
                available_at=retry_at,
                next_attempt_at=retry_at,
                context=SCHEDULER_CONTEXT,
                audit_action="retry_scheduled",
                error_code=result.code,
                error_message=result.diagnostic,
            )
        except (PlanningLeaseLostError, PlanningVersionConflictError):
            LOGGER.warning("Reminder retry state was not committed: reminder_id=%s", reminder.id)

    def _commit_terminal(
        self,
        job: Any,
        reminder: Reminder,
        now: str,
        code: str,
        diagnostic: str | None,
    ) -> None:
        try:
            self._repository.commit_reminder_delivery(
                reminder_id=reminder.id,
                expected_version=reminder.version,
                job_id=job.id,
                lease_owner=self._worker_id,
                lease_token=job.lease_token,
                delivery_state="failed",
                outbox_status="failed",
                now=now,
                final_failure_at=now,
                context=SCHEDULER_CONTEXT,
                audit_action="terminal_failure",
                error_code=code,
                error_message=diagnostic,
            )
        except (PlanningLeaseLostError, PlanningVersionConflictError):
            LOGGER.warning("Reminder terminal failure was not committed: reminder_id=%s", reminder.id)


def validate_scheduler_modes(
    *,
    durable_scheduler_enabled: bool,
    legacy_scheduler_enabled: bool,
    reminder_cutover_enabled: bool,
) -> None:
    """Fail closed if lifecycle composition would attach two or zero workers."""

    if durable_scheduler_enabled and legacy_scheduler_enabled:
        raise RuntimeError("durable and legacy reminder schedulers cannot both be enabled")
    if not durable_scheduler_enabled and not legacy_scheduler_enabled:
        raise RuntimeError("exactly one reminder scheduler lifecycle must be enabled")
    if durable_scheduler_enabled and not reminder_cutover_enabled:
        raise RuntimeError("durable reminder scheduler requires Planning reminder cutover")
