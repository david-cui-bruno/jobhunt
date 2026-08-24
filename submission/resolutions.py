from __future__ import annotations

import re
import sqlite3
import time
from typing import Iterable, Protocol


NEGATIVE_CACHE_BACKOFF_SECONDS = 6 * 60 * 60


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
    source_hash TEXT NOT NULL,
    retry_after INTEGER
);
CREATE INDEX IF NOT EXISTS posting_url_resolutions_target_idx
ON posting_url_resolutions(resolved_url);
"""


def ensure_resolution_schema(conn: sqlite3.Connection) -> None:
    should_commit = not conn.in_transaction
    for statement in [part.strip() for part in SCHEMA.split(";") if part.strip()]:
        conn.execute(statement)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(posting_url_resolutions)")}
    if "retry_after" not in columns:
        conn.execute("ALTER TABLE posting_url_resolutions ADD COLUMN retry_after INTEGER")
    if should_commit:
        conn.commit()


def cached_resolution(conn: sqlite3.Connection, posting_id: str, source_url: str) -> str | None:
    row = conn.execute(
        "SELECT resolved_url FROM posting_url_resolutions "
        "WHERE posting_id=? AND source_url=? AND COALESCE(resolved_url, '')<>''",
        (posting_id, source_url),
    ).fetchone()
    return row[0] if row else None


def cached_resolution_backoff_active(
    conn: sqlite3.Connection,
    posting_id: str,
    source_url: str,
    now: int | None = None,
) -> str | None:
    row = conn.execute(
        "SELECT last_error FROM posting_url_resolutions "
        "WHERE posting_id=? AND source_url=? AND COALESCE(resolved_url, '')='' "
        "AND COALESCE(retry_after, 0)>?",
        (posting_id, source_url, int(time.time()) if now is None else now),
    ).fetchone()
    return row[0] if row else None


def record_resolution(conn: sqlite3.Connection, result: ResolutionLike, posting_id: str | None = None) -> None:
    resolved_posting_id = posting_id or getattr(result, "posting_id", None)
    if not resolved_posting_id:
        raise ValueError("posting_id is required")
    conn.execute(
        """
        INSERT INTO posting_url_resolutions
        (posting_id, source_url, resolved_url, resolver, resolved_at, last_error, source_hash, retry_after)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(posting_id) DO UPDATE SET
            source_url=excluded.source_url,
            resolved_url=excluded.resolved_url,
            resolver=excluded.resolver,
            resolved_at=excluded.resolved_at,
            last_error=excluded.last_error,
            source_hash=excluded.source_hash,
            retry_after=excluded.retry_after
        """,
        (
            resolved_posting_id,
            result.source_url,
            result.resolved_url,
            result.resolver,
            int(time.time()) if result.resolved_url else None,
            _sanitize_error(result.error),
            result.source_hash,
            None if result.resolved_url else int(time.time()) + NEGATIVE_CACHE_BACKOFF_SECONDS,
        ),
    )


def _sanitize_error(error: str) -> str:
    return re.sub(r"\s+", " ", error).strip()[:500]


TECHNICAL_MANUAL_PREFIXES = (
    "no adapter for other",
    "no adapter for icims",
)
TERMINAL_OUTCOMES = frozenset({"stale", "submitted", "deduplicated"})
TERMINAL_STATUSES = frozenset({"submitted", "skipped"})


def retriage_candidates(conn: sqlite3.Connection, posting_ids: Iterable[str] | None = None) -> list[dict]:
    from submission.identity import canonical_conflict_reason
    from submission.lanes import classify_url, preparation_destination

    candidates: list[dict] = []
    requested_ids = list(dict.fromkeys(posting_ids or []))
    query, params = _candidate_query(requested_ids if posting_ids is not None else None)
    for row in conn.execute(query, params):
        posting_id = row["posting_id"] if isinstance(row, sqlite3.Row) else row[0]
        company = row["company"] if isinstance(row, sqlite3.Row) else row[1]
        title = row["title"] if isinstance(row, sqlite3.Row) else row[2]
        source_url = row["source_url"] if isinstance(row, sqlite3.Row) else row[3]
        resolved_url = row["resolved_url"] if isinstance(row, sqlite3.Row) else row[4]
        reason = row["last_error"] if isinstance(row, sqlite3.Row) else row[5]
        if not resolved_url or not _technical_reason(reason):
            continue
        if "click" in reason.lower():
            continue
        if _has_finished_attempt(conn, posting_id):
            continue
        if canonical_conflict_reason(conn, posting_id, resolved_url):
            continue
        destination, destination_reason = preparation_destination(conn, resolved_url)
        ats, lane = classify_url(resolved_url)
        if lane.name == "unsupported":
            continue
        candidates.append({
            "posting_id": posting_id,
            "company": company or "",
            "title": title or "",
            "source_url": source_url or "",
            "resolved_url": resolved_url,
            "ats": ats,
            "destination": destination,
            "reason": reason,
            "last_error": reason,
            "next_error": destination_reason or "",
        })
    candidates.sort(key=lambda item: item["posting_id"])
    return candidates


def apply_retriage(conn: sqlite3.Connection, posting_ids: list[str]) -> dict:
    requested_ids = list(dict.fromkeys(posting_ids))
    requested = len(requested_ids)
    if not requested_ids:
        return {"requested": 0, "updated": 0, "skipped": 0}
    try:
        conn.execute("BEGIN IMMEDIATE")
        current = {
            candidate["posting_id"]: candidate
            for candidate in retriage_candidates(conn, requested_ids)
        }
        updated = 0
        for posting_id in requested_ids:
            candidate = current.get(posting_id)
            if candidate is None:
                continue
            changed = conn.execute(
                "UPDATE postings SET status=?, outcome=?, last_error=?, last_attempt_at=? "
                "WHERE posting_id=? AND status='manual' AND COALESCE(last_error, '')=?",
                (
                    "queued" if candidate["destination"] == "ready" else "manual",
                    None if candidate["destination"] == "ready" else "manual",
                    "" if candidate["destination"] == "ready" else candidate["next_error"],
                    int(time.time()),
                    posting_id,
                    candidate["last_error"],
                ),
            ).rowcount
            updated += changed
        conn.commit()
        return {"requested": requested, "updated": updated, "skipped": requested - updated}
    except Exception:
        conn.rollback()
        raise


def _candidate_query(posting_ids: list[str] | None = None) -> tuple[str, tuple]:
    id_clause = ""
    params: tuple = ()
    if posting_ids is not None:
        if not posting_ids:
            return "SELECT NULL WHERE 0", ()
        placeholders = ",".join("?" for _ in posting_ids)
        id_clause = f" AND p.posting_id IN ({placeholders})"
        params = tuple(posting_ids)
    return """
        SELECT p.posting_id,
               COALESCE(p.company, '') AS company,
               COALESCE(p.title, '') AS title,
               r.source_url,
               r.resolved_url,
               COALESCE(p.last_error, '') AS last_error
        FROM postings p
        JOIN posting_url_resolutions r USING(posting_id)
        WHERE p.status='manual'
          AND COALESCE(r.resolved_url, '')<>''
          AND COALESCE(p.outcome, '') NOT IN ('stale', 'submitted', 'deduplicated')
          AND COALESCE(p.status, '') NOT IN ('submitted', 'skipped')
          {id_clause}
        ORDER BY p.posting_id
    """.format(id_clause=id_clause), params


def _technical_reason(reason: str) -> bool:
    return any(reason.startswith(prefix) for prefix in TECHNICAL_MANUAL_PREFIXES)


def _has_finished_attempt(conn: sqlite3.Connection, posting_id: str) -> bool:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='submission_attempts'"
    ).fetchone()
    if not exists:
        return False
    return conn.execute(
        "SELECT 1 FROM submission_attempts WHERE posting_id=? AND finished_at IS NOT NULL LIMIT 1",
        (posting_id,),
    ).fetchone() is not None
