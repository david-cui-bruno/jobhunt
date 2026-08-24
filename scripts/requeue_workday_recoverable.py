#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from submission.workday_tenant import workday_tenant_key

COVERED_REASONS = (
    "resume upload zone never appeared",
    "apply button not found (posting closed?)",
)
ACTIVE_OR_APPLIED_STATES = {"ready", "submitting", "sprinting", "tailoring", "submitted", "applied"}
TERMINAL_EXCLUDED_STATES = {"stale", "submitted", "applied"}
REQUEUE_ERROR = "requeued after Workday entry repair"

SAFE_FIELDS = ("posting_id", "company", "title", "tenant", "reason", "attempt_count", "url")


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(conn, table):
        return set()
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _require_postings(conn: sqlite3.Connection) -> None:
    cols = _columns(conn, "postings")
    required = {"id", "url"}
    missing = sorted(required - cols)
    if missing:
        raise sqlite3.OperationalError("postings table missing required columns: " + ", ".join(missing))


def _value(row: sqlite3.Row, columns: set[str], name: str, default=None):
    return row[name] if name in columns else default


def _source_state(row: sqlite3.Row, columns: set[str]) -> str:
    value = _value(row, columns, "source_status")
    if value is None:
        value = _value(row, columns, "status")
    return str(value or "")


def _reason(row: sqlite3.Row, columns: set[str]) -> str:
    value = _value(row, columns, "last_error")
    if value is None:
        value = _value(row, columns, "outcome")
    return str(value or "")


def _canonical(row: sqlite3.Row, columns: set[str]) -> str:
    value = _value(row, columns, "canonical_url") or _value(row, columns, "url") or ""
    return str(value)


def _attempt_count(row: sqlite3.Row, columns: set[str]) -> int:
    try:
        return int(_value(row, columns, "attempt_count", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _covered_reason(reason: str) -> bool:
    return any(reason.startswith(prefix) for prefix in COVERED_REASONS)


def _has_application_ledger(conn: sqlite3.Connection, posting_id: str) -> bool:
    if not _table_exists(conn, "application_ledger"):
        return False
    cols = _columns(conn, "application_ledger")
    if "posting_id" not in cols:
        return False
    return conn.execute(
        "SELECT 1 FROM application_ledger WHERE posting_id=? LIMIT 1", (posting_id,)
    ).fetchone() is not None


def _has_finished_click_or_confirmation(conn: sqlite3.Connection, posting_id: str) -> bool:
    if not _table_exists(conn, "submission_attempts"):
        return False
    cols = _columns(conn, "submission_attempts")
    if "posting_id" not in cols:
        return False
    click_col = "click_attempted" if "click_attempted" in cols else "0"
    confirm_col = "confirmation_observed" if "confirmation_observed" in cols else "0"
    return conn.execute(
        f"""
        SELECT 1 FROM submission_attempts
        WHERE posting_id=?
          AND (COALESCE({click_col}, 0)=1 OR COALESCE({confirm_col}, 0)=1)
        LIMIT 1
        """,
        (posting_id,),
    ).fetchone() is not None


def _active_alias_exists(conn: sqlite3.Connection, row: sqlite3.Row, columns: set[str]) -> bool:
    canonical = _canonical(row, columns)
    if not canonical:
        return False
    status_expr = "COALESCE(source_status, status, '')"
    if "source_status" not in columns and "status" in columns:
        status_expr = "COALESCE(status, '')"
    elif "status" not in columns and "source_status" in columns:
        status_expr = "COALESCE(source_status, '')"
    elif "status" not in columns and "source_status" not in columns:
        return False
    canonical_expr = "COALESCE(canonical_url, url, '')" if "canonical_url" in columns else "COALESCE(url, '')"
    placeholders = ",".join("?" for _ in ACTIVE_OR_APPLIED_STATES)
    params = [row["id"], canonical, *sorted(ACTIVE_OR_APPLIED_STATES)]
    return conn.execute(
        f"""
        SELECT 1 FROM postings
        WHERE id<>?
          AND {canonical_expr}=?
          AND {status_expr} IN ({placeholders})
        LIMIT 1
        """,
        params,
    ).fetchone() is not None


def _is_eligible(conn: sqlite3.Connection, row: sqlite3.Row, columns: set[str]) -> bool:
    state = _source_state(row, columns)
    if state not in {"failed", "manual"} or state in TERMINAL_EXCLUDED_STATES:
        return False
    url = str(_value(row, columns, "url", "") or "")
    if not workday_tenant_key(url):
        return False
    if not _covered_reason(_reason(row, columns)):
        return False
    posting_id = str(row["id"])
    if _has_application_ledger(conn, posting_id):
        return False
    if _has_finished_click_or_confirmation(conn, posting_id):
        return False
    if _active_alias_exists(conn, row, columns):
        return False
    return True


def recoverable_workday_rows(conn: sqlite3.Connection) -> list[dict]:
    conn.row_factory = sqlite3.Row
    _require_postings(conn)
    columns = _columns(conn, "postings")
    rows = conn.execute("SELECT * FROM postings ORDER BY rowid").fetchall()
    safe = []
    for row in rows:
        if not _is_eligible(conn, row, columns):
            continue
        url = str(_value(row, columns, "url", "") or "")
        safe.append(
            {
                "posting_id": str(row["id"]),
                "company": str(_value(row, columns, "company", "") or ""),
                "title": str(_value(row, columns, "title", "") or ""),
                "tenant": workday_tenant_key(url),
                "reason": _reason(row, columns),
                "attempt_count": _attempt_count(row, columns),
                "url": url,
            }
        )
    return safe


def apply_requeue(conn: sqlite3.Connection, posting_ids: Iterable[str]) -> dict:
    requested = list(dict.fromkeys(str(pid) for pid in posting_ids))
    updated: list[str] = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        eligible = {row["posting_id"] for row in recoverable_workday_rows(conn)}
        columns = _columns(conn, "postings")
        status_col = "source_status" if "source_status" in columns else "status"
        if status_col not in columns:
            raise sqlite3.OperationalError("postings table missing source_status or status")
        for posting_id in requested:
            if posting_id not in eligible:
                continue
            changed = conn.execute(
                f"""
                UPDATE postings
                SET {status_col}='ready', outcome=NULL, last_error=?
                WHERE id=? AND {status_col} IN ('failed', 'manual')
                """,
                (REQUEUE_ERROR, posting_id),
            ).rowcount
            if changed == 1:
                updated.append(posting_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {
        "requested": len(requested),
        "updated": len(updated),
        "skipped": [pid for pid in requested if pid not in updated],
        "updated_ids": updated,
    }


def _connect_existing(path: Path, *, readonly: bool) -> sqlite3.Connection:
    if not path.exists():
        raise FileNotFoundError(f"database does not exist: {path}")
    if readonly:
        uri = f"file:{path.resolve()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    else:
        conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _emit(payload: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, sort_keys=True))
    else:
        if payload.get("ok") is False:
            print("error: " + str(payload.get("error", "unknown error")))
        elif payload.get("mode") == "preview":
            print(f"recoverable Workday rows: {payload['count']}")
            for row in payload.get("rows", []):
                print(f"{row['posting_id']}\t{row['company']}\t{row['title']}\t{row['reason']}")
        else:
            print(f"updated {payload.get('updated', 0)} of {payload.get('requested', 0)} requested rows")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Preview or requeue safe Workday recoverable rows")
    parser.add_argument("--db", required=True, help="Path to an existing tracker SQLite database")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--preview", action="store_true", help="Preview eligible rows without mutation")
    mode.add_argument("--apply", action="store_true", help="Apply requeue for requested eligible rows")
    parser.add_argument("--json", action="store_true", help="Emit JSON")
    parser.add_argument("--posting-id", action="append", default=[], help="Posting ID to apply. Repeatable")
    parser.add_argument("--limit", type=int, default=None, help="Bound preview rows used by apply when no posting IDs are supplied")
    args = parser.parse_args(argv)

    path = Path(args.db)
    preview = not args.apply
    try:
        conn = _connect_existing(path, readonly=preview)
        if preview:
            rows = recoverable_workday_rows(conn)
            if args.limit is not None:
                rows = rows[: max(args.limit, 0)]
            payload = {"ok": True, "mode": "preview", "count": len(rows), "rows": rows}
        else:
            if not args.posting_id and args.limit is None:
                raise ValueError("--apply requires at least one --posting-id or a positive --limit")
            if args.limit is not None and args.limit <= 0:
                raise ValueError("--apply requires a positive --limit when no --posting-id is supplied")
            candidates = args.posting_id
            if not candidates:
                rows = recoverable_workday_rows(conn)
                rows = rows[: args.limit]
                candidates = [row["posting_id"] for row in rows]
            result = apply_requeue(conn, candidates)
            payload = {"ok": True, "mode": "apply", **result}
        _emit(payload, args.json)
        return 0
    except Exception as exc:
        _emit({"ok": False, "mode": "apply" if args.apply else "preview", "error": type(exc).__name__ + ": " + str(exc)}, args.json)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
