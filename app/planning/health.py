"""Content-free operational health for the Planning foundation."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from app.planning.capabilities import planning_capability_metadata
from app.planning.operations import PlanningOperationsState, PlanningOperationsStateStore
from app.planning.models import utc_now


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlanningIncident:
    code: str
    active: bool
    aggregate_count: int
    age_seconds: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "active": self.active,
            "aggregateCount": self.aggregate_count,
            "ageSeconds": self.age_seconds,
        }


class PlanningHealthService:
    """Observe Planning storage, the existing scheduler, and backup state.

    No provider, transport, HTTP request object, or presentation adapter is
    required.  Scheduler heartbeat is intentionally process-local and starts
    as ``unknown`` after restart until the real durable loop begins an
    iteration.
    """

    def __init__(
        self,
        database: Any | None,
        *,
        scheduler: Any | None,
        scheduler_enabled: bool,
        scheduler_heartbeat_stale_after_seconds: float,
        backup_dir: str,
        backup_enabled: bool,
        backup_service_ready: bool,
        backup_interval_seconds: int,
        application_version: str = "unknown",
        application_commit: str = "unknown",
        now_fn: Callable[[], str] = utc_now,
        logger: logging.Logger | None = None,
        state_store: PlanningOperationsStateStore | None = None,
        provider_cache: Any | None = None,
    ) -> None:
        if scheduler_heartbeat_stale_after_seconds <= 0:
            raise ValueError("scheduler heartbeat stale threshold must be positive")
        if backup_interval_seconds <= 0:
            raise ValueError("backup interval must be positive")
        self.database = database
        self.scheduler = scheduler
        self.scheduler_enabled = scheduler_enabled
        self.scheduler_heartbeat_stale_after_seconds = scheduler_heartbeat_stale_after_seconds
        self.backup_dir = Path(backup_dir)
        self.backup_enabled = backup_enabled
        self.backup_service_ready = backup_service_ready
        self.backup_interval_seconds = backup_interval_seconds
        self.application_version = _metadata(application_version)
        self.application_commit = _metadata(application_commit)
        self.now_fn = now_fn
        self.logger = logger or LOGGER
        self.state_store = state_store or PlanningOperationsStateStore(self.backup_dir)
        self.provider_cache = provider_cache
        self._previous_incidents: dict[str, tuple[bool, int]] = {}

    def snapshot(self, *, correlation_id: str | None = None) -> dict[str, Any]:
        observed_at = self.now_fn()
        now_dt = _parse_timestamp(observed_at)
        database_facts = self._database_facts(observed_at, now_dt)
        scheduler_facts, scheduler_incident = self._scheduler_facts(observed_at, now_dt)
        state = self.state_store.load()
        backup_facts, backup_incidents = self._backup_facts(state, now_dt)
        incidents = [scheduler_incident, *backup_incidents]
        incidents.extend(self._work_incidents(database_facts, now_dt))
        self._log_incident_transitions(incidents, correlation_id)
        active_incidents = [incident.to_dict() for incident in incidents if incident.active]
        provider_facts = (
            self.provider_cache.health_snapshot()
            if self.provider_cache is not None
            else {"providerStatus": "not_configured", "providerLastSyncAt": None, "providerErrorCode": None}
        )
        return {
            "schemaVersion": "planning.operations.v1",
            "observedAt": observed_at,
            **database_facts,
            **scheduler_facts,
            **backup_facts,
            **provider_facts,
            "capabilityMetadata": planning_capability_metadata().to_dict(),
            "applicationVersion": self.application_version,
            "applicationCommit": self.application_commit,
            "incidents": active_incidents,
        }

    def _database_facts(self, observed_at: str, now_dt: datetime | None) -> dict[str, Any]:
        del observed_at
        facts: dict[str, Any] = {
            "planningSchemaVersion": None,
            "dbAvailable": False,
            "dbIntegrityStatus": "unknown",
            "queuedOutboxCount": 0,
            "leasedOutboxCount": 0,
            "retryingReminderCount": 0,
            "terminalFailedReminderCount": 0,
            "activeDueReminderCount": 0,
            "oldestQueuedOrLeasedOutboxAgeSeconds": None,
            "eligibleQueuedOrLeasedOutboxCount": 0,
        }
        if self.database is None:
            return facts
        try:
            connection = self.database.connection
            connection.execute("SELECT 1").fetchone()
            facts["dbAvailable"] = True
            facts["planningSchemaVersion"] = int(self.database.schema_version())
            integrity = str(self.database.integrity_check())
            foreign_key_errors = int(connection.execute("SELECT COUNT(*) FROM pragma_foreign_key_check").fetchone()[0])
            facts["dbIntegrityStatus"] = "ok" if integrity == "ok" and foreign_key_errors == 0 else "failed"
            queued = int(connection.execute("SELECT COUNT(*) FROM outbox WHERE status = 'queued'").fetchone()[0])
            leased = int(connection.execute("SELECT COUNT(*) FROM outbox WHERE status = 'leased'").fetchone()[0])
            facts["queuedOutboxCount"] = queued
            facts["leasedOutboxCount"] = leased
            facts["retryingReminderCount"] = int(
                connection.execute(
                    "SELECT COUNT(*) FROM reminders WHERE deleted_at IS NULL AND delivery_state = 'retrying'"
                ).fetchone()[0]
            )
            facts["terminalFailedReminderCount"] = int(
                connection.execute(
                    "SELECT COUNT(*) FROM reminders WHERE deleted_at IS NULL AND delivery_state = 'failed'"
                ).fetchone()[0]
            )
            facts["activeDueReminderCount"] = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM reminders
                    WHERE deleted_at IS NULL
                      AND status IN ('pending', 'due')
                      AND due_at_utc <= ?
                    """,
                    (self.now_fn(),),
                ).fetchone()[0]
            )
            oldest = connection.execute(
                """
                SELECT MIN(o.available_at)
                FROM outbox AS o
                JOIN reminders AS r ON r.id = o.reminder_id
                WHERE o.status IN ('queued', 'leased')
                  AND o.available_at <= ?
                  AND r.deleted_at IS NULL
                  AND r.status IN ('pending', 'due')
                  AND r.delivery_state IN ('not_due', 'queued', 'retrying')
                """,
                (self.now_fn(),),
            ).fetchone()[0]
            facts["oldestQueuedOrLeasedOutboxAgeSeconds"] = _age_seconds(oldest, now_dt)
            facts["eligibleQueuedOrLeasedOutboxCount"] = int(
                connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM outbox AS o
                    JOIN reminders AS r ON r.id = o.reminder_id
                    WHERE o.status IN ('queued', 'leased')
                      AND o.available_at <= ?
                      AND r.deleted_at IS NULL
                      AND r.status IN ('pending', 'due')
                      AND r.delivery_state IN ('not_due', 'queued', 'retrying')
                    """,
                    (self.now_fn(),),
                ).fetchone()[0]
            )
        except Exception:
            facts["dbAvailable"] = False
            facts["dbIntegrityStatus"] = "unknown"
        return facts

    def _scheduler_facts(
        self,
        observed_at: str,
        now_dt: datetime | None,
    ) -> tuple[dict[str, Any], PlanningIncident]:
        if not self.scheduler_enabled:
            return (
                {
                    "durableSchedulerEnabled": False,
                    "schedulerHeartbeatAt": None,
                    "schedulerHeartbeatAgeSeconds": None,
                    "schedulerHealth": "disabled",
                },
                PlanningIncident("planning.scheduler_heartbeat_stale", False, 0, None),
            )
        heartbeat = getattr(self.scheduler, "heartbeat", None) if self.scheduler is not None else None
        heartbeat_at = getattr(heartbeat, "heartbeat_at", None)
        age = _age_seconds(heartbeat_at, now_dt)
        if heartbeat_at is None:
            health = "unknown"
        elif age is None or age > self.scheduler_heartbeat_stale_after_seconds:
            health = "degraded"
        elif getattr(heartbeat, "last_iteration_succeeded", None) is False:
            health = "degraded"
        else:
            health = "healthy"
        incident_active = heartbeat_at is not None and (
            age is None or age > self.scheduler_heartbeat_stale_after_seconds
        )
        incident = PlanningIncident(
            "planning.scheduler_heartbeat_stale",
            incident_active,
            1 if incident_active else 0,
            age if incident_active else None,
        )
        return (
            {
                "durableSchedulerEnabled": True,
                "schedulerHeartbeatAt": heartbeat_at,
                "schedulerHeartbeatAgeSeconds": age,
                "schedulerHealth": health,
            },
            incident,
        )

    def _backup_facts(
        self,
        state: PlanningOperationsState,
        now_dt: datetime | None,
    ) -> tuple[dict[str, Any], list[PlanningIncident]]:
        if not self.backup_enabled:
            return (
                {
                    "backupStatus": "disabled",
                    "lastSuccessfulBackupAt": None,
                    "lastSuccessfulRestoreVerificationAt": None,
                    "lastBackupAgeSeconds": None,
                    "lastRestoreVerificationStatus": "unknown",
                },
                [],
            )
        last_backup_age = _age_seconds(state.last_successful_backup_at, now_dt)
        if not self.backup_service_ready:
            backup_status = "unavailable"
        elif state.last_backup_status == "failed":
            backup_status = "failed"
        elif last_backup_age is None:
            backup_status = "unknown"
        elif last_backup_age > self.backup_interval_seconds:
            backup_status = "overdue"
        else:
            backup_status = "fresh"
        incidents: list[PlanningIncident] = []
        if backup_status == "failed" or backup_status == "unavailable":
            incidents.append(PlanningIncident("planning.backup_failed", True, 1, last_backup_age))
        elif backup_status == "overdue":
            incidents.append(PlanningIncident("planning.backup_overdue", True, 1, last_backup_age))
        if state.last_restore_verification_status == "failed":
            incidents.append(
                PlanningIncident("planning.restore_verification_failed", True, 1, None)
            )
        return (
            {
                "backupStatus": backup_status,
                "lastSuccessfulBackupAt": state.last_successful_backup_at,
                "lastSuccessfulRestoreVerificationAt": state.last_successful_restore_verification_at,
                "lastBackupAgeSeconds": last_backup_age,
                "lastRestoreVerificationStatus": state.last_restore_verification_status,
            },
            incidents,
        )

    @staticmethod
    def _work_incidents(facts: Mapping[str, Any], now_dt: datetime | None) -> list[PlanningIncident]:
        stuck_count = int(facts.get("eligibleQueuedOrLeasedOutboxCount", 0))
        stuck_age = facts.get("oldestQueuedOrLeasedOutboxAgeSeconds")
        # A queued/leased row only becomes an incident after it is eligible and
        # has exceeded one polling interval.  Future jobs are not incidents.
        stuck_active = stuck_age is not None and int(stuck_age) >= 30
        failed_count = int(facts.get("terminalFailedReminderCount", 0))
        integrity_failed = facts.get("dbIntegrityStatus") == "failed"
        return [
            PlanningIncident("planning.outbox_stuck", stuck_active, stuck_count if stuck_active else 0, stuck_age if stuck_active else None),
            PlanningIncident("planning.delivery_terminal_failure", failed_count > 0, failed_count, None),
            PlanningIncident("planning.database_integrity_failure", integrity_failed, 1 if integrity_failed else 0, None),
        ]

    def _log_incident_transitions(
        self,
        incidents: list[PlanningIncident],
        correlation_id: str | None,
    ) -> None:
        for incident in incidents:
            current = (incident.active, incident.aggregate_count)
            previous = self._previous_incidents.get(incident.code)
            if previous is None:
                self._previous_incidents[incident.code] = current
                if not incident.active:
                    continue
                previous = None
            if previous == current:
                continue
            self._previous_incidents[incident.code] = current
            self.logger.warning(
                "planning_incident_transition code=%s active=%s aggregate_count=%s age_seconds=%s correlation_id=%s",
                incident.code,
                incident.active,
                incident.aggregate_count,
                incident.age_seconds,
                correlation_id or "health",
            )


def _parse_timestamp(value: str | None) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _age_seconds(value: str | None, now: datetime | None) -> int | None:
    parsed = _parse_timestamp(value)
    if parsed is None or now is None:
        return None
    return max(0, int((now - parsed).total_seconds()))


def _metadata(value: str | None) -> str:
    return value.strip()[:256] if isinstance(value, str) and value.strip() else "unknown"
