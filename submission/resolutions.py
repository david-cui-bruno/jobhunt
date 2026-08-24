from __future__ import annotations

import re
import sqlite3
import time
from typing import Protocol


class ResolutionLike(Protocol):
    source_url: str
    resolved_url: str | None
    resolver: str
    source_hash: str
    error: str


SCHEMA = """
CREATE TABLE IF NOT EXISTS posting_url_resolutions (
    posting_id TEXT PRIMARY KEY,
    source_url TEXT NOT NULL,
    resolved_url TEXT,
    resolver TEXT NOT NULL,
    resolved_at INTEGER,
    last_error TEXT NOT NULL DEFAULT '',
    source_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS posting_url_resolutions_target_idx
ON posting_url_resolutions(resolved_url);
"""


def ensure_resolution_schema(conn: sqlite3.Connection) -> None:
    should_commit = not conn.in_transaction
    for statement in [part.strip() for part in SCHEMA.split(";") if part.strip()]:
        conn.execute(statement)
    if should_commit:
        conn.commit()


def cached_resolution(conn: sqlite3.Connection, posting_id: str, source_url: str) -> str | None:
    row = conn.execute(
        "SELECT resolved_url FROM posting_url_resolutions "
        "WHERE posting_id=? AND source_url=? AND COALESCE(resolved_url, '')<>''",
        (posting_id, source_url),
    ).fetchone()
    return row[0] if row else None


def record_resolution(conn: sqlite3.Connection, result: ResolutionLike, posting_id: str | None = None) -> None:
    resolved_posting_id = posting_id or getattr(result, "posting_id", None)
    if not resolved_posting_id:
        raise ValueError("posting_id is required")
    conn.execute(
        """
        INSERT INTO posting_url_resolutions
        (posting_id, source_url, resolved_url, resolver, resolved_at, last_error, source_hash)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(posting_id) DO UPDATE SET
            source_url=excluded.source_url,
            resolved_url=excluded.resolved_url,
            resolver=excluded.resolver,
            resolved_at=excluded.resolved_at,
            last_error=excluded.last_error,
            source_hash=excluded.source_hash
        """,
        (
            resolved_posting_id,
            result.source_url,
            result.resolved_url,
            result.resolver,
            int(time.time()) if result.resolved_url else None,
            _sanitize_error(result.error),
            result.source_hash,
        ),
    )


def _sanitize_error(error: str) -> str:
    return re.sub(r"\s+", " ", error).strip()[:500]
