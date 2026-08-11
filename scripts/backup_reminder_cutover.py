#!/usr/bin/env python3
"""Create a recoverable, timestamped A2 reminder cutover backup.

The source directory and destination root are explicit to make accidental
production writes unlikely. This script never deletes an existing backup.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


TIMESTAMP_PATTERN = re.compile(r"^\d{8}T\d{6}Z$")
ARTIFACT_NAMES = ("reminders.json", "state.json", "planning.sqlite3")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _copy_sqlite(source: Path, destination: Path) -> str:
    with sqlite3.connect(source) as source_connection, sqlite3.connect(destination) as destination_connection:
        source_connection.backup(destination_connection)
        integrity = str(destination_connection.execute("PRAGMA integrity_check").fetchone()[0])
    if integrity != "ok":
        raise RuntimeError("Planning database backup failed integrity_check")
    return integrity


def create_backup(source_dir: Path, destination_root: Path, timestamp: str | None = None) -> Path:
    source_dir = source_dir.resolve()
    destination_root = destination_root.resolve()
    if not source_dir.is_dir():
        raise RuntimeError("source directory does not exist")
    if not (source_dir / "reminders.json").is_file():
        raise RuntimeError("reminders.json is required for a reminder cutover backup")
    if destination_root == source_dir or source_dir in destination_root.parents:
        raise RuntimeError("backup destination must not be inside the source directory")
    backup_timestamp = timestamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if TIMESTAMP_PATTERN.fullmatch(backup_timestamp) is None:
        raise RuntimeError("timestamp must use YYYYMMDDTHHMMSSZ")
    destination = destination_root / f"alice-reminder-cutover-{backup_timestamp}"
    if destination.exists():
        raise RuntimeError("timestamped backup destination already exists")
    destination.mkdir(parents=True, mode=0o700)

    artifacts: list[dict[str, Any]] = []
    for name in ARTIFACT_NAMES:
        source = source_dir / name
        if not source.exists():
            continue
        target = destination / name
        if name == "planning.sqlite3":
            integrity = _copy_sqlite(source, target)
        else:
            shutil.copy2(source, target)
            integrity = None
        os.chmod(target, 0o600)
        artifacts.append(
            {
                "name": name,
                "size_bytes": target.stat().st_size,
                "sha256": _sha256(target),
                "sqlite_integrity": integrity,
            }
        )
    manifest = {
        "kind": "alice_reminder_cutover_backup",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "artifacts": artifacts,
        "deletion_policy": "no automatic backup deletion",
    }
    manifest_path = destination / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(manifest_path, 0o600)
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description="Back up A2 reminder cutover state")
    parser.add_argument("--source-dir", required=True, type=Path)
    parser.add_argument("--destination-root", required=True, type=Path)
    parser.add_argument("--timestamp")
    args = parser.parse_args()
    destination = create_backup(args.source_dir, args.destination_root, args.timestamp)
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
