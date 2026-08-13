#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sqlite3
from pathlib import Path


def backup_database(source_path: Path, destination_path: Path) -> None:
    source_path = source_path.resolve()
    destination_path = destination_path.resolve()
    temporary_path = destination_path.with_name(
        f".{destination_path.name}.backup-{os.getpid()}"
    )

    if not source_path.is_file():
        raise FileNotFoundError(f"Missing SQLite database: {source_path}")

    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path.unlink(missing_ok=True)
    source = sqlite3.connect(f"{source_path.as_uri()}?mode=ro", uri=True)
    destination = sqlite3.connect(temporary_path)
    try:
        source.backup(destination)
        result = destination.execute("PRAGMA integrity_check").fetchone()
        if result != ("ok",):
            raise RuntimeError(f"SQLite integrity check failed: {result}")
    finally:
        destination.close()
        source.close()

    os.chmod(temporary_path, source_path.stat().st_mode & 0o777)
    os.replace(temporary_path, destination_path)
    for suffix in ("-wal", "-shm", "-journal"):
        Path(f"{destination_path}{suffix}").unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create an integrity-checked, self-contained SQLite backup."
    )
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()

    backup_database(args.source, args.destination)
    print("sqlite_backup: ok")


if __name__ == "__main__":
    main()
