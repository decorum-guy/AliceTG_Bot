from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence, TYPE_CHECKING

from app.planning.errors import PlanningMigrationError, PlanningNewerSchemaError

if TYPE_CHECKING:
    from app.planning.db import PlanningDatabase


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


def _load_migrations() -> tuple[Migration, ...]:
    migrations_dir = Path(__file__).with_name("sql")
    files = sorted(migrations_dir.glob("[0-9][0-9][0-9]_*.sql"))
    migrations: list[Migration] = []
    for path in files:
        prefix, _, stem = path.stem.partition("_")
        version = int(prefix)
        migrations.append(Migration(version=version, name=stem, sql=path.read_text(encoding="utf-8")))
    return tuple(migrations)


MIGRATIONS = _load_migrations()


def _statements(sql: str) -> Iterable[str]:
    """Split the small SQL migration without using executescript's implicit commit."""

    buffer = ""
    for line in sql.splitlines():
        stripped = line.strip()
        if not stripped and not buffer:
            continue
        if stripped.startswith("--") and not buffer:
            continue
        buffer += line + "\n"
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            if statement:
                yield statement
            buffer = ""
    if buffer.strip():
        raise PlanningMigrationError("migration contains an incomplete SQL statement")


class MigrationRunner:
    def __init__(self, database: "PlanningDatabase", migrations: Sequence[Migration] = MIGRATIONS) -> None:
        self.database = database
        ordered = tuple(sorted(migrations, key=lambda item: item.version))
        versions = [item.version for item in ordered]
        if len(set(versions)) != len(versions) or any(version <= 0 for version in versions):
            raise PlanningMigrationError("migration versions must be unique positive integers")
        self.migrations = ordered

    @property
    def latest_supported_version(self) -> int:
        return self.migrations[-1].version if self.migrations else 0

    def apply(self) -> int:
        with self.database.transaction() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY CHECK (version > 0),
                    name TEXT NOT NULL,
                    applied_at TEXT NOT NULL
                )
                """
            )
            rows = connection.execute("SELECT version, name FROM schema_migrations ORDER BY version").fetchall()
            applied = {int(row[0]): str(row[1]) for row in rows}
            if applied and max(applied) > self.latest_supported_version:
                raise PlanningNewerSchemaError(
                    f"database schema {max(applied)} is newer than supported "
                    f"schema {self.latest_supported_version}"
                )

            known = {migration.version: migration for migration in self.migrations}
            for version, name in applied.items():
                migration = known.get(version)
                if migration is None:
                    raise PlanningMigrationError(f"database contains unknown migration {version}")
                if migration.name != name:
                    raise PlanningMigrationError(
                        f"migration {version} name mismatch: database={name!r}, application={migration.name!r}"
                    )

            applied_versions = sorted(applied)
            if applied_versions and applied_versions != list(range(1, applied_versions[-1] + 1)):
                raise PlanningMigrationError("migration history has a gap")
            for migration in self.migrations:
                if migration.version in applied:
                    continue
                if applied_versions and migration.version != applied_versions[-1] + 1:
                    raise PlanningMigrationError("migration history has a gap")
                try:
                    for statement in _statements(migration.sql):
                        connection.execute(statement)
                    connection.execute(
                        "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
                        (migration.version, migration.name, _migration_timestamp()),
                    )
                except sqlite3.Error as exc:
                    raise PlanningMigrationError(
                        f"migration {migration.version} ({migration.name}) failed"
                    ) from exc
                applied_versions.append(migration.version)
            return max(applied_versions, default=0)


def _migration_timestamp() -> str:
    from app.planning.models import utc_now

    return utc_now()
