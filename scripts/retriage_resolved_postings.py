#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from submission.resolutions import apply_retriage, retriage_candidates

SAFE_KEYS = ("posting_id", "company", "title", "source_url", "resolved_url", "ats", "destination", "reason")
DEFAULT_APPLY_LIMIT = 25


def safe_candidate(candidate: dict) -> dict:
    return {key: candidate[key] for key in SAFE_KEYS}


def emit_error(message: str) -> int:
    print(json.dumps({"error": message}, sort_keys=True, separators=(",", ":")))
    return 2


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone() is not None


def open_database(path: str, apply: bool) -> sqlite3.Connection:
    db_path = Path(path)
    if path != ":memory:" and not db_path.exists():
        raise RuntimeError("database file does not exist")
    mode = "rw" if apply else "ro"
    conn = sqlite3.connect(f"file:{db_path}?mode={mode}", timeout=30, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Preview or apply safe resolved posting re-triage")
    parser.add_argument("--db", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--preview", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--json", action="store_true", required=True)
    parser.add_argument("--limit", type=int, default=DEFAULT_APPLY_LIMIT, help="maximum rows to apply, default 25")
    args = parser.parse_args(argv)
    if args.limit < 1:
        return emit_error("limit must be at least 1")

    conn = None
    try:
        conn = open_database(args.db, args.apply)
        if not table_exists(conn, "postings"):
            return emit_error("database is missing postings schema")
        if not table_exists(conn, "posting_url_resolutions"):
            return emit_error("database is missing posting_url_resolutions schema")
        candidates = retriage_candidates(conn)
        selected = candidates[:args.limit] if args.apply else candidates
        payload = {
            "mode": "apply" if args.apply else "preview",
            "count": len(candidates),
            "limit": args.limit,
            "candidates": [safe_candidate(candidate) for candidate in selected],
        }
        if args.apply:
            payload["result"] = apply_retriage(conn, [candidate["posting_id"] for candidate in selected])
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return 0
    except RuntimeError as exc:
        return emit_error(str(exc))
    except sqlite3.Error as exc:
        return emit_error(f"sqlite error: {exc}")
    except Exception as exc:
        return emit_error(f"unexpected error: {exc}")
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
