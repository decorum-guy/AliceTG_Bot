from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from app.planning.audit import AuditWriter
from app.planning.db import PlanningDatabase
from app.planning.delivery_settings import (
    ReminderDeliveryPreferences,
    ReminderDeliveryPreferencesStore,
    legacy_phone_channels,
)
from app.planning.errors import PlanningNotFoundError, PlanningValidationError
from app.planning.models import (
    MutationContext,
    REMINDER_DELIVERY_JOB_TYPE,
    Reminder,
    new_uuid4,
    utc_now,
)
from app.planning.repositories import PlanningRepository
from app.services.reminder_store import (
    ReminderRecord,
    ReminderSettings,
    ReminderSettingsStore,
    ReminderSource,
    ReminderStore,
)


LOGGER = logging.getLogger(__name__)

IMPORT_VERSION = "002_import_legacy_reminders"
LEGACY_IMPORT_TIMEZONE = "UTC"
LEGACY_DELIVERY_INFERRED = "legacy_delivery_inferred"
LEGACY_STATUSES = frozenset({"pending", "fired", "cancelled"})
LEGACY_SOURCES = frozenset({"alice", "telegram"})
LEGACY_ID_PATTERN = re.compile(r"^[0-9a-f]{12}$")
LEGACY_RECORD_KEYS = frozenset(
    {
        "id",
        "text",
        "due_at",
        "delay_seconds",
        "source",
        "created_at",
        "status",
        "chat_id",
        "fired_at",
        "cancelled_at",
    }
)
LEGACY_TOP_LEVEL_KEYS = frozenset({"settings", "reminders"})
LEGACY_SETTINGS_KEYS = frozenset(
    {
        "voice_enabled",
        "voice_station_entity_id",
        "notify_telegram_enabled",
        "notify_iphone_enabled",
    }
)
IMPORT_CONTEXT = MutationContext(
    audience="operator",
    actor_id="legacy-import",
    actor_type="service",
    surface="system",
)


class LegacyImportError(RuntimeError):
    """Base error for the explicit A2 import boundary."""


class LegacySourceChangedError(LegacyImportError):
    """Raised when a completed import is asked to consume another source."""


class LegacyImportNotReadyError(LegacyImportError):
    """Raised when the cutover gate has no matching successful import."""


class LegacyImportVerificationError(LegacyImportError):
    """Raised when post-write semantic verification does not match the source."""


@dataclass(frozen=True)
class LegacyPreflightReport:
    source_sha256: str | None
    source_size_bytes: int
    total_records: int
    status_counts: dict[str, int]
    source_counts: dict[str, int]
    valid_count: int
    invalid_count: int
    duplicate_legacy_ids: tuple[str, ...]
    timestamp_problems: tuple[str, ...]
    semantic_duplicate_count: int
    planning_status_counts: dict[str, int]
    planning_delivery_counts: dict[str, int]
    expected_resulting_rows: int
    mapping_count: int
    semantic_hash: str | None
    settings_present: bool
    settings_valid: bool
    warnings: tuple[str, ...]
    blockers: tuple[str, ...]

    def to_dict(self, *, include_legacy_ids: bool = False) -> dict[str, Any]:
        duplicate_ids: dict[str, Any] = {"count": len(self.duplicate_legacy_ids)}
        if include_legacy_ids:
            duplicate_ids["values"] = list(self.duplicate_legacy_ids)
        return {
            "source_sha256": self.source_sha256,
            "source_size_bytes": self.source_size_bytes,
            "total_records": self.total_records,
            "status_counts": dict(sorted(self.status_counts.items())),
            "source_counts": dict(sorted(self.source_counts.items())),
            "valid_count": self.valid_count,
            "invalid_count": self.invalid_count,
            "duplicate_legacy_ids": duplicate_ids,
            "timestamp_problems": list(self.timestamp_problems),
            "semantic_duplicate_count": self.semantic_duplicate_count,
            "semantic_import_counts": {
                "planning_status": dict(sorted(self.planning_status_counts.items())),
                "planning_delivery_state": dict(sorted(self.planning_delivery_counts.items())),
            },
            "expected_resulting_planning_rows": self.expected_resulting_rows,
            "mapping_count": self.mapping_count,
            "semantic_hash": self.semantic_hash,
            "settings": {
                "present": self.settings_present,
                "valid": self.settings_valid,
                "boundary": "legacy_json_unchanged_by_import",
            },
            "warnings": list(self.warnings),
            "blockers": list(self.blockers),
        }


@dataclass(frozen=True)
class LegacyImportResult:
    source_sha256: str
    semantic_hash: str
    imported_count: int
    mapping_count: int
    already_imported: bool
    report: LegacyPreflightReport

    def to_dict(self) -> dict[str, Any]:
        return {
            "import_version": IMPORT_VERSION,
            "source_sha256": self.source_sha256,
            "semantic_hash": self.semantic_hash,
            "imported_count": self.imported_count,
            "mapping_count": self.mapping_count,
            "already_imported": self.already_imported,
            "report": self.report.to_dict(),
        }


@dataclass(frozen=True)
class _NormalizedLegacyRecord:
    legacy_id: str
    text: str
    due_at: str
    delay_seconds: int
    source: str
    created_at: str
    status: str
    chat_id: int | None
    fired_at: str | None
    cancelled_at: str | None
    due_at_utc: str
    created_at_utc: str
    fired_at_utc: str | None
    cancelled_at_utc: str | None
    planning_status: str
    delivery_state: str
    inferred_semantics: str | None

    def semantic_dict(self) -> dict[str, Any]:
        return {
            "legacy_id": self.legacy_id,
            "text": self.text,
            "due_at_utc": self.due_at_utc,
            "delay_seconds": self.delay_seconds,
            "source": self.source,
            "created_at_utc": self.created_at_utc,
            "legacy_status": self.status,
            "chat_id": self.chat_id,
            "fired_at_utc": self.fired_at_utc,
            "cancelled_at_utc": self.cancelled_at_utc,
            "planning_status": self.planning_status,
            "delivery_state": self.delivery_state,
            "inferred_semantics": self.inferred_semantics,
        }


@dataclass(frozen=True)
class _LoadedLegacySource:
    report: LegacyPreflightReport
    records: tuple[_NormalizedLegacyRecord, ...]


class _RecordValidationError(ValueError):
    def __init__(self, message: str, *codes: str) -> None:
        super().__init__(message)
        self.codes = tuple(codes)


def _sha256_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _semantic_hash(records: tuple[_NormalizedLegacyRecord, ...] | list[_NormalizedLegacyRecord]) -> str:
    payload = [record.semantic_dict() for record in sorted(records, key=lambda item: item.legacy_id)]
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _sha256_bytes(encoded)


def _normalize_timestamp(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _RecordValidationError(f"{field} is not a timestamp", f"invalid_{field}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _RecordValidationError(f"{field} is not a timestamp", f"invalid_{field}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _RecordValidationError(f"{field} is timezone-naive", f"timezone_naive_{field}")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _validate_settings(document: Mapping[str, Any]) -> tuple[bool, tuple[str, ...]]:
    settings = document.get("settings")
    if settings is None:
        return True, ()
    if not isinstance(settings, dict):
        return False, ("settings_not_object",)
    unknown = set(settings) - LEGACY_SETTINGS_KEYS
    if unknown:
        return False, ("settings_unknown_fields",)
    for key in ("voice_enabled", "notify_telegram_enabled", "notify_iphone_enabled"):
        if key in settings and not isinstance(settings[key], bool):
            return False, (f"settings_{key}_not_boolean",)
    if "voice_station_entity_id" in settings:
        value = settings["voice_station_entity_id"]
        if not isinstance(value, str) or not value or len(value) > 256:
            return False, ("settings_voice_station_invalid",)
    return True, ()


def _parse_record(item: Any, index: int) -> _NormalizedLegacyRecord:
    if not isinstance(item, dict):
        raise _RecordValidationError(f"record {index} is not an object", "record_not_object")
    unknown = set(item) - LEGACY_RECORD_KEYS
    if unknown:
        raise _RecordValidationError(f"record {index} has unknown fields", "record_unknown_fields")
    required = {"id", "text", "due_at", "delay_seconds", "source", "created_at"}
    missing = required - set(item)
    if missing:
        raise _RecordValidationError(f"record {index} is missing required fields", "record_missing_fields")

    legacy_id = item["id"]
    if not isinstance(legacy_id, str) or LEGACY_ID_PATTERN.fullmatch(legacy_id) is None:
        raise _RecordValidationError(f"record {index} has an invalid legacy id", "legacy_id_invalid")
    text = item["text"]
    if not isinstance(text, str) or not text.strip() or len(text) > 500:
        raise _RecordValidationError(f"record {index} has invalid reminder text", "text_invalid")
    delay_seconds = item["delay_seconds"]
    if isinstance(delay_seconds, bool) or not isinstance(delay_seconds, int) or delay_seconds < 0:
        raise _RecordValidationError(f"record {index} has invalid delay_seconds", "delay_invalid")
    source = item["source"]
    if not isinstance(source, str) or source not in LEGACY_SOURCES:
        raise _RecordValidationError(f"record {index} has an invalid source", "source_invalid")
    status = item.get("status", "pending")
    if not isinstance(status, str) or status not in LEGACY_STATUSES:
        raise _RecordValidationError(f"record {index} has an invalid status", "status_invalid")
    chat_id = item.get("chat_id")
    if chat_id is not None and (isinstance(chat_id, bool) or not isinstance(chat_id, int)):
        raise _RecordValidationError(f"record {index} has an invalid chat_id", "chat_id_invalid")

    due_at = item["due_at"]
    created_at = item["created_at"]
    fired_at = item.get("fired_at")
    cancelled_at = item.get("cancelled_at")
    due_at_utc = _normalize_timestamp(due_at, "due_at")
    created_at_utc = _normalize_timestamp(created_at, "created_at")
    fired_at_utc = None if fired_at is None else _normalize_timestamp(fired_at, "fired_at")
    cancelled_at_utc = None if cancelled_at is None else _normalize_timestamp(cancelled_at, "cancelled_at")

    if status == "fired":
        if fired_at is None:
            raise _RecordValidationError(f"record {index} fired status has no fired_at", "fired_at_missing")
        if cancelled_at is not None:
            raise _RecordValidationError(
                f"record {index} fired status has cancelled_at", "fired_cancelled_conflict"
            )
        planning_status = "completed"
        delivery_state = "delivered"
        inferred_semantics = LEGACY_DELIVERY_INFERRED
    elif status == "cancelled":
        if cancelled_at is None:
            raise _RecordValidationError(
                f"record {index} cancelled status has no cancelled_at", "cancelled_at_missing"
            )
        if fired_at is not None:
            raise _RecordValidationError(
                f"record {index} cancelled status has fired_at", "cancelled_fired_conflict"
            )
        planning_status = "cancelled"
        delivery_state = "not_due"
        inferred_semantics = None
    else:
        if fired_at is not None or cancelled_at is not None:
            raise _RecordValidationError(
                f"record {index} pending status has terminal timestamp", "pending_terminal_timestamp"
            )
        planning_status = "pending"
        delivery_state = "not_due"
        inferred_semantics = None

    return _NormalizedLegacyRecord(
        legacy_id=legacy_id,
        text=text,
        due_at=str(due_at),
        delay_seconds=delay_seconds,
        source=source,
        created_at=str(created_at),
        status=status,
        chat_id=chat_id,
        fired_at=None if fired_at is None else str(fired_at),
        cancelled_at=None if cancelled_at is None else str(cancelled_at),
        due_at_utc=due_at_utc,
        created_at_utc=created_at_utc,
        fired_at_utc=fired_at_utc,
        cancelled_at_utc=cancelled_at_utc,
        planning_status=planning_status,
        delivery_state=delivery_state,
        inferred_semantics=inferred_semantics,
    )


def _load_source(path: str | Path) -> _LoadedLegacySource:
    source_path = Path(path)
    try:
        raw = source_path.read_bytes()
    except OSError:
        report = LegacyPreflightReport(
            source_sha256=None,
            source_size_bytes=0,
            total_records=0,
            status_counts={},
            source_counts={},
            valid_count=0,
            invalid_count=0,
            duplicate_legacy_ids=(),
            timestamp_problems=(),
            semantic_duplicate_count=0,
            planning_status_counts={},
            planning_delivery_counts={},
            expected_resulting_rows=0,
            mapping_count=0,
            semantic_hash=None,
            settings_present=False,
            settings_valid=False,
            warnings=(),
            blockers=("source_file_unreadable",),
        )
        return _LoadedLegacySource(report, ())

    source_sha256 = _sha256_bytes(raw)
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        report = LegacyPreflightReport(
            source_sha256=source_sha256,
            source_size_bytes=len(raw),
            total_records=0,
            status_counts={},
            source_counts={},
            valid_count=0,
            invalid_count=0,
            duplicate_legacy_ids=(),
            timestamp_problems=(),
            semantic_duplicate_count=0,
            planning_status_counts={},
            planning_delivery_counts={},
            expected_resulting_rows=0,
            mapping_count=0,
            semantic_hash=None,
            settings_present=False,
            settings_valid=False,
            warnings=(),
            blockers=("source_json_invalid",),
        )
        return _LoadedLegacySource(report, ())

    blockers: list[str] = []
    warnings: list[str] = ["settings_remain_in_legacy_json"]
    if not isinstance(document, dict):
        blockers.append("top_level_not_object")
        report = LegacyPreflightReport(
            source_sha256=source_sha256,
            source_size_bytes=len(raw),
            total_records=0,
            status_counts={},
            source_counts={},
            valid_count=0,
            invalid_count=0,
            duplicate_legacy_ids=(),
            timestamp_problems=(),
            semantic_duplicate_count=0,
            planning_status_counts={},
            planning_delivery_counts={},
            expected_resulting_rows=0,
            mapping_count=0,
            semantic_hash=None,
            settings_present=False,
            settings_valid=False,
            warnings=tuple(warnings),
            blockers=tuple(blockers),
        )
        return _LoadedLegacySource(report, ())

    unknown_top_level = set(document) - LEGACY_TOP_LEVEL_KEYS
    if unknown_top_level:
        blockers.append("top_level_unknown_fields")
    if "reminders" not in document or not isinstance(document.get("reminders"), list):
        blockers.append("reminders_not_list")
        reminders: list[Any] = []
    else:
        reminders = document["reminders"]

    settings_present = "settings" in document
    settings_valid, settings_problems = _validate_settings(document)
    if not settings_valid:
        blockers.append("settings_invalid")
        warnings.extend(settings_problems)

    total_records = len(reminders)
    status_counts: Counter[str] = Counter()
    source_counts: Counter[str] = Counter()
    parsed_records: list[_NormalizedLegacyRecord] = []
    validation_error_count = 0
    timestamp_problems: set[str] = set()
    legacy_id_counts: Counter[str] = Counter()
    for index, item in enumerate(reminders):
        if isinstance(item, dict):
            status = item.get("status", "pending")
            source = item.get("source")
            if isinstance(status, str):
                status_counts[status if status in LEGACY_STATUSES else "unknown"] += 1
            if isinstance(source, str):
                source_counts[source if source in LEGACY_SOURCES else "unknown"] += 1
            legacy_id = item.get("id")
            if isinstance(legacy_id, str):
                legacy_id_counts[legacy_id] += 1
        try:
            parsed = _parse_record(item, index)
        except _RecordValidationError as exc:
            validation_error_count += 1
            timestamp_problems.update(code for code in exc.codes if "timestamp" in code or "timezone" in code)
            continue
        parsed_records.append(parsed)

    duplicate_legacy_ids = tuple(sorted(legacy_id for legacy_id, count in legacy_id_counts.items() if count > 1))
    if duplicate_legacy_ids:
        blockers.append("duplicate_legacy_id")
    if validation_error_count:
        blockers.append("record_validation")
    valid_records = tuple(record for record in parsed_records if record.legacy_id not in duplicate_legacy_ids)
    valid_count = len(valid_records)
    # Every source item is counted exactly once.  A malformed item can also
    # carry a duplicated id, so adding the two diagnostics would overcount
    # invalid records.
    invalid_count = total_records - valid_count

    semantic_keys = [
        json.dumps(
            {key: value for key, value in record.semantic_dict().items() if key != "legacy_id"},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        for record in valid_records
    ]
    semantic_duplicate_count = len(semantic_keys) - len(set(semantic_keys))
    if semantic_duplicate_count:
        warnings.append("semantic_duplicate_records_preserved_by_legacy_id")

    planning_status_counts = Counter(record.planning_status for record in valid_records)
    planning_delivery_counts = Counter(record.delivery_state for record in valid_records)
    source_semantic_hash = None if blockers else _semantic_hash(list(valid_records))
    report = LegacyPreflightReport(
        source_sha256=source_sha256,
        source_size_bytes=len(raw),
        total_records=total_records,
        status_counts=dict(status_counts),
        source_counts=dict(source_counts),
        valid_count=valid_count,
        invalid_count=invalid_count,
        duplicate_legacy_ids=duplicate_legacy_ids,
        timestamp_problems=tuple(sorted(timestamp_problems)),
        semantic_duplicate_count=semantic_duplicate_count,
        planning_status_counts=dict(planning_status_counts),
        planning_delivery_counts=dict(planning_delivery_counts),
        expected_resulting_rows=valid_count if not blockers else 0,
        mapping_count=valid_count if not blockers else 0,
        semantic_hash=source_semantic_hash,
        settings_present=settings_present,
        settings_valid=settings_valid,
        warnings=tuple(dict.fromkeys(warnings)),
        blockers=tuple(dict.fromkeys(blockers)),
    )
    return _LoadedLegacySource(report, valid_records)


class LegacyReminderImporter:
    """Strict, transaction-bound import boundary for the legacy JSON store."""

    def __init__(
        self,
        database: PlanningDatabase,
        *,
        audit: AuditWriter | None = None,
        import_version: str = IMPORT_VERSION,
    ) -> None:
        self.database = database
        self.audit = audit or AuditWriter(database)
        self.import_version = import_version

    def preflight(self, source_path: str | Path) -> LegacyPreflightReport:
        return _load_source(source_path).report

    def import_file(self, source_path: str | Path) -> LegacyImportResult:
        loaded = _load_source(source_path)
        report = loaded.report
        if report.blockers:
            raise LegacyImportError(self._blocked_message(report))
        if report.source_sha256 is None or report.semantic_hash is None:
            raise LegacyImportError("legacy source has no usable hash")

        with self.database.transaction():
            marker = self.database.connection.execute(
                "SELECT * FROM legacy_reminder_imports WHERE import_version = ?",
                (self.import_version,),
            ).fetchone()
            if marker is not None:
                marker_source_hash = str(marker["source_sha256"])
                if marker_source_hash != report.source_sha256:
                    raise LegacySourceChangedError(
                        "legacy source hash changed after the completed import; operator review is required"
                    )
                if (
                    int(marker["imported_count"]) != report.expected_resulting_rows
                    or int(marker["mapping_count"]) != report.mapping_count
                    or str(marker["semantic_hash"]) != report.semantic_hash
                ):
                    raise LegacyImportVerificationError("completed import marker does not match the source report")
                return LegacyImportResult(
                    source_sha256=report.source_sha256,
                    semantic_hash=report.semantic_hash,
                    imported_count=0,
                    mapping_count=0,
                    already_imported=True,
                    report=report,
                )

            orphaned_mappings = self.database.connection.execute(
                "SELECT COUNT(*) FROM legacy_reminder_mappings WHERE origin = 'legacy'"
            ).fetchone()[0]
            if int(orphaned_mappings) != 0:
                raise LegacyImportError("legacy mappings exist without a completed import marker")

            imported_at = utc_now()
            for record in loaded.records:
                planning_id = self._insert_imported_record(record, report.source_sha256, imported_at)
                self._record_import_audit(record, planning_id, report.source_sha256)

            self._verify_import(loaded.records, report)
            report_json = json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            self.database.connection.execute(
                """
                INSERT INTO legacy_reminder_imports(
                    id, import_version, source_sha256, status, imported_count,
                    mapping_count, semantic_hash, report_json, started_at, completed_at
                ) VALUES (?, ?, ?, 'completed', ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_uuid4(),
                    self.import_version,
                    report.source_sha256,
                    report.expected_resulting_rows,
                    report.mapping_count,
                    report.semantic_hash,
                    report_json,
                    imported_at,
                    utc_now(),
                ),
            )
        return LegacyImportResult(
            source_sha256=report.source_sha256,
            semantic_hash=report.semantic_hash,
            imported_count=report.expected_resulting_rows,
            mapping_count=report.mapping_count,
            already_imported=False,
            report=report,
        )

    def require_cutover_ready(self, source_path: str | Path) -> Mapping[str, Any]:
        loaded = _load_source(source_path)
        report = loaded.report
        if report.blockers:
            raise LegacyImportNotReadyError(self._blocked_message(report))
        if report.source_sha256 is None or report.semantic_hash is None:
            raise LegacyImportNotReadyError("legacy source has no usable hash")
        marker = self.database.connection.execute(
            "SELECT * FROM legacy_reminder_imports WHERE import_version = ?",
            (self.import_version,),
        ).fetchone()
        if marker is None:
            raise LegacyImportNotReadyError("Planning cutover requires a completed legacy import marker")
        if str(marker["semantic_hash"]) != report.semantic_hash:
            raise LegacySourceChangedError(
                "legacy reminder records changed after import; operator review is required before cutover"
            )
        if int(marker["imported_count"]) != report.expected_resulting_rows:
            raise LegacyImportVerificationError("cutover source count does not match the completed import marker")
        self._verify_import(loaded.records, report)
        if str(marker["source_sha256"]) != report.source_sha256:
            LOGGER.warning(
                "legacy source bytes changed after import but normalized reminder semantics are unchanged; "
                "treating this as a settings/format-only change"
            )
        return dict(marker)

    def _insert_imported_record(self, record: _NormalizedLegacyRecord, source_sha256: str, imported_at: str) -> str:
        planning_id = new_uuid4()
        audit_correlation_id = new_uuid4()
        completed_at = record.fired_at_utc if record.planning_status == "completed" else None
        cancelled_at = record.cancelled_at_utc if record.planning_status == "cancelled" else None
        deleted_at = cancelled_at
        self.database.connection.execute(
            """
            INSERT INTO reminders(
                id, title, notes, due_at_utc, timezone, status, source, source_ref,
                created_by, completed_at, cancelled_at, delivery_state,
                next_attempt_at, final_failure_at, version, created_at, updated_at,
                audit_correlation_id, deleted_at
            ) VALUES (?, ?, NULL, ?, ?, ?, ?, NULL, ?, ?, ?, ?, NULL, NULL, 1, ?, ?, ?, ?)
            """,
            (
                planning_id,
                record.text,
                record.due_at_utc,
                LEGACY_IMPORT_TIMEZONE,
                record.planning_status,
                record.source,
                "legacy-import",
                completed_at,
                cancelled_at,
                record.delivery_state,
                record.created_at_utc,
                imported_at,
                audit_correlation_id,
                deleted_at,
            ),
        )
        self.database.connection.execute(
            """
            INSERT INTO legacy_reminder_mappings(
                planning_id, origin, legacy_id, legacy_source, legacy_status,
                legacy_created_at, legacy_created_at_utc, legacy_due_at, legacy_due_at_utc,
                legacy_fired_at, legacy_fired_at_utc, legacy_cancelled_at,
                legacy_cancelled_at_utc, legacy_chat_id, legacy_delay_seconds,
                import_version, source_sha256, inferred_semantics, created_at
            ) VALUES (?, 'legacy', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                planning_id,
                record.legacy_id,
                record.source,
                record.status,
                record.created_at,
                record.created_at_utc,
                record.due_at,
                record.due_at_utc,
                record.fired_at,
                record.fired_at_utc,
                record.cancelled_at,
                record.cancelled_at_utc,
                record.chat_id,
                record.delay_seconds,
                self.import_version,
                source_sha256,
                record.inferred_semantics,
                imported_at,
            ),
        )
        return planning_id

    def _record_import_audit(
        self,
        record: _NormalizedLegacyRecord,
        planning_id: str,
        source_sha256: str,
    ) -> None:
        after = {
            "import_version": self.import_version,
            "source_sha256": source_sha256,
            "legacy_id": record.legacy_id,
            "legacy_status": record.status,
            "legacy_source": record.source,
            "original_fired_at": record.fired_at,
            "original_cancelled_at": record.cancelled_at,
            "markers": [] if record.inferred_semantics is None else [record.inferred_semantics],
        }
        self.audit.record(
            context=IMPORT_CONTEXT,
            action="legacy_import",
            object_domain="reminder",
            object_id=planning_id,
            old_version=None,
            new_version=1,
            before=None,
            after=after,
            correlation_id=new_uuid4(),
        )

    def _verify_import(
        self,
        records: tuple[_NormalizedLegacyRecord, ...],
        report: LegacyPreflightReport,
    ) -> None:
        rows = self.database.connection.execute(
            """
            SELECT
                m.legacy_id, m.legacy_source, m.legacy_status,
                m.legacy_created_at_utc, m.legacy_due_at_utc,
                m.legacy_fired_at_utc, m.legacy_cancelled_at_utc,
                m.legacy_chat_id, m.legacy_delay_seconds, m.inferred_semantics,
                r.title, r.status, r.delivery_state
            FROM legacy_reminder_mappings AS m
            JOIN reminders AS r ON r.id = m.planning_id
            WHERE m.origin = 'legacy' AND m.import_version = ?
            ORDER BY m.legacy_id
            """,
            (self.import_version,),
        ).fetchall()
        if len(rows) != len(records):
            raise LegacyImportVerificationError("imported mapping count does not match the source")
        expected = {record.legacy_id: record for record in records}
        actual_semantic: list[dict[str, Any]] = []
        status_counts: Counter[str] = Counter()
        delivery_counts: Counter[str] = Counter()
        for row in rows:
            legacy_id = str(row["legacy_id"])
            record = expected.get(legacy_id)
            if record is None:
                raise LegacyImportVerificationError("import created an unexpected legacy mapping")
            if str(row["title"]) != record.text:
                raise LegacyImportVerificationError("imported reminder title does not match the source")
            status_counts[str(row["status"])] += 1
            delivery_counts[str(row["delivery_state"])] += 1
            actual_semantic.append(
                {
                    "legacy_id": legacy_id,
                    "text": str(row["title"]),
                    "due_at_utc": str(row["legacy_due_at_utc"]),
                    "delay_seconds": int(row["legacy_delay_seconds"]),
                    "source": str(row["legacy_source"]),
                    "created_at_utc": str(row["legacy_created_at_utc"]),
                    "legacy_status": str(row["legacy_status"]),
                    "chat_id": row["legacy_chat_id"],
                    "fired_at_utc": row["legacy_fired_at_utc"],
                    "cancelled_at_utc": row["legacy_cancelled_at_utc"],
                    "planning_status": str(row["status"]),
                    "delivery_state": str(row["delivery_state"]),
                    "inferred_semantics": row["inferred_semantics"],
                }
            )
        encoded = json.dumps(
            sorted(actual_semantic, key=lambda item: str(item["legacy_id"])),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if _sha256_bytes(encoded) != report.semantic_hash:
            raise LegacyImportVerificationError("imported semantic hash does not match the source")
        if dict(status_counts) != report.planning_status_counts:
            raise LegacyImportVerificationError("imported status counts do not match the source")
        if dict(delivery_counts) != report.planning_delivery_counts:
            raise LegacyImportVerificationError("imported delivery counts do not match the source")

    @staticmethod
    def _blocked_message(report: LegacyPreflightReport) -> str:
        return "legacy source preflight blocked import: " + ", ".join(report.blockers)


class PlanningReminderStoreAdapter:
    """ReminderStore-shaped adapter over Planning and canonical owner policy."""

    def __init__(self, database: PlanningDatabase, settings_store: ReminderSettingsStore) -> None:
        self.database = database
        self.settings_store = settings_store
        self.repository = PlanningRepository(database)
        self.delivery_preferences = ReminderDeliveryPreferencesStore(database)

    async def create(
        self,
        *,
        text: str,
        due_at: datetime,
        delay_seconds: int,
        source: ReminderSource,
        chat_id: int,
    ) -> ReminderRecord:
        if source not in LEGACY_SOURCES:
            raise LegacyImportError("native reminder source is invalid")
        if isinstance(delay_seconds, bool) or not isinstance(delay_seconds, int) or delay_seconds < 0:
            raise LegacyImportError("native reminder delay_seconds is invalid")
        if isinstance(chat_id, bool) or not isinstance(chat_id, int):
            raise LegacyImportError("native reminder chat_id is invalid")
        due_at_utc = _normalize_runtime_datetime(due_at)
        surface = "ha" if source == "alice" else "telegram"
        context = MutationContext(
            audience="operator",
            actor_id="reminder-adapter",
            actor_type="service",
            surface=surface,
        )
        with self.database.transaction():
            reminder = self.repository.create_reminder(
                title=text,
                due_at_utc=due_at_utc,
                timezone=LEGACY_IMPORT_TIMEZONE,
                context=context,
                outbox_job_type=REMINDER_DELIVERY_JOB_TYPE,
                outbox_payload={"chat_id": chat_id},
            )
            self.database.connection.execute(
                """
                INSERT INTO legacy_reminder_mappings(
                    planning_id, origin, legacy_chat_id, legacy_delay_seconds, created_at
                ) VALUES (?, 'native', ?, ?, ?)
                """,
                (reminder.id, chat_id, delay_seconds, utc_now()),
            )
        return self._to_legacy_record(reminder, self._metadata(reminder.id), for_get=False)

    async def list_pending(self) -> list[ReminderRecord]:
        rows = self.database.connection.execute(
            """
            SELECT id FROM reminders
            WHERE deleted_at IS NULL AND status IN ('pending', 'due')
            ORDER BY due_at_utc, id
            """
        ).fetchall()
        result: list[ReminderRecord] = []
        for row in rows:
            reminder = self.repository.get_reminder(str(row["id"]))
            result.append(self._to_legacy_record(reminder, self._metadata(reminder.id), for_get=False))
        return result

    async def get(self, reminder_id: str) -> ReminderRecord | None:
        planning_id = self._resolve_planning_id(reminder_id)
        try:
            reminder = self.repository.get_reminder(planning_id)
        except (PlanningNotFoundError, PlanningValidationError):
            return None
        return self._to_legacy_record(reminder, self._metadata(reminder.id), for_get=True)

    async def mark_fired(self, reminder_id: str) -> None:
        planning_id = self._resolve_planning_id(reminder_id)
        try:
            reminder = self.repository.get_reminder(planning_id)
        except (PlanningNotFoundError, PlanningValidationError):
            return
        if reminder.status in {"completed", "cancelled"} or reminder.delivery_state == "delivered":
            return
        context = MutationContext(
            audience="operator",
            actor_id="reminder-adapter",
            actor_type="service",
            surface="system",
        )
        self.repository.update_reminder(
            reminder.id,
            expected_version=reminder.version,
            context=context,
            delivery_state="delivered",
        )

    async def cancel(self, reminder_id: str) -> bool:
        planning_id = self._resolve_planning_id(reminder_id)
        try:
            reminder = self.repository.get_reminder(planning_id)
        except (PlanningNotFoundError, PlanningValidationError):
            return False
        if reminder.status not in {"pending", "due"} or reminder.deleted_at is not None:
            return False
        context = MutationContext(
            audience="operator",
            actor_id="reminder-adapter",
            actor_type="service",
            surface="telegram",
        )
        self.repository.cancel_reminder(
            reminder.id,
            expected_version=reminder.version,
            context=context,
        )
        return True

    async def get_settings(self) -> ReminderSettings:
        legacy = await self.settings_store.get_settings()
        preferences = self.delivery_preferences.ensure_from_legacy(
            spoken_endpoint=legacy.spoken_endpoint,
            notify_telegram_enabled=legacy.notify_telegram_enabled,
            notify_iphone_enabled=legacy.notify_iphone_enabled,
        )
        return ReminderSettings(
            voice_enabled=legacy.voice_enabled,
            voice_station_entity_id=legacy.voice_station_entity_id,
            notify_telegram_enabled="telegram" in preferences.phone_channels,
            notify_iphone_enabled="home_assistant" in preferences.phone_channels,
            spoken_endpoint=preferences.spoken_endpoint,
            phone_channels=preferences.phone_channels,
        )

    async def get_delivery_preferences(self) -> ReminderDeliveryPreferences:
        legacy = await self.settings_store.get_settings()
        return self.delivery_preferences.ensure_from_legacy(
            spoken_endpoint=legacy.spoken_endpoint,
            notify_telegram_enabled=legacy.notify_telegram_enabled,
            notify_iphone_enabled=legacy.notify_iphone_enabled,
        )

    async def update_delivery_preferences(
        self,
        *,
        expected_revision: int,
        spoken_endpoint: str,
        phone_channels: tuple[str, ...],
    ) -> ReminderDeliveryPreferences:
        legacy = await self.settings_store.get_settings()
        self.delivery_preferences.ensure_from_legacy(
            spoken_endpoint=legacy.spoken_endpoint,
            notify_telegram_enabled=legacy.notify_telegram_enabled,
            notify_iphone_enabled=legacy.notify_iphone_enabled,
        )
        return self.delivery_preferences.update(
            expected_revision=expected_revision,
            spoken_endpoint=spoken_endpoint,
            phone_channels=phone_channels,
            context=MutationContext(
                audience="operator",
                actor_id="control-center",
                actor_type="service",
                surface="panel-agent",
            ),
        )

    async def update_settings(
        self,
        *,
        voice_enabled: bool | None = None,
        voice_station_entity_id: str | None = None,
        notify_telegram_enabled: bool | None = None,
        notify_iphone_enabled: bool | None = None,
        spoken_endpoint: str | None = None,
        phone_channels: tuple[str, ...] | None = None,
    ) -> ReminderSettings:
        updated = await self.settings_store.update_settings(
            voice_enabled=voice_enabled,
            voice_station_entity_id=voice_station_entity_id,
            notify_telegram_enabled=notify_telegram_enabled,
            notify_iphone_enabled=notify_iphone_enabled,
            spoken_endpoint=spoken_endpoint,
            phone_channels=phone_channels,
        )
        current = await self.get_delivery_preferences()
        selected = phone_channels or legacy_phone_channels(
            notify_telegram_enabled=updated.notify_telegram_enabled,
            notify_iphone_enabled=updated.notify_iphone_enabled,
        )
        if (
            spoken_endpoint is not None
            or phone_channels is not None
            or notify_telegram_enabled is not None
            or notify_iphone_enabled is not None
        ) and (current.spoken_endpoint != updated.spoken_endpoint or current.phone_channels != selected):
            self.delivery_preferences.update(
                expected_revision=current.revision,
                spoken_endpoint=updated.spoken_endpoint,
                phone_channels=selected,
                context=MutationContext(
                    audience="operator",
                    actor_id="reminder-adapter",
                    actor_type="service",
                    surface="system",
                ),
            )
        return await self.get_settings()

    def _metadata(self, planning_id: str):
        return self.database.connection.execute(
            "SELECT * FROM legacy_reminder_mappings WHERE planning_id = ?",
            (planning_id,),
        ).fetchone()

    def _resolve_planning_id(self, identifier: str) -> str:
        mapping = self.database.connection.execute(
            "SELECT planning_id FROM legacy_reminder_mappings WHERE legacy_id = ?",
            (identifier,),
        ).fetchone()
        return str(mapping["planning_id"]) if mapping is not None else identifier

    @staticmethod
    def _to_legacy_record(reminder: Reminder, metadata: Any, *, for_get: bool) -> ReminderRecord:
        legacy_source = None if metadata is None else metadata["legacy_source"]
        source = legacy_source or reminder.source
        if source not in LEGACY_SOURCES:
            source = "telegram"
        if reminder.status == "cancelled":
            status = "cancelled"
        elif reminder.status == "completed":
            status = "fired"
        elif for_get and reminder.delivery_state == "delivered":
            # Keep the old scheduler from sending a delivered active Planning row again.
            status = "fired"
        else:
            status = "pending"
        delay_seconds = 0 if metadata is None or metadata["legacy_delay_seconds"] is None else int(
            metadata["legacy_delay_seconds"]
        )
        fired_at = reminder.completed_at
        if metadata is not None and metadata["legacy_fired_at_utc"] is not None:
            fired_at = str(metadata["legacy_fired_at_utc"])
        return ReminderRecord(
            id=reminder.id if metadata is None or metadata["legacy_id"] is None else str(metadata["legacy_id"]),
            text=reminder.title,
            due_at=reminder.due_at_utc,
            delay_seconds=delay_seconds,
            source=source,  # type: ignore[arg-type]
            created_at=reminder.created_at,
            status=status,  # type: ignore[arg-type]
            chat_id=None if metadata is None else metadata["legacy_chat_id"],
            fired_at=fired_at,
            cancelled_at=reminder.cancelled_at,
        )


def _normalize_runtime_datetime(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise LegacyImportError("runtime reminder datetime must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def build_reminder_store(
    *,
    reminders_state_path: str,
    planning_db_path: str,
    cutover_enabled: bool,
) -> tuple[ReminderStore | PlanningReminderStoreAdapter, PlanningDatabase | None]:
    """Build the explicitly gated store; this function never auto-imports JSON."""

    if not cutover_enabled:
        return ReminderStore(reminders_state_path), None
    database = PlanningDatabase(planning_db_path)
    try:
        LegacyReminderImporter(database).require_cutover_ready(reminders_state_path)
    except BaseException:
        database.close()
        raise
    return PlanningReminderStoreAdapter(database, ReminderSettingsStore(reminders_state_path)), database


def _build_cli() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="A2 legacy reminder preflight/import boundary")
    subparsers = parser.add_subparsers(dest="command", required=True)
    preflight = subparsers.add_parser("preflight", help="read and structurally report a legacy JSON source")
    preflight.add_argument("--source", required=True, type=Path)
    import_command = subparsers.add_parser("import", help="transactionally import a legacy JSON source")
    import_command.add_argument("--source", required=True, type=Path)
    import_command.add_argument("--db", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_cli().parse_args(argv)
    if args.command == "preflight":
        report = _load_source(args.source).report
        print(json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True, indent=2))
        return 1 if report.blockers else 0
    database = PlanningDatabase(args.db)
    try:
        result = LegacyReminderImporter(database).import_file(args.source)
    except LegacyImportError as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False, sort_keys=True))
        return 1
    finally:
        database.close()
    print(json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
