"""Encrypted online backups and isolated Planning restore verification.

The module has no Telegram, Home Assistant, HTTP or Control Center imports.
The only source snapshot mechanism is SQLite's native online backup API.  The
resulting standalone database and its redacted manifest are packaged together
and authenticated/encrypted with AES-256-GCM using a dedicated 32-byte key.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from app.planning.db import PlanningDatabase
from app.planning.db import PlanningDatabaseConfig
from app.planning.errors import PlanningError
from app.planning.migrations import MIGRATIONS
from app.planning.models import utc_now
from app.planning.operations import PlanningOperationsState, PlanningOperationsStateStore

try:  # Keep non-backup application imports usable when the optional runtime is absent.
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
except ImportError:  # pragma: no cover - exercised by the missing-runtime guard.
    InvalidTag = Exception  # type: ignore[assignment,misc]
    Cipher = algorithms = modes = None  # type: ignore[assignment]


BACKUP_SCHEMA_VERSION = 5
BACKUP_MANIFEST_VERSION = 1
BACKUP_PACKAGE_FORMAT = "planning-backup-a8"
BACKUP_MAGIC = b"PLANNING-BACKUP-A8\x00"
BACKUP_CIPHER_VERSION = 1
BACKUP_NONCE_BYTES = 12
BACKUP_TAG_BYTES = 16
BACKUP_CHUNK_BYTES = 1024 * 1024
BACKUP_MANIFEST_MAX_BYTES = 128 * 1024
DEFAULT_BACKUP_DIR = "/app/data/backups/planning"
DEFAULT_BACKUP_RETENTION_COUNT = 14
DEFAULT_BACKUP_INTERVAL_SECONDS = 86_400

_BACKUP_NAME_RE = re.compile(
    r"^planning-(?P<created>\d{8}T\d{6}Z)-schema(?P<schema>\d+)-(?P<nonce>[0-9a-f]{12})\.sqlite3\.a8$"
)
_KEY_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ALLOWED_PACKAGE_MEMBERS = frozenset({"manifest.json", "planning.sqlite3"})
_CORE_TABLES = (
    "schema_migrations",
    "projects",
    "reminders",
    "tasks",
    "calendar_events",
    "idempotency_keys",
    "outbox",
    "delivery_attempts",
    "audit_events",
    "provider_mappings",
    "sync_cursors",
    "sync_conflicts",
    "legacy_reminder_imports",
    "legacy_reminder_mappings",
    "telegram_action_tokens",
    "provider_sources",
    "provider_calendars",
    "provider_event_cache",
)
_EPHEMERAL_PREFIXES = (
    "/tmp/",
    "/private/tmp/",
    "/var/tmp/",
    "/private/var/folders/",
    "/dev/shm/",
)


class PlanningBackupError(PlanningError):
    """Base class for safe, content-free backup failures."""

    def __init__(self, code: str, category: str = "backup") -> None:
        self.code = code
        self.category = category
        super().__init__(code)


class PlanningBackupConfigurationError(PlanningBackupError):
    pass


class PlanningBackupVerificationError(PlanningBackupError):
    pass


@dataclass(frozen=True)
class BackupResult:
    package_name: str
    created_at: str
    schema_version: int
    database_size_bytes: int
    retained_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": True,
            "package": self.package_name,
            "created_at": self.created_at,
            "schema_version": self.schema_version,
            "database_size_bytes": self.database_size_bytes,
            "retained_count": self.retained_count,
            "encrypted": True,
            "encryption_mode": "AES-256-GCM",
        }


@dataclass(frozen=True)
class BackupListEntry:
    package_name: str
    created_at: str
    schema_version: int
    size_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "package": self.package_name,
            "created_at": self.created_at,
            "schema_version": self.schema_version,
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True)
class RestoreVerificationResult:
    package_name: str
    manifest_version: int
    source_schema_version: int
    verified_schema_version: int
    database_size_bytes: int
    table_counts: Mapping[str, int]
    migrated_in_isolation: bool
    invalidated_capabilities: int
    resumable_due_jobs: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": True,
            "package": self.package_name,
            "manifest_version": self.manifest_version,
            "source_schema_version": self.source_schema_version,
            "verified_schema_version": self.verified_schema_version,
            "database_size_bytes": self.database_size_bytes,
            "table_counts": dict(self.table_counts),
            "migrated_in_isolation": self.migrated_in_isolation,
            "invalidated_capabilities": self.invalidated_capabilities,
            "resumable_due_jobs": self.resumable_due_jobs,
            "original_artifact_modified": False,
        }


@dataclass(frozen=True)
class _DatabaseInspection:
    schema_version: int
    table_counts: Mapping[str, int]
    integrity_check: str
    foreign_key_errors: int


def parse_encryption_key(value: str | bytes | None) -> bytes:
    """Parse the dedicated 64-hex-character operational secret.

    A raw high-entropy key is used directly.  No password KDF is involved and
    this key is never placed in a manifest, log record or status response.
    """

    if isinstance(value, bytes):
        if len(value) != 32 or not any(value):
            raise PlanningBackupConfigurationError("invalid_encryption_key", "encryption")
        return value
    if not isinstance(value, str) or _KEY_RE.fullmatch(value.strip()) is None:
        raise PlanningBackupConfigurationError("invalid_encryption_key", "encryption")
    try:
        key = bytes.fromhex(value.strip())
    except ValueError as exc:
        raise PlanningBackupConfigurationError("invalid_encryption_key", "encryption") from exc
    if len(key) != 32 or not any(key):
        raise PlanningBackupConfigurationError("invalid_encryption_key", "encryption")
    return key


class PlanningBackupService:
    """Create and retain encrypted snapshots without stopping Planning."""

    def __init__(
        self,
        database: PlanningDatabase,
        *,
        backup_dir: str | os.PathLike[str] = DEFAULT_BACKUP_DIR,
        encryption_key: str | bytes | None,
        retention_count: int = DEFAULT_BACKUP_RETENTION_COUNT,
        application_version: str = "unknown",
        application_commit: str = "unknown",
        environment: str = "development",
        now_fn: Callable[[], str] = utc_now,
        state_store: PlanningOperationsStateStore | None = None,
    ) -> None:
        self.database = database
        self.backup_dir = _validate_backup_directory(backup_dir, environment=environment)
        self.encryption_key = parse_encryption_key(encryption_key)
        if isinstance(retention_count, bool) or not 1 <= retention_count <= 365:
            raise PlanningBackupConfigurationError("invalid_retention_count", "configuration")
        self.retention_count = retention_count
        self.application_version = _bounded_metadata(application_version)
        self.application_commit = _bounded_metadata(application_commit)
        self.now_fn = now_fn
        self.state_store = state_store or PlanningOperationsStateStore(self.backup_dir)

    def backup(self) -> BackupResult:
        created_at = self.now_fn()
        self._record_attempt(created_at)
        try:
            source_schema_version = self.database.schema_version()
            package_name = _package_name(created_at, source_schema_version)
        except (OSError, sqlite3.Error) as exc:
            self._record_failure(created_at, "database_unavailable")
            raise PlanningBackupError("database_unavailable", "database") from exc
        final_path = self.backup_dir / package_name
        temporary_final: Path | None = None
        try:
            _ensure_backup_directory(self.backup_dir)
            with tempfile.TemporaryDirectory(prefix=".planning-backup-", dir=self.backup_dir) as work_name:
                work_dir = Path(work_name)
                snapshot_path = work_dir / "planning.sqlite3"
                self._create_snapshot(snapshot_path)
                inspection = _inspect_database(snapshot_path, require_core=True)
                if inspection.integrity_check != "ok":
                    raise PlanningBackupError("integrity_check_failed", "database")
                if inspection.foreign_key_errors:
                    raise PlanningBackupError("foreign_key_check_failed", "database")
                if inspection.schema_version != self.database.schema_version():
                    raise PlanningBackupError("source_schema_changed", "database")
                database_sha256, database_size = _hash_file(snapshot_path)
                manifest = _manifest(
                    created_at=created_at,
                    schema_version=inspection.schema_version,
                    database_sha256=database_sha256,
                    database_size_bytes=database_size,
                    table_counts=inspection.table_counts,
                    application_version=self.application_version,
                    application_commit=self.application_commit,
                )
                zip_path = work_dir / "payload.zip"
                _write_package_zip(zip_path, snapshot_path, manifest)
                temporary_final = self.backup_dir / f".{package_name}.{secrets.token_hex(8)}.tmp"
                _encrypt_file(zip_path, temporary_final, self.encryption_key)
                _restrict_file(temporary_final)
                _fsync_file(temporary_final)
                os.replace(temporary_final, final_path)
                temporary_final = None
                _fsync_directory(self.backup_dir)
            retained_count = self._enforce_retention(new_package=package_name)
            self.state_store.update(
                last_backup_attempt_at=created_at,
                last_successful_backup_at=created_at,
                last_backup_status="success",
                last_backup_error_code=None,
            )
            return BackupResult(
                package_name=package_name,
                created_at=created_at,
                schema_version=inspection.schema_version,
                database_size_bytes=database_size,
                retained_count=retained_count,
            )
        except PlanningBackupError as exc:
            self._record_failure(created_at, exc.code)
            raise
        except (OSError, sqlite3.Error, zipfile.BadZipFile) as exc:
            self._record_failure(created_at, "backup_failed")
            raise PlanningBackupError("backup_failed", "backup") from exc
        finally:
            if temporary_final is not None:
                try:
                    temporary_final.unlink()
                except FileNotFoundError:
                    pass

    def list_backups(self) -> list[BackupListEntry]:
        _ensure_backup_directory(self.backup_dir)
        entries: list[BackupListEntry] = []
        for path in self.backup_dir.iterdir():
            parsed = _parse_package_name(path.name)
            if parsed is None or path.is_symlink() or not path.is_file():
                continue
            created_at, schema_version = parsed
            try:
                size = path.stat().st_size
            except OSError:
                continue
            entries.append(
                BackupListEntry(
                    package_name=path.name,
                    created_at=created_at,
                    schema_version=schema_version,
                    size_bytes=size,
                )
            )
        return sorted(entries, key=lambda entry: (entry.created_at, entry.package_name), reverse=True)

    def status(self) -> dict[str, Any]:
        state = self.state_store.load()
        return {
            "backup_enabled": True,
            "backup_directory_ready": self.backup_dir.is_dir(),
            "retention_count": self.retention_count,
            "state": state.to_dict(),
            "recognized_backup_count": len(self.list_backups()) if self.backup_dir.is_dir() else 0,
        }

    def _create_snapshot(self, snapshot_path: Path) -> None:
        target = sqlite3.connect(str(snapshot_path), isolation_level=None)
        try:
            target.execute("PRAGMA foreign_keys = ON")
            self.database.online_backup(target)
        finally:
            target.close()
        _restrict_file(snapshot_path)

    def _enforce_retention(self, *, new_package: str) -> int:
        entries = self.list_backups()
        keep = {new_package}
        for entry in entries:
            if entry.package_name == new_package:
                continue
            if len(keep) >= self.retention_count:
                break
            keep.add(entry.package_name)
        for entry in entries:
            if entry.package_name in keep:
                continue
            candidate = self.backup_dir / entry.package_name
            if candidate.is_symlink() or not candidate.is_file():
                continue
            try:
                candidate.unlink()
            except OSError:
                # A successful backup is still valid when cleanup is deferred.
                continue
        _fsync_directory(self.backup_dir)
        return len(self.list_backups())

    def _record_attempt(self, timestamp: str) -> None:
        try:
            self.state_store.update(
                last_backup_attempt_at=timestamp,
                last_backup_status="running",
                last_backup_error_code=None,
            )
        except OSError:
            pass

    def _record_failure(self, timestamp: str, code: str) -> None:
        try:
            self.state_store.update(
                last_backup_attempt_at=timestamp,
                last_backup_status="failed",
                last_backup_error_code=code,
            )
        except OSError:
            pass


class PlanningBackupVerifier:
    """Verify a package only in a temporary isolated directory."""

    def __init__(
        self,
        *,
        backup_dir: str | os.PathLike[str] = DEFAULT_BACKUP_DIR,
        encryption_key: str | bytes | None,
        environment: str = "development",
        now_fn: Callable[[], str] = utc_now,
        state_store: PlanningOperationsStateStore | None = None,
    ) -> None:
        self.backup_dir = _validate_backup_directory(backup_dir, environment=environment)
        self.encryption_key = parse_encryption_key(encryption_key)
        self.now_fn = now_fn
        self.state_store = state_store or PlanningOperationsStateStore(self.backup_dir)

    def verify(self, package: str | os.PathLike[str]) -> RestoreVerificationResult:
        package_path = _recognized_package_path(self.backup_dir, package)
        verification_at = self.now_fn()
        try:
            with tempfile.TemporaryDirectory(prefix=".planning-restore-", dir=self.backup_dir) as work_name:
                work_dir = Path(work_name)
                zip_path = work_dir / "payload.zip"
                _decrypt_file(package_path, zip_path, self.encryption_key)
                manifest, database_path = _extract_package(zip_path, work_dir)
                result = self._verify_isolated(package_path.name, manifest, database_path, verification_at)
            self._record_success(verification_at)
            return result
        except PlanningBackupVerificationError as exc:
            self._record_failure(verification_at, exc.code)
            raise
        except (OSError, sqlite3.Error, zipfile.BadZipFile, ValueError, KeyError) as exc:
            self._record_failure(verification_at, "verification_failed")
            raise PlanningBackupVerificationError("verification_failed", "restore") from exc

    def _verify_isolated(
        self,
        package_name: str,
        manifest: Mapping[str, Any],
        database_path: Path,
        verification_at: str,
    ) -> RestoreVerificationResult:
        _validate_manifest(manifest)
        source_schema_version = int(manifest["schema_version"])
        if source_schema_version > max((migration.version for migration in MIGRATIONS), default=0):
            raise PlanningBackupVerificationError("future_schema", "schema")
        digest, size = _hash_file(database_path)
        if digest != manifest["database_sha256"] or size != int(manifest["database_size_bytes"]):
            raise PlanningBackupVerificationError("hash_mismatch", "manifest")

        database = PlanningDatabase(database_path, auto_migrate=False)
        try:
            before = _inspect_database(database_path, require_core=False)
            if before.schema_version != source_schema_version:
                raise PlanningBackupVerificationError("schema_version_mismatch", "schema")
            _compare_table_counts(before.table_counts, manifest["table_counts"])
            migrated = source_schema_version < max((migration.version for migration in MIGRATIONS), default=0)
            if migrated:
                database.migrate()
            after = _inspect_database(database_path, require_core=True)
            if after.integrity_check != "ok":
                raise PlanningBackupVerificationError("integrity_check_failed", "database")
            if after.foreign_key_errors:
                raise PlanningBackupVerificationError("foreign_key_check_failed", "database")
            invalidated = _invalidate_restored_capabilities(database, verification_at)
            resumable_due_jobs = _semantic_checks(database, verification_at)
            return RestoreVerificationResult(
                package_name=package_name,
                manifest_version=int(manifest["manifest_version"]),
                source_schema_version=source_schema_version,
                verified_schema_version=after.schema_version,
                database_size_bytes=size,
                table_counts=dict(before.table_counts),
                migrated_in_isolation=migrated,
                invalidated_capabilities=invalidated,
                resumable_due_jobs=resumable_due_jobs,
            )
        finally:
            database.close()

    def _record_success(self, timestamp: str) -> None:
        try:
            self.state_store.update(
                last_restore_verification_at=timestamp,
                last_successful_restore_verification_at=timestamp,
                last_restore_verification_status="success",
                last_restore_verification_error_code=None,
            )
        except OSError:
            pass

    def _record_failure(self, timestamp: str, code: str) -> None:
        try:
            self.state_store.update(
                last_restore_verification_at=timestamp,
                last_restore_verification_status="failed",
                last_restore_verification_error_code=code,
            )
        except OSError:
            pass


def _manifest(
    *,
    created_at: str,
    schema_version: int,
    database_sha256: str,
    database_size_bytes: int,
    table_counts: Mapping[str, int],
    application_version: str,
    application_commit: str,
) -> dict[str, Any]:
    return {
        "manifest_version": BACKUP_MANIFEST_VERSION,
        "format": BACKUP_PACKAGE_FORMAT,
        "created_at": created_at,
        "schema_version": schema_version,
        "database_sha256": database_sha256,
        "database_size_bytes": database_size_bytes,
        "table_counts": {name: int(table_counts[name]) for name in _CORE_TABLES},
        "application_version": application_version,
        "application_commit": application_commit,
        "integrity_check": "ok",
        "foreign_key_check": "ok",
        "backup_method": "sqlite.connection.backup",
        "backup_method_version": 1,
        "wal_policy": "standalone_snapshot_does_not_rely_on_source_wal",
        "encrypted": True,
        "encryption_mode": "AES-256-GCM",
        "restore_capability_policy": "invalidate_on_isolated_restore_verification",
    }


def _validate_manifest(manifest: Mapping[str, Any]) -> None:
    required = {
        "manifest_version",
        "format",
        "created_at",
        "schema_version",
        "database_sha256",
        "database_size_bytes",
        "table_counts",
        "application_version",
        "application_commit",
        "integrity_check",
        "foreign_key_check",
        "backup_method",
        "backup_method_version",
        "wal_policy",
        "encrypted",
        "encryption_mode",
        "restore_capability_policy",
    }
    if set(manifest) != required:
        raise PlanningBackupVerificationError("manifest_fields_invalid", "manifest")
    if isinstance(manifest["manifest_version"], bool) or manifest["manifest_version"] != BACKUP_MANIFEST_VERSION:
        raise PlanningBackupVerificationError("manifest_version_unsupported", "manifest")
    if manifest["format"] != BACKUP_PACKAGE_FORMAT:
        raise PlanningBackupVerificationError("format_unsupported", "manifest")
    if manifest["encrypted"] is not True or manifest["encryption_mode"] != "AES-256-GCM":
        raise PlanningBackupVerificationError("encryption_metadata_invalid", "encryption")
    if manifest["backup_method"] != "sqlite.connection.backup":
        raise PlanningBackupVerificationError("backup_method_unsupported", "manifest")
    if manifest["wal_policy"] != "standalone_snapshot_does_not_rely_on_source_wal":
        raise PlanningBackupVerificationError("wal_policy_unsupported", "manifest")
    if manifest["integrity_check"] != "ok" or manifest["foreign_key_check"] != "ok":
        raise PlanningBackupVerificationError("manifest_integrity_invalid", "manifest")
    if isinstance(manifest["schema_version"], bool) or not isinstance(manifest["schema_version"], int) or manifest["schema_version"] < 1:
        raise PlanningBackupVerificationError("schema_version_invalid", "schema")
    if isinstance(manifest["database_size_bytes"], bool) or not isinstance(manifest["database_size_bytes"], int) or manifest["database_size_bytes"] < 1:
        raise PlanningBackupVerificationError("database_size_invalid", "manifest")
    if not isinstance(manifest["database_sha256"], str) or _SHA256_RE.fullmatch(manifest["database_sha256"]) is None:
        raise PlanningBackupVerificationError("database_hash_invalid", "manifest")
    counts = manifest["table_counts"]
    if not isinstance(counts, Mapping) or set(counts) != set(_CORE_TABLES):
        raise PlanningBackupVerificationError("table_counts_invalid", "manifest")
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in counts.values()):
        raise PlanningBackupVerificationError("table_counts_invalid", "manifest")
    for field_name in ("created_at", "application_version", "application_commit"):
        if not isinstance(manifest[field_name], str) or len(manifest[field_name]) > 256:
            raise PlanningBackupVerificationError("manifest_metadata_invalid", "manifest")


def _compare_table_counts(actual: Mapping[str, int], expected: object) -> None:
    if not isinstance(expected, Mapping):
        raise PlanningBackupVerificationError("table_counts_invalid", "manifest")
    for table_name in _CORE_TABLES:
        if table_name not in actual or int(actual[table_name]) != int(expected[table_name]):
            raise PlanningBackupVerificationError("table_counts_mismatch", f"table:{table_name}")


def _inspect_database(path: Path, *, require_core: bool) -> _DatabaseInspection:
    connection = sqlite3.connect(str(path))
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        schema_row = connection.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
        ).fetchone()
        schema_version = int(schema_row[0]) if schema_row else 0
        actual_tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        if require_core and set(_CORE_TABLES) - actual_tables:
            raise PlanningBackupVerificationError("core_tables_missing", "database")
        unknown_tables = actual_tables - set(_CORE_TABLES)
        if unknown_tables:
            raise PlanningBackupVerificationError("unexpected_table", "database")
        table_counts = {
            table_name: int(connection.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0])
            if table_name in actual_tables
            else 0
            for table_name in _CORE_TABLES
        }
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_key_errors = int(
            connection.execute("SELECT COUNT(*) FROM pragma_foreign_key_check").fetchone()[0]
        )
        return _DatabaseInspection(
            schema_version=schema_version,
            table_counts=table_counts,
            integrity_check=integrity,
            foreign_key_errors=foreign_key_errors,
        )
    except sqlite3.OperationalError as exc:
        raise PlanningBackupVerificationError("database_unreadable", "database") from exc
    finally:
        connection.close()


def _semantic_checks(database: PlanningDatabase, now: str) -> int:
    connection = database.connection
    impossible = connection.execute(
        """
        SELECT COUNT(*) FROM reminders
        WHERE (status = 'completed' AND completed_at IS NULL)
           OR (status = 'cancelled' AND cancelled_at IS NULL)
           OR (status = 'pending' AND delivery_state IN ('delivered', 'failed'))
           OR (delivery_state = 'retrying' AND next_attempt_at IS NULL)
        """
    ).fetchone()[0]
    if int(impossible) != 0:
        raise PlanningBackupVerificationError("impossible_reminder_state", "domain:reminder")
    orphan_outbox = connection.execute(
        """
        SELECT COUNT(*) FROM outbox AS o
        LEFT JOIN reminders AS r ON r.id = o.reminder_id
        WHERE o.reminder_id IS NOT NULL AND r.id IS NULL
        """
    ).fetchone()[0]
    if int(orphan_outbox) != 0:
        raise PlanningBackupVerificationError("orphan_outbox", "domain:outbox")
    duplicate_keys = connection.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT dedupe_key FROM outbox
            WHERE dedupe_key IS NOT NULL
            GROUP BY dedupe_key HAVING COUNT(*) > 1
        )
        """
    ).fetchone()[0]
    if int(duplicate_keys) != 0:
        raise PlanningBackupVerificationError("duplicate_outbox_key", "domain:outbox")
    relationship_error = connection.execute(
        """
        SELECT COUNT(*)
        FROM reminders AS r
        LEFT JOIN outbox AS o ON o.reminder_id = r.id
        WHERE r.deleted_at IS NULL
          AND r.status IN ('pending', 'due')
          AND r.delivery_state IN ('not_due', 'queued', 'retrying')
          AND o.id IS NULL
        """
    ).fetchone()[0]
    if int(relationship_error) != 0:
        raise PlanningBackupVerificationError("reminder_outbox_relationship", "domain:reminder")
    due_jobs = connection.execute(
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
        (now,),
    ).fetchone()[0]
    return int(due_jobs)


def _invalidate_restored_capabilities(database: PlanningDatabase, now: str) -> int:
    try:
        cursor = database.connection.execute(
            "UPDATE telegram_action_tokens SET consumed_at = COALESCE(consumed_at, ?) WHERE consumed_at IS NULL",
            (now,),
        )
        database.connection.commit()
        return int(cursor.rowcount)
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return 0
        raise PlanningBackupVerificationError("capability_invalidation_failed", "domain:telegram") from exc


def _write_package_zip(zip_path: Path, database_path: Path, manifest: Mapping[str, Any]) -> None:
    manifest_bytes = _canonical_json(manifest)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        archive.writestr("manifest.json", manifest_bytes)
        archive.write(database_path, "planning.sqlite3")


def _extract_package(zip_path: Path, work_dir: Path) -> tuple[Mapping[str, Any], Path]:
    with zipfile.ZipFile(zip_path, "r") as archive:
        member_names = archive.namelist()
        names = set(member_names)
        if (
            len(member_names) != len(names)
            or names != _ALLOWED_PACKAGE_MEMBERS
            or any("/" in name or "\\" in name for name in names)
        ):
            raise PlanningBackupVerificationError("package_members_invalid", "package")
        manifest_info = archive.getinfo("manifest.json")
        if manifest_info.file_size > BACKUP_MANIFEST_MAX_BYTES:
            raise PlanningBackupVerificationError("manifest_too_large", "manifest")
        try:
            manifest = json.loads(archive.read("manifest.json"))
        except (ValueError, TypeError, UnicodeDecodeError) as exc:
            raise PlanningBackupVerificationError("manifest_invalid_json", "manifest") from exc
        if not isinstance(manifest, Mapping):
            raise PlanningBackupVerificationError("manifest_invalid_json", "manifest")
        database_path = work_dir / "planning.sqlite3"
        with archive.open("planning.sqlite3", "r") as source, database_path.open("wb") as target:
            shutil.copyfileobj(source, target, length=BACKUP_CHUNK_BYTES)
    _restrict_file(database_path)
    return manifest, database_path


def _encrypt_file(source: Path, destination: Path, key: bytes) -> None:
    _require_crypto()
    nonce = secrets.token_bytes(BACKUP_NONCE_BYTES)
    header = BACKUP_MAGIC + bytes([BACKUP_CIPHER_VERSION]) + nonce
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(header)
    with source.open("rb") as input_file, destination.open("wb") as output_file:
        output_file.write(header)
        while True:
            chunk = input_file.read(BACKUP_CHUNK_BYTES)
            if not chunk:
                break
            output_file.write(encryptor.update(chunk))
        output_file.write(encryptor.finalize())
        output_file.write(encryptor.tag)


def _decrypt_file(source: Path, destination: Path, key: bytes) -> None:
    _require_crypto()
    minimum = len(BACKUP_MAGIC) + 1 + BACKUP_NONCE_BYTES + BACKUP_TAG_BYTES
    try:
        source_size = source.stat().st_size
    except OSError as exc:
        raise PlanningBackupVerificationError("package_unreadable", "package") from exc
    if source_size < minimum:
        raise PlanningBackupVerificationError("package_truncated", "encryption")
    decryptor = None
    temporary_path = destination
    try:
        with source.open("rb") as input_file, temporary_path.open("wb") as output_file:
            magic = input_file.read(len(BACKUP_MAGIC))
            version = input_file.read(1)
            nonce = input_file.read(BACKUP_NONCE_BYTES)
            if magic != BACKUP_MAGIC or version != bytes([BACKUP_CIPHER_VERSION]) or len(nonce) != BACKUP_NONCE_BYTES:
                raise PlanningBackupVerificationError("encryption_header_invalid", "encryption")
            header = magic + version + nonce
            decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).decryptor()
            decryptor.authenticate_additional_data(header)
            remaining = source_size - minimum
            while remaining:
                chunk = input_file.read(min(BACKUP_CHUNK_BYTES, remaining))
                if not chunk or len(chunk) > remaining:
                    raise PlanningBackupVerificationError("package_truncated", "encryption")
                remaining -= len(chunk)
                output_file.write(decryptor.update(chunk))
            tag = input_file.read(BACKUP_TAG_BYTES)
            try:
                output_file.write(decryptor.finalize_with_tag(tag))
            except InvalidTag as exc:
                raise PlanningBackupVerificationError("encryption_authentication_failed", "encryption") from exc
            output_file.flush()
            os.fsync(output_file.fileno())
    except PlanningBackupVerificationError:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
        raise


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(BACKUP_CHUNK_BYTES)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}", size


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _package_name(created_at: str, schema_version: int) -> str:
    parsed = datetime.fromisoformat(created_at[:-1] + "+00:00").astimezone(timezone.utc)
    timestamp = parsed.strftime("%Y%m%dT%H%M%SZ")
    return f"planning-{timestamp}-schema{schema_version}-{secrets.token_hex(6)}.sqlite3.a8"


def _parse_package_name(name: str) -> tuple[str, int] | None:
    match = _BACKUP_NAME_RE.fullmatch(name)
    if match is None:
        return None
    try:
        parsed = datetime.strptime(match.group("created"), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return parsed.isoformat(timespec="seconds").replace("+00:00", "Z"), int(match.group("schema"))


def _recognized_package_path(root: Path, value: str | os.PathLike[str]) -> Path:
    raw = Path(value)
    candidate = raw if raw.is_absolute() else root / raw
    resolved_root = root.resolve()
    try:
        resolved_candidate = candidate.resolve(strict=True)
    except OSError as exc:
        raise PlanningBackupVerificationError("backup_not_found", "path") from exc
    if candidate.is_symlink() or resolved_candidate.parent != resolved_root:
        raise PlanningBackupVerificationError("backup_path_outside_configured_directory", "path")
    if _parse_package_name(resolved_candidate.name) is None:
        raise PlanningBackupVerificationError("backup_path_not_recognized", "path")
    return resolved_candidate


def _validate_backup_directory(value: str | os.PathLike[str], *, environment: str) -> Path:
    raw = os.fspath(value)
    if not raw:
        raise PlanningBackupConfigurationError("backup_directory_empty", "configuration")
    path = Path(raw)
    if not path.is_absolute():
        raise PlanningBackupConfigurationError("backup_directory_must_be_absolute", "configuration")
    resolved = Path(os.path.realpath(raw))
    if str(environment).strip().lower() in {"production", "prod"}:
        if _is_ephemeral(resolved):
            raise PlanningBackupConfigurationError("backup_directory_ephemeral", "configuration")
    return resolved


def _is_ephemeral(path: Path) -> bool:
    value = str(path)
    return value in {"/tmp", "/private/tmp", "/var/tmp", "/dev/shm"} or any(
        value.startswith(prefix) for prefix in _EPHEMERAL_PREFIXES
    )


def _ensure_backup_directory(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o700)
    except OSError as exc:
        raise PlanningBackupError("backup_directory_unavailable", "configuration") from exc


def _restrict_file(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _fsync_file(path: Path) -> None:
    try:
        with path.open("rb") as handle:
            os.fsync(handle.fileno())
    except OSError:
        pass


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _bounded_metadata(value: str | None) -> str:
    if not isinstance(value, str) or not value.strip():
        return "unknown"
    return value.strip()[:256]


def _require_crypto() -> None:
    if Cipher is None or algorithms is None or modes is None:
        raise PlanningBackupConfigurationError("encryption_runtime_dependency_missing", "encryption")


def _cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.planning.backup")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("backup", help="create one encrypted online Planning backup")
    verify = subparsers.add_parser("verify", help="verify one recognized backup in isolation")
    verify.add_argument("package", help="recognized backup filename or path under configured backup directory")
    subparsers.add_parser("list", help="list recognized Planning backup artifacts")
    subparsers.add_parser("status", help="show bounded Planning backup state")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _cli_parser().parse_args(list(argv) if argv is not None else None)
    backup_dir = os.getenv("PLANNING_BACKUP_DIR", DEFAULT_BACKUP_DIR).strip() or DEFAULT_BACKUP_DIR
    environment = os.getenv("PLANNING_ENV", os.getenv("APP_ENV", "development"))
    try:
        if args.command == "list":
            service = _unbound_service(backup_dir, environment)
            print(json.dumps({"ok": True, "backups": [item.to_dict() for item in service.list_backups()]}, sort_keys=True))
            return 0
        if args.command == "status":
            service = _unbound_service(backup_dir, environment)
            print(json.dumps(service.status(), sort_keys=True))
            return 0
        key = os.getenv("PLANNING_BACKUP_ENCRYPTION_KEY", "")
        db_path = os.getenv("PLANNING_DB_PATH", "/app/data/planning.sqlite3")
        if args.command == "backup":
            database = PlanningDatabase(
                config=PlanningDatabaseConfig(path=db_path, environment=environment)
            )
            try:
                service = PlanningBackupService(
                    database,
                    backup_dir=backup_dir,
                    encryption_key=key,
                    retention_count=int(os.getenv("PLANNING_BACKUP_RETENTION_COUNT", str(DEFAULT_BACKUP_RETENTION_COUNT))),
                    application_version=os.getenv("APP_VERSION", "unknown"),
                    application_commit=os.getenv("APP_COMMIT", "unknown"),
                    environment=environment,
                )
                print(json.dumps(service.backup().to_dict(), sort_keys=True))
            finally:
                database.close()
            return 0
        if args.command == "verify":
            verifier = PlanningBackupVerifier(backup_dir=backup_dir, encryption_key=key, environment=environment)
            print(json.dumps(verifier.verify(args.package).to_dict(), sort_keys=True))
            return 0
    except PlanningBackupError as exc:
        print(json.dumps({"ok": False, "error": {"code": exc.code, "category": exc.category}}, sort_keys=True), file=sys.stderr)
        return 2
    except (OSError, sqlite3.Error, ValueError) as exc:
        del exc
        print(json.dumps({"ok": False, "error": {"code": "operator_command_failed", "category": "backup"}}, sort_keys=True), file=sys.stderr)
        return 2
    return 2


def _unbound_service(backup_dir: str, environment: str) -> PlanningBackupService:
    # List/status do not need a database or key; the small object is only used
    # for its configured-root and state/retention views.
    service = object.__new__(PlanningBackupService)
    service.backup_dir = _validate_backup_directory(backup_dir, environment=environment)
    retention_count = int(
        os.getenv("PLANNING_BACKUP_RETENTION_COUNT", str(DEFAULT_BACKUP_RETENTION_COUNT))
    )
    if not 1 <= retention_count <= 365:
        raise PlanningBackupConfigurationError("invalid_retention_count", "configuration")
    service.retention_count = retention_count
    service.state_store = PlanningOperationsStateStore(service.backup_dir)
    return service


if __name__ == "__main__":  # pragma: no cover - covered through subprocess CLI tests.
    raise SystemExit(main())
