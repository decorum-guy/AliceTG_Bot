from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from app.planning.db import PlanningDatabase
from app.planning.models import (
    new_uuid4,
    utc_now,
    validate_event_shape,
    validate_text,
)
from app.planning.providers.contracts import (
    CalendarWindow,
    ExternalCalendar,
    ExternalCalendarEvent,
    ExternalCalendarProvider,
    ExternalResourceVerification,
    ProviderAdapterError,
    ProviderStatus,
)


@dataclass(frozen=True)
class ProviderRefreshResult:
    source_id: str
    status: ProviderStatus
    calendars_seen: int
    events_seen: int
    tombstones_created: int
    observed_at: str
    last_successful_sync_at: str | None
    error_code: str | None = None
    missing_candidates_seen: int = 0
    deletions_deferred: int = 0
    deletions_confirmed: int = 0


@dataclass(frozen=True)
class _ResourceReconciliationResult:
    tombstones_created: int
    missing_candidates_seen: int
    deletions_deferred: int
    deletions_confirmed: int


class ProviderCalendarCache:
    """Trusted provider ingestion/cache path, separate from native EventService."""

    def __init__(
        self,
        database: PlanningDatabase,
        *,
        provider: ExternalCalendarProvider | None,
        provider_name: str,
        account_id: str | None,
        display_label: str,
        enabled: bool,
        configured: bool,
        now_fn: Callable[[], str] = utc_now,
        max_calendars: int = 32,
    ) -> None:
        validate_text(provider_name, "provider.name", max_length=64)
        validate_text(display_label, "provider.display_label", max_length=100)
        if account_id is not None:
            validate_text(account_id, "provider.account_id", max_length=128)
        if not 1 <= max_calendars <= 100:
            raise ValueError("provider calendar bound is invalid")
        self.database = database
        self.provider = provider
        self.provider_name = provider_name
        self.account_id = account_id
        self.display_label = display_label
        self.enabled = enabled
        self.configured = configured
        self.now_fn = now_fn
        self.max_calendars = max_calendars
        self.source_id = _source_id(provider_name, account_id)
        self._ensure_source()

    def source_metadata(self) -> list[dict[str, Any]]:
        """Return safe per-source freshness metadata for additive planning.v1 envelopes."""

        now = self.now_fn()
        native = {
            "sourceType": "native_planning",
            "accountId": "local",
            "provider": "local",
            "status": "current",
            "lastSyncedAt": now,
            "observedAt": now,
            "errorCode": None,
            "calendars": [],
        }
        row = self.database.connection.execute(
            "SELECT * FROM provider_sources WHERE source_id = ?",
            (self.source_id,),
        ).fetchone()
        if row is None:
            return [native]
        calendars = self.database.connection.execute(
            """
            SELECT provider_calendar_id, display_name, color, enabled, status,
                   last_successful_sync_at, observed_at, last_error_code
            FROM provider_calendars
            WHERE source_id = ?
            ORDER BY provider_calendar_id
            """,
            (self.source_id,),
        ).fetchall()
        return [
            native,
            {
                "sourceType": "external_calendar",
                "accountId": str(row["account_id"]),
                "provider": str(row["provider"]),
                "status": str(row["status"]),
                "lastSyncedAt": row["last_successful_sync_at"],
                "observedAt": str(row["observed_at"]),
                "errorCode": row["last_error_code"],
                "calendars": [
                    {
                        "calendarId": str(calendar["provider_calendar_id"]),
                        "displayName": str(calendar["display_name"]),
                        "color": calendar["color"],
                        "enabled": bool(calendar["enabled"]),
                        "status": str(calendar["status"]),
                        "lastSyncedAt": calendar["last_successful_sync_at"],
                        "observedAt": str(calendar["observed_at"]),
                        "errorCode": calendar["last_error_code"],
                    }
                    for calendar in calendars
                ],
            },
        ]

    def health_snapshot(self) -> dict[str, Any]:
        row = self.database.connection.execute(
            "SELECT status, last_successful_sync_at, last_error_code FROM provider_sources WHERE source_id = ?",
            (self.source_id,),
        ).fetchone()
        if row is None:
            return {"providerStatus": "not_configured", "providerLastSyncAt": None, "providerErrorCode": None}
        return {
            "providerStatus": str(row["status"]),
            "providerLastSyncAt": row["last_successful_sync_at"],
            "providerErrorCode": row["last_error_code"],
        }

    async def refresh(self, window: CalendarWindow) -> ProviderRefreshResult:
        window.validate()
        observed_at = self.now_fn()
        if not self.enabled:
            self._set_source_state(status="disabled", observed_at=observed_at, error_code=None)
            return ProviderRefreshResult(
                self.source_id, "disabled", 0, 0, 0, observed_at, self._last_successful_sync(), None
            )
        if not self.configured or self.provider is None:
            self._set_source_state(status="not_configured", observed_at=observed_at, error_code=None)
            return ProviderRefreshResult(
                self.source_id, "not_configured", 0, 0, 0, observed_at, self._last_successful_sync(), None
            )

        try:
            account = await self.provider.discover_account()
            if account.provider != self.provider_name:
                raise ProviderAdapterError("provider_identity_mismatch")
            calendars = await self.provider.list_calendars()
            if len(calendars) > self.max_calendars:
                raise ProviderAdapterError("provider_calendar_limit")
            fetched: list[
                tuple[ExternalCalendar, list[ExternalCalendarEvent], list[ExternalResourceVerification]]
            ] = []
            verifier = getattr(self.provider, "verify_resources", None)
            for calendar in calendars:
                if not calendar.enabled:
                    fetched.append((calendar, [], []))
                    continue
                events = await self.provider.fetch_events(calendar, window)
                verifications: list[ExternalResourceVerification] = []
                if callable(verifier):
                    refs = self._resource_refs_for_calendar(calendar.provider_calendar_id)
                    if refs:
                        verifications = await verifier(calendar, refs, window)
                fetched.append((calendar, events, verifications))
            return self._commit_success(
                account_id=account.account_id,
                calendars=fetched,
                window=window,
                observed_at=observed_at,
            )
        except ProviderAdapterError as exc:
            return self._record_failure(exc.code, observed_at)
        except (OSError, ValueError, TypeError, sqlite3.Error) as exc:
            # The error code is stable and intentionally excludes exception text.
            return self._record_failure("provider_refresh_failed", observed_at, cause=exc)
        except Exception as exc:
            return self._record_failure("provider_refresh_failed", observed_at, cause=exc)

    def _commit_success(
        self,
        *,
        account_id: str,
        calendars: list[
            tuple[ExternalCalendar, list[ExternalCalendarEvent], list[ExternalResourceVerification]]
        ],
        window: CalendarWindow,
        observed_at: str,
    ) -> ProviderRefreshResult:
        validate_text(account_id, "provider.account_id", max_length=128)
        refresh_token = new_uuid4()
        tombstones = 0
        event_count = 0
        missing_candidates_seen = 0
        deletions_deferred = 0
        deletions_confirmed = 0
        with self.database.transaction() as connection:
            self._upsert_source(
                connection,
                account_id=account_id,
                status="current",
                observed_at=observed_at,
                last_successful_sync_at=observed_at,
                error_code=None,
            )
            seen_calendar_ids: set[str] = set()
            for calendar, events, verifications in calendars:
                validate_text(calendar.provider_calendar_id, "provider.calendar_id", max_length=256)
                seen_calendar_ids.add(calendar.provider_calendar_id)
                self._upsert_calendar(connection, calendar, observed_at)
                fetched_event_ids = {event.provider_event_id for event in events}
                fetched_resource_refs = {
                    event.resource_ref for event in events if event.resource_ref is not None
                }
                for event in events:
                    self._upsert_event(
                        connection,
                        event=event,
                        window=window,
                        refresh_token=refresh_token,
                        observed_at=observed_at,
                    )
                    event_count += 1
                for verification in verifications:
                    fetched_event_ids.update(event.provider_event_id for event in verification.events)
                    fetched_resource_refs.update(
                        event.resource_ref
                        for event in verification.events
                        if event.resource_ref is not None
                    )
                    for event in verification.events:
                        self._upsert_event(
                            connection,
                            event=event,
                            window=window,
                            refresh_token=refresh_token,
                            observed_at=observed_at,
                        )
                        event_count += 1
                reconciliation = self._reconcile_resources(
                    connection,
                    provider_calendar_id=calendar.provider_calendar_id,
                    verifications=verifications,
                    refresh_token=refresh_token,
                    deleted_at=observed_at,
                    fetched_event_ids=fetched_event_ids,
                    fetched_resource_refs=fetched_resource_refs,
                )
                tombstones += reconciliation.tombstones_created
                missing_candidates_seen += reconciliation.missing_candidates_seen
                deletions_deferred += reconciliation.deletions_deferred
                deletions_confirmed += reconciliation.deletions_confirmed
            previous_calendar_ids = [
                str(row["provider_calendar_id"])
                for row in connection.execute(
                    "SELECT provider_calendar_id FROM provider_calendars WHERE source_id = ?",
                    (self.source_id,),
                ).fetchall()
            ]
            disappeared_calendar_ids = sorted(set(previous_calendar_ids) - seen_calendar_ids)
            for provider_calendar_id in disappeared_calendar_ids:
                connection.execute(
                    """
                    UPDATE provider_calendars
                    SET enabled = 0, status = 'disabled', last_error_code = ?,
                        observed_at = ?, updated_at = ?
                    WHERE source_id = ? AND provider_calendar_id = ?
                    """,
                    (
                        "provider_calendar_disappeared",
                        observed_at,
                        observed_at,
                        self.source_id,
                        provider_calendar_id,
                    ),
                )
                connection.execute(
                    """
                    UPDATE calendar_events
                    SET sync_state = 'stale', updated_at = ?
                    WHERE provider_calendar_id = ? AND deleted_at IS NULL
                      AND provider_id IS NOT NULL AND source = 'calendar-provider'
                    """,
                    (observed_at, provider_calendar_id),
                )
                connection.execute(
                    """
                    UPDATE provider_event_cache
                    SET missing_successes = 0, updated_at = ?
                    WHERE source_id = ? AND provider_calendar_id = ?
                    """,
                    (observed_at, self.source_id, provider_calendar_id),
                )
        return ProviderRefreshResult(
            self.source_id,
            "current",
            len(calendars),
            event_count,
            tombstones,
            observed_at,
            observed_at,
            None,
            missing_candidates_seen,
            deletions_deferred,
            deletions_confirmed,
        )

    def _upsert_source(
        self,
        connection: sqlite3.Connection,
        *,
        account_id: str,
        status: ProviderStatus,
        observed_at: str,
        last_successful_sync_at: str | None,
        error_code: str | None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO provider_sources(
                source_id, provider, account_id, display_label, enabled, configured, status,
                last_successful_sync_at, observed_at, last_error_code, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id) DO UPDATE SET
                account_id = excluded.account_id,
                display_label = excluded.display_label,
                enabled = excluded.enabled,
                configured = excluded.configured,
                status = excluded.status,
                last_successful_sync_at = excluded.last_successful_sync_at,
                observed_at = excluded.observed_at,
                last_error_code = excluded.last_error_code,
                updated_at = excluded.updated_at
            """,
            (
                self.source_id,
                self.provider_name,
                account_id,
                self.display_label,
                int(self.enabled),
                int(self.configured),
                status,
                last_successful_sync_at,
                observed_at,
                error_code,
                observed_at,
            ),
        )

    def _upsert_calendar(self, connection: sqlite3.Connection, calendar: ExternalCalendar, observed_at: str) -> None:
        connection.execute(
            """
            INSERT INTO provider_calendars(
                provider_calendar_id, source_id, display_name, color, enabled, status,
                last_successful_sync_at, observed_at, last_error_code, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'current', ?, ?, NULL, ?)
            ON CONFLICT(provider_calendar_id) DO UPDATE SET
                source_id = excluded.source_id,
                display_name = excluded.display_name,
                color = excluded.color,
                enabled = excluded.enabled,
                status = 'current',
                last_successful_sync_at = excluded.last_successful_sync_at,
                observed_at = excluded.observed_at,
                last_error_code = NULL,
                updated_at = excluded.updated_at
            """,
            (
                calendar.provider_calendar_id,
                self.source_id,
                calendar.display_name,
                calendar.color,
                int(calendar.enabled),
                observed_at,
                observed_at,
                observed_at,
            ),
        )

    def _upsert_event(
        self,
        connection: sqlite3.Connection,
        *,
        event: ExternalCalendarEvent,
        window: CalendarWindow,
        refresh_token: str,
        observed_at: str,
    ) -> None:
        if event.resource_ref is not None:
            validate_text(event.resource_ref, "provider.resource_ref", max_length=512)
        validate_event_shape(
            all_day=event.all_day,
            timezone_name=event.timezone,
            start_at_utc=event.start_at_utc,
            end_at_utc=event.end_at_utc,
            start_date=event.start_date,
            end_date_exclusive=event.end_date_exclusive,
            sync_state="synced",
            title=event.title,
            notes=event.notes,
            location=event.location,
            recurrence_rule=None,
            provider_id=event.provider_event_id,
            provider_calendar_id=event.provider_calendar_id,
        )
        identity_key = event.provider_event_id
        cached = connection.execute(
            """
            SELECT canonical_event_id FROM provider_event_cache
            WHERE source_id = ? AND identity_key = ?
            """,
            (self.source_id, identity_key),
        ).fetchone()
        canonical_id = str(cached["canonical_event_id"]) if cached else new_uuid4()
        existing = connection.execute(
            "SELECT * FROM calendar_events WHERE id = ?",
            (canonical_id,),
        ).fetchone()
        if existing is None:
            connection.execute(
                """
                INSERT INTO calendar_events(
                    id, title, notes, location, all_day, start_at_utc, end_at_utc,
                    start_date, end_date_exclusive, timezone, recurrence_rule,
                    provider_id, provider_calendar_id, sync_state, source, source_ref,
                    version, created_at, updated_at, audit_correlation_id, deleted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, 'synced',
                          'calendar-provider', ?, 1, ?, ?, ?, NULL)
                """,
                (
                    canonical_id,
                    event.title,
                    event.notes,
                    event.location,
                    int(event.all_day),
                    event.start_at_utc,
                    event.end_at_utc,
                    event.start_date,
                    event.end_date_exclusive,
                    event.timezone,
                    event.provider_event_id,
                    event.provider_calendar_id,
                    f"icloud:event:{event.provider_event_id}",
                    observed_at,
                    observed_at,
                    new_uuid4(),
                ),
            )
        else:
            changed = any(
                existing[field] != value
                for field, value in {
                    "title": event.title,
                    "notes": event.notes,
                    "location": event.location,
                    "all_day": int(event.all_day),
                    "start_at_utc": event.start_at_utc,
                    "end_at_utc": event.end_at_utc,
                    "start_date": event.start_date,
                    "end_date_exclusive": event.end_date_exclusive,
                    "timezone": event.timezone,
                    "provider_id": event.provider_event_id,
                    "provider_calendar_id": event.provider_calendar_id,
                    "sync_state": "synced",
                    "source": "calendar-provider",
                    "deleted_at": None,
                }.items()
            )
            if changed:
                connection.execute(
                    """
                    UPDATE calendar_events SET
                        title = ?, notes = ?, location = ?, all_day = ?, start_at_utc = ?,
                        end_at_utc = ?, start_date = ?, end_date_exclusive = ?, timezone = ?,
                        provider_id = ?, provider_calendar_id = ?, sync_state = 'synced',
                        source = 'calendar-provider', source_ref = ?, version = version + 1,
                        updated_at = ?, deleted_at = NULL
                    WHERE id = ? AND provider_id IS NOT NULL AND provider_calendar_id IS NOT NULL
                    """,
                    (
                        event.title,
                        event.notes,
                        event.location,
                        int(event.all_day),
                        event.start_at_utc,
                        event.end_at_utc,
                        event.start_date,
                        event.end_date_exclusive,
                        event.timezone,
                        event.provider_event_id,
                        event.provider_calendar_id,
                        f"icloud:event:{event.provider_event_id}",
                        observed_at,
                        canonical_id,
                    ),
                )
        connection.execute(
            """
            INSERT INTO provider_event_cache(
                canonical_event_id, source_id, provider_calendar_id, provider_event_id,
                identity_key, recurrence_instance_key, resource_ref, window_start_utc,
                window_end_utc, last_seen_refresh, last_seen_at, created_at, updated_at,
                missing_successes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            ON CONFLICT(source_id, identity_key) DO UPDATE SET
                canonical_event_id = excluded.canonical_event_id,
                provider_calendar_id = excluded.provider_calendar_id,
                provider_event_id = excluded.provider_event_id,
                recurrence_instance_key = excluded.recurrence_instance_key,
                resource_ref = excluded.resource_ref,
                window_start_utc = excluded.window_start_utc,
                window_end_utc = excluded.window_end_utc,
                last_seen_refresh = excluded.last_seen_refresh,
                last_seen_at = excluded.last_seen_at,
                updated_at = excluded.updated_at,
                missing_successes = 0
            """,
            (
                canonical_id,
                self.source_id,
                event.provider_calendar_id,
                event.provider_event_id,
                identity_key,
                event.recurrence_instance_key,
                event.resource_ref,
                _timestamp(window.start),
                _timestamp(window.end),
                refresh_token,
                observed_at,
                observed_at,
                observed_at,
            ),
        )

    def _resource_refs_for_calendar(self, provider_calendar_id: str) -> list[str]:
        rows = self.database.connection.execute(
            """
            SELECT DISTINCT pec.resource_ref
            FROM provider_event_cache AS pec
            JOIN calendar_events AS ce ON ce.id = pec.canonical_event_id
            WHERE pec.source_id = ? AND pec.provider_calendar_id = ?
              AND pec.resource_ref IS NOT NULL AND ce.deleted_at IS NULL
            ORDER BY pec.resource_ref
            LIMIT 512
            """,
            (self.source_id, provider_calendar_id),
        ).fetchall()
        return [str(row["resource_ref"]) for row in rows if row["resource_ref"]]

    def _reconcile_resources(
        self,
        connection: sqlite3.Connection,
        *,
        provider_calendar_id: str,
        verifications: list[ExternalResourceVerification],
        refresh_token: str,
        deleted_at: str,
        fetched_event_ids: set[str],
        fetched_resource_refs: set[str],
    ) -> _ResourceReconciliationResult:
        missing_refs = [verification.resource_ref for verification in verifications if verification.status == "missing"]
        present_refs = [verification.resource_ref for verification in verifications if verification.status == "present"]
        tombstone_ids: set[str] = set()
        missing_candidates_seen = 0
        deletions_deferred = 0
        for resource_ref in missing_refs:
            rows = connection.execute(
                """
                SELECT pec.canonical_event_id, pec.provider_event_id, pec.missing_successes
                FROM provider_event_cache AS pec
                JOIN calendar_events AS ce ON ce.id = pec.canonical_event_id
                WHERE pec.source_id = ? AND pec.provider_calendar_id = ?
                  AND pec.resource_ref = ? AND ce.deleted_at IS NULL
                  AND ce.provider_id IS NOT NULL AND ce.provider_calendar_id IS NOT NULL
                """,
                (self.source_id, provider_calendar_id, resource_ref),
            ).fetchall()
            for row in rows:
                missing_candidates_seen += 1
                canonical_id = str(row["canonical_event_id"])
                # A successful calendar query in this same refresh wins over a
                # contradictory verifier response. Never delete what we just
                # fetched from the provider.
                if (
                    str(row["provider_event_id"]) in fetched_event_ids
                    or resource_ref in fetched_resource_refs
                ):
                    connection.execute(
                        """
                        UPDATE provider_event_cache
                        SET missing_successes = 0, updated_at = ?
                        WHERE canonical_event_id = ?
                        """,
                        (deleted_at, canonical_id),
                    )
                    continue
                if int(row["missing_successes"]) >= 1:
                    tombstone_ids.add(canonical_id)
                else:
                    deletions_deferred += 1
                    connection.execute(
                        """
                        UPDATE provider_event_cache
                        SET missing_successes = 1, updated_at = ?
                        WHERE canonical_event_id = ?
                        """,
                        (deleted_at, canonical_id),
                    )
        for canonical_id in tombstone_ids:
            connection.execute(
                """
                UPDATE calendar_events
                SET deleted_at = ?, updated_at = ?, version = version + 1
                WHERE id = ? AND provider_id IS NOT NULL AND provider_calendar_id IS NOT NULL
                """,
                (deleted_at, deleted_at, canonical_id),
            )
            connection.execute(
                """
                UPDATE provider_event_cache
                SET missing_successes = 0, updated_at = ?
                WHERE canonical_event_id = ?
                """,
                (deleted_at, canonical_id),
            )
        for resource_ref in present_refs:
            connection.execute(
                """
                UPDATE provider_event_cache
                SET missing_successes = 0, updated_at = ?
                WHERE source_id = ? AND provider_calendar_id = ? AND resource_ref = ?
                """,
                (deleted_at, self.source_id, provider_calendar_id, resource_ref),
            )
            connection.execute(
                """
                UPDATE calendar_events
                SET sync_state = 'stale', updated_at = ?
                WHERE id IN (
                    SELECT canonical_event_id FROM provider_event_cache
                    WHERE source_id = ? AND provider_calendar_id = ?
                      AND resource_ref = ? AND last_seen_refresh != ?
                ) AND deleted_at IS NULL AND provider_id IS NOT NULL
                """,
                (deleted_at, self.source_id, provider_calendar_id, resource_ref, refresh_token),
            )
        return _ResourceReconciliationResult(
            tombstones_created=len(tombstone_ids),
            missing_candidates_seen=missing_candidates_seen,
            deletions_deferred=deletions_deferred,
            deletions_confirmed=len(tombstone_ids),
        )

    def _record_failure(
        self,
        error_code: str,
        observed_at: str,
        *,
        cause: BaseException | None = None,
    ) -> ProviderRefreshResult:
        del cause
        has_cache = self.database.connection.execute(
            "SELECT 1 FROM provider_event_cache WHERE source_id = ? LIMIT 1",
            (self.source_id,),
        ).fetchone()
        status: ProviderStatus = "stale" if has_cache else "error"
        with self.database.transaction() as connection:
            self._set_source_state(
                status=status,
                observed_at=observed_at,
                error_code=error_code,
                connection=connection,
            )
            connection.execute(
                """
                UPDATE provider_event_cache
                SET missing_successes = 0, updated_at = ?
                WHERE source_id = ?
                """,
                (observed_at, self.source_id),
            )
            connection.execute(
                """
                UPDATE calendar_events
                SET sync_state = 'stale', updated_at = ?
                WHERE id IN (
                    SELECT canonical_event_id FROM provider_event_cache WHERE source_id = ?
                ) AND deleted_at IS NULL
                  AND provider_id IS NOT NULL AND provider_calendar_id IS NOT NULL
                """,
                (observed_at, self.source_id),
            )
            connection.execute(
                """
                UPDATE provider_calendars
                SET status = ?, observed_at = ?, last_error_code = ?, updated_at = ?
                WHERE source_id = ?
                """,
                (status, observed_at, error_code, observed_at, self.source_id),
            )
        return ProviderRefreshResult(
            self.source_id,
            status,
            0,
            0,
            0,
            observed_at,
            self._last_successful_sync(),
            error_code,
        )

    def _ensure_source(self) -> None:
        status: ProviderStatus
        if not self.enabled:
            status = "disabled"
        elif not self.configured:
            status = "not_configured"
        else:
            status = "error"
        self._set_source_state(status=status, observed_at=self.now_fn(), error_code=None)

    def _set_source_state(
        self,
        *,
        status: ProviderStatus,
        observed_at: str,
        error_code: str | None,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        if connection is None:
            with self.database.transaction() as connection:
                self._set_source_state(
                    status=status,
                    observed_at=observed_at,
                    error_code=error_code,
                    connection=connection,
                )
            return
        last_successful_sync_at = self._last_successful_sync(connection=connection)
        self._upsert_source(
            connection,
            account_id=self.account_id or "not-configured",
            status=status,
            observed_at=observed_at,
            last_successful_sync_at=last_successful_sync_at,
            error_code=error_code,
        )

    def _last_successful_sync(self, *, connection: sqlite3.Connection | None = None) -> str | None:
        selected = connection or self.database.connection
        row = selected.execute(
            "SELECT last_successful_sync_at FROM provider_sources WHERE source_id = ?",
            (self.source_id,),
        ).fetchone()
        return None if row is None else row["last_successful_sync_at"]


def _source_id(provider: str, account_id: str | None) -> str:
    value = f"{provider}|{account_id or 'not-configured'}"
    return f"{provider}_source_{hashlib.sha256(value.encode()).hexdigest()}"


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
