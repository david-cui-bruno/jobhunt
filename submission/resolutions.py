from __future__ import annotations

import re
import sqlite3
import time
from typing import Iterable, Protocol

from compensation.normalize import requested_period
from compensation.research import _context_from_row, _has_compensation_marker, compensation_marker_sql_predicate
from compensation.schema import load_resolution


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
TECHNICAL_CURRENT_URL_REASONS = frozenset({"no adapter for other", "no adapter for oraclecloud"})
TERMINAL_OUTCOMES = frozenset({"stale", "submitted", "deduplicated"})
TERMINAL_STATUSES = frozenset({"submitted", "skipped"})


def retriage_candidates(
    conn: sqlite3.Connection,
    posting_ids: Iterable[str] | None = None,
    ats: str | None = None,
) -> list[dict]:
    from submission.identity import canonical_conflict_reason
    from submission.lanes import classify_url, preparation_destination
    from watcher.filter import title_ok

    candidates: list[dict] = []
    requested_ids = list(dict.fromkeys(posting_ids or []))
    query, params = _candidate_query(conn, requested_ids if posting_ids is not None else None)
    for row in conn.execute(query, params):
        posting_id = row["posting_id"] if isinstance(row, sqlite3.Row) else row[0]
        company = row["company"] if isinstance(row, sqlite3.Row) else row[1]
        title = row["title"] if isinstance(row, sqlite3.Row) else row[2]
        source = row["source"] if isinstance(row, sqlite3.Row) else row[3]
        source_url = row["source_url"] if isinstance(row, sqlite3.Row) else row[4]
        resolved_url = row["resolved_url"] if isinstance(row, sqlite3.Row) else row[5]
        reason = row["last_error"] if isinstance(row, sqlite3.Row) else row[6]
        has_resolution = bool(row["has_resolution"] if isinstance(row, sqlite3.Row) else row[7])
        if not resolved_url or not _technical_reason(reason, allow_current_url=not has_resolution):
            continue
        if source and not title_ok(title, source=source):
            continue
        if "click" in reason.lower():
            continue
        if _has_finished_attempt(conn, posting_id):
            continue
        if canonical_conflict_reason(conn, posting_id, resolved_url):
            continue
        ats_name, lane = classify_url(resolved_url)
        if ats is not None and ats_name != ats:
            continue
        if lane.name == "unsupported":
            continue
        if not has_resolution and not lane.automatic:
            continue
        if not has_resolution and reason not in TECHNICAL_CURRENT_URL_REASONS:
            continue
        if not has_resolution and ats_name != "oraclecloud":
            continue
        destination, destination_reason = preparation_destination(conn, resolved_url)
        candidates.append({
            "posting_id": posting_id,
            "company": company or "",
            "title": title or "",
            "source_url": source_url or "",
            "resolved_url": resolved_url,
            "ats": ats_name,
            "destination": destination,
            "reason": reason,
            "last_error": reason,
            "next_error": destination_reason or "",
        })
    candidates.sort(key=lambda item: item["posting_id"])
    return candidates


def apply_retriage(conn: sqlite3.Connection, posting_ids: list[str], ats: str | None = None) -> dict:
    requested_ids = list(dict.fromkeys(posting_ids))
    requested = len(requested_ids)
    if not requested_ids:
        return {"requested": 0, "updated": 0, "skipped": 0}
    try:
        conn.execute("BEGIN IMMEDIATE")
        current = {
            candidate["posting_id"]: candidate
            for candidate in retriage_candidates(conn, requested_ids, ats=ats)
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


def compensation_retriage_candidates(conn: sqlite3.Connection, now: int | None = None) -> list[dict]:
    from submission.identity import canonical_conflict_reason
    from submission.lanes import classify_url, preparation_destination

    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='compensation_evidence'"
    ).fetchone() is None:
        return []
    current_now = int(time.time()) if now is None else int(now)
    candidates: list[dict] = []
    for row in conn.execute(_compensation_candidate_query(conn)):
        reason = row["last_error"]
        if not _has_compensation_marker(reason):
            continue
        period = requested_period(reason)
        if period is None:
            continue
        context = _context_from_row(row, period)
        if load_resolution(conn, context, current_now) is None:
            continue
        url = row["url"] or ""
        ats_name, lane = classify_url(url)
        if ats_name in {"oraclecloud", "ashby"} or lane.name in {"manual", "unsupported", "ashby"}:
            continue
        if not lane.automatic:
            continue
        if _has_application(conn, row["posting_id"]):
            continue
        if _has_finished_attempt(conn, row["posting_id"]):
            continue
        if canonical_conflict_reason(conn, row["posting_id"], url):
            continue
        destination, destination_reason = preparation_destination(conn, url)
        if destination != "ready":
            continue
        candidates.append({
            "posting_id": row["posting_id"],
            "company": row["company"] or "",
            "title": row["title"] or "",
            "source_url": url,
            "resolved_url": url,
            "ats": ats_name,
            "destination": destination,
            "reason": reason,
            "last_error": reason,
            "next_error": destination_reason or "",
            "candidate_kind": "compensation",
            "original_status": row["status"],
            "original_attempt_count": row["attempt_count"] or 0,
        })
    candidates.sort(key=lambda item: item["posting_id"])
    return candidates


def apply_compensation_retriage(conn: sqlite3.Connection, posting_ids: list[str]) -> dict:
    requested_ids = list(dict.fromkeys(posting_ids))
    requested = len(requested_ids)
    if not requested_ids:
        return {"requested": 0, "updated": 0, "skipped": 0}
    try:
        conn.execute("BEGIN IMMEDIATE")
        current = {c["posting_id"]: c for c in compensation_retriage_candidates(conn) if c["posting_id"] in requested_ids}
        updated = 0
        for posting_id in requested_ids:
            candidate = current.get(posting_id)
            if candidate is None:
                continue
            updated += conn.execute(
                "UPDATE postings SET status='ready', outcome=NULL, last_error='', last_attempt_at=? "
                "WHERE posting_id=? AND status=? AND COALESCE(last_error,'')=? "
                "AND COALESCE(attempt_count,0)=?",
                (int(time.time()), posting_id, candidate["original_status"], candidate["last_error"], candidate["original_attempt_count"]),
            ).rowcount
        conn.commit()
        return {"requested": requested, "updated": updated, "skipped": requested - updated}
    except Exception:
        conn.rollback()
        raise


def _compensation_candidate_query(conn: sqlite3.Connection) -> str:
    source_expr = "COALESCE(p.source, '')" if _has_postings_column(conn, "source") else "''"
    locations_expr = "COALESCE(p.locations, '')" if _has_postings_column(conn, "locations") else "''"
    attempt_expr = "COALESCE(p.attempt_count, 0)" if _has_postings_column(conn, "attempt_count") else "0"
    marker_predicate = compensation_marker_sql_predicate("p.last_error")
    return f"""
        SELECT p.posting_id, COALESCE(p.company,'') AS company, COALESCE(p.title,'') AS title,
               {locations_expr} AS locations, COALESCE(p.url,'') AS url, {source_expr} AS source,
               COALESCE(p.status,'') AS status, COALESCE(p.last_error,'') AS last_error,
               {attempt_expr} AS attempt_count
        FROM postings p
        WHERE p.status IN ('manual','failed')
          AND {marker_predicate}
          AND COALESCE(p.outcome, '') NOT IN ('stale', 'submitted', 'deduplicated')
        ORDER BY p.posting_id
    """


def _has_application(conn: sqlite3.Connection, posting_id: str) -> bool:
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='applications'").fetchone()
    if not exists:
        return False
    return conn.execute("SELECT 1 FROM applications WHERE posting_id=? LIMIT 1", (posting_id,)).fetchone() is not None


def _candidate_query(conn: sqlite3.Connection, posting_ids: list[str] | None = None) -> tuple[str, tuple]:
    id_clause = ""
    params: tuple = ()
    source_expr = "COALESCE(p.source, '')" if _has_postings_column(conn, "source") else "''"
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
               {source_expr} AS source,
               COALESCE(r.source_url, p.url, '') AS source_url,
               COALESCE(NULLIF(r.resolved_url, ''), p.url, '') AS resolved_url,
               COALESCE(p.last_error, '') AS last_error,
               COALESCE(r.resolved_url, '')<>'' AS has_resolution
        FROM postings p
        LEFT JOIN posting_url_resolutions r USING(posting_id)
        WHERE p.status='manual'
          AND COALESCE(COALESCE(NULLIF(r.resolved_url, ''), p.url), '')<>''
          AND COALESCE(p.outcome, '') NOT IN ('stale', 'submitted', 'deduplicated')
          AND COALESCE(p.status, '') NOT IN ('submitted', 'skipped')
          {id_clause}
        ORDER BY p.posting_id
    """.format(id_clause=id_clause, source_expr=source_expr), params


def _has_postings_column(conn: sqlite3.Connection, name: str) -> bool:
    return any(row[1] == name for row in conn.execute("PRAGMA table_info(postings)").fetchall())


def _technical_reason(reason: str, allow_current_url: bool = False) -> bool:
    if allow_current_url:
        return reason in TECHNICAL_CURRENT_URL_REASONS
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
