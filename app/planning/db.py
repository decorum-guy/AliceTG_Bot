from __future__ import annotations

import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Mapping

from app.planning.errors import PlanningConfigurationError


DEFAULT_PLANNING_DB_PATH = "/app/data/planning.sqlite3"
DEFAULT_BUSY_TIMEOUT_MS = 5_000
_PRODUCTION_NAMES = frozenset({"production", "prod"})
_EPHEMERAL_PREFIXES = (
    "/tmp/",
    "/private/tmp/",
    "/var/tmp/",
    "/private/var/folders/",
    "/dev/shm/",
)


@dataclass(frozen=True)
class PlanningDatabaseConfig:
    path: str = DEFAULT_PLANNING_DB_PATH
    environment: str = "development"
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS

    def validate(self) -> "PlanningDatabaseConfig":
        if not self.path:
            raise PlanningConfigurationError("PLANNING_DB_PATH must not be empty")
        if self.busy_timeout_ms < 0 or self.busy_timeout_ms > 120_000:
            raise PlanningConfigurationError("planning SQLite busy timeout is out of range")
        if self.is_production:
            if self.path == ":memory:":
                raise PlanningConfigurationError("production Planning storage cannot use :memory:")
            path = os.path.abspath(self.path)
            if not os.path.isabs(self.path):
                raise PlanningConfigurationError("production Planning storage path must be absolute")
            if any(path == prefix[:-1] or path.startswith(prefix) for prefix in _EPHEMERAL_PREFIXES):
                raise PlanningConfigurationError(
                    "production Planning storage cannot use an obviously ephemeral path"
                )
        return self

    @property
    def is_production(self) -> bool:
        return self.environment.strip().lower() in _PRODUCTION_NAMES

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "PlanningDatabaseConfig":
        values = os.environ if environ is None else environ
        path = values.get("PLANNING_DB_PATH", DEFAULT_PLANNING_DB_PATH).strip() or DEFAULT_PLANNING_DB_PATH
        environment = (
            values.get("PLANNING_ENV")
            or values.get("APP_ENV")
            or values.get("ENVIRONMENT")
            or "development"
        ).strip()
        timeout = int(values.get("PLANNING_DB_BUSY_TIMEOUT_MS", str(DEFAULT_BUSY_TIMEOUT_MS)))
        return cls(path=path, environment=environment, busy_timeout_ms=timeout).validate()


class PlanningDatabase:
    """One explicitly configured SQLite connection for the Planning store."""

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        config: PlanningDatabaseConfig | None = None,
        auto_migrate: bool = True,
    ) -> None:
        if config is not None and path is not None:
            raise PlanningConfigurationError("provide either path or config, not both")
        selected_config = config
        if selected_config is None and path is not None:
            selected_config = PlanningDatabaseConfig(path=os.fspath(path))
        if selected_config is None:
            selected_config = PlanningDatabaseConfig.from_env()
        self.config = selected_config.validate()
        self.path = self.config.path
        self._lock = threading.RLock()
        self._transaction_depth = 0
        self._savepoint_counter = 0
        self._connection = self._open_connection()
        if auto_migrate:
            self.migrate()

    @classmethod
    def from_env(cls, *, auto_migrate: bool = True) -> "PlanningDatabase":
        return cls(config=PlanningDatabaseConfig.from_env(), auto_migrate=auto_migrate)

    def _open_connection(self) -> sqlite3.Connection:
        if self.path != ":memory:":
            database_path = Path(self.path)
            database_path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(
                str(database_path),
                timeout=self.config.busy_timeout_ms / 1000,
                isolation_level=None,
                check_same_thread=False,
            )
        else:
            connection = sqlite3.connect(
                ":memory:",
                timeout=self.config.busy_timeout_ms / 1000,
                isolation_level=None,
                check_same_thread=False,
            )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(f"PRAGMA busy_timeout = {self.config.busy_timeout_ms}")
            journal_mode = self._enable_wal(connection)
            if self.path != ":memory:" and journal_mode != "wal":
                raise PlanningConfigurationError("Planning SQLite database did not enable WAL mode")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.execute("PRAGMA temp_store = MEMORY")
            if self.path != ":memory:":
                try:
                    Path(self.path).chmod(0o600)
                except OSError:
                    # Permission hardening is best effort on filesystems that do not expose chmod.
                    pass
            return connection
        except BaseException:
            try:
                connection.close()
            except sqlite3.Error:
                pass
            raise

    @staticmethod
    def _enable_wal(connection: sqlite3.Connection) -> str:
        """Enable WAL while tolerating a concurrent first opener/migrator."""

        deadline = time.monotonic() + 5.0
        while True:
            try:
                return str(connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]).lower()
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)

    @property
    def connection(self) -> sqlite3.Connection:
        return self._connection

    def online_backup(
        self,
        destination: sqlite3.Connection,
        *,
        pages: int = 1_000,
        sleep: float = 0.01,
    ) -> None:
        """Copy a consistent snapshot using SQLite's online backup API.

        The destination is owned by the caller.  The database lock only
        serialises this connection with mutations made through this
        ``PlanningDatabase`` instance; SQLite's backup API also coordinates
        with writers in other processes through the database's WAL protocol.
        """

        if pages <= 0:
            raise ValueError("online backup pages must be positive")
        if sleep < 0:
            raise ValueError("online backup sleep must not be negative")
        with self._lock:
            self._connection.backup(destination, pages=pages, sleep=sleep)

    @property
    def in_transaction(self) -> bool:
        return self._transaction_depth > 0

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run a mutation in an explicit transaction, with savepoints for nesting."""

        with self._lock:
            if self._transaction_depth == 0:
                self._connection.execute("BEGIN IMMEDIATE")
                self._transaction_depth = 1
                try:
                    yield self._connection
                except BaseException:
                    self._connection.rollback()
                    self._transaction_depth = 0
                    raise
                else:
                    self._connection.commit()
                    self._transaction_depth = 0
                return

            self._savepoint_counter += 1
            savepoint = f"planning_sp_{self._savepoint_counter}"
            self._connection.execute(f"SAVEPOINT {savepoint}")
            self._transaction_depth += 1
            try:
                yield self._connection
            except BaseException:
                self._connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self._connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                self._transaction_depth -= 1
                raise
            else:
                self._connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                self._transaction_depth -= 1

    def migrate(self) -> int:
        from app.planning.migrations import MigrationRunner

        return MigrationRunner(self).apply()

    def integrity_check(self) -> str:
        result = self._connection.execute("PRAGMA integrity_check").fetchone()
        return str(result[0])

    def schema_version(self) -> int:
        try:
            result = self._connection.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return 0
            raise
        return int(result[0])

    def close(self) -> None:
        with self._lock:
            if self._transaction_depth:
                self._connection.rollback()
                self._transaction_depth = 0
            self._connection.close()

    def __enter__(self) -> "PlanningDatabase":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()
