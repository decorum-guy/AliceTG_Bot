"""Small, content-free operational state persisted beside Planning backups.

This file is deliberately not part of the Planning database.  It records only
the timestamps and bounded result codes needed to distinguish a fresh backup
from an overdue or failed one after a process restart.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping


OPERATIONS_STATE_VERSION = 1
OPERATIONS_STATE_FILENAME = ".planning-operations-state.json"


@dataclass(frozen=True)
class PlanningOperationsState:
    last_backup_attempt_at: str | None = None
    last_successful_backup_at: str | None = None
    last_backup_status: str = "unknown"
    last_backup_error_code: str | None = None
    last_restore_verification_at: str | None = None
    last_successful_restore_verification_at: str | None = None
    last_restore_verification_status: str = "unknown"
    last_restore_verification_error_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": OPERATIONS_STATE_VERSION,
            "last_backup_attempt_at": self.last_backup_attempt_at,
            "last_successful_backup_at": self.last_successful_backup_at,
            "last_backup_status": self.last_backup_status,
            "last_backup_error_code": self.last_backup_error_code,
            "last_restore_verification_at": self.last_restore_verification_at,
            "last_successful_restore_verification_at": self.last_successful_restore_verification_at,
            "last_restore_verification_status": self.last_restore_verification_status,
            "last_restore_verification_error_code": self.last_restore_verification_error_code,
        }


class PlanningOperationsStateStore:
    """Atomic, bounded sidecar state for the Planning operations services."""

    def __init__(self, directory: str | os.PathLike[str]) -> None:
        self.directory = Path(directory)
        self.path = self.directory / OPERATIONS_STATE_FILENAME
        self._lock = threading.RLock()

    def load(self) -> PlanningOperationsState:
        with self._lock:
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return PlanningOperationsState()
            except (OSError, ValueError, TypeError):
                return PlanningOperationsState(last_backup_status="failed", last_backup_error_code="state_unreadable")
            return self._from_mapping(payload)

    def update(self, **changes: Any) -> PlanningOperationsState:
        with self._lock:
            current = self.load()
            allowed = set(current.to_dict()) - {"version"}
            unknown = set(changes) - allowed
            if unknown:
                raise ValueError("unknown Planning operations state field")
            next_state = replace(current, **changes)
            self.directory.mkdir(parents=True, exist_ok=True)
            try:
                self.directory.chmod(0o700)
            except OSError:
                pass
            fd, temporary_name = tempfile.mkstemp(
                prefix=".planning-operations-state-",
                suffix=".tmp",
                dir=self.directory,
                text=True,
            )
            temporary_path = Path(temporary_name)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(next_state.to_dict(), handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary_path, self.path)
                _fsync_directory(self.directory)
            finally:
                try:
                    temporary_path.unlink()
                except FileNotFoundError:
                    pass
            return next_state

    @staticmethod
    def _from_mapping(payload: object) -> PlanningOperationsState:
        if not isinstance(payload, Mapping) or payload.get("version") != OPERATIONS_STATE_VERSION:
            return PlanningOperationsState(last_backup_status="failed", last_backup_error_code="state_invalid")
        values: dict[str, Any] = {}
        for field_name in PlanningOperationsState.__dataclass_fields__:
            value = payload.get(field_name)
            if value is not None and not isinstance(value, str):
                return PlanningOperationsState(last_backup_status="failed", last_backup_error_code="state_invalid")
            values[field_name] = value
        for field_name in ("last_backup_status", "last_restore_verification_status"):
            if values[field_name] is None:
                values[field_name] = "unknown"
        return PlanningOperationsState(**values)


def _fsync_directory(directory: Path) -> None:
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
