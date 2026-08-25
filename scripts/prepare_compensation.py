import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apply.jd import fetch_jd
from compensation.research import SearchProviderUnavailable, TavilySearchProvider, prepare_posting
from submission.database import connect_tracker


_POSTINGS_COLUMNS = {"posting_id", "company", "title", "locations", "url", "status", "last_error"}


def _provider_from_env():
    key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not key:
        return None
    return TavilySearchProvider(key)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prepare source-backed compensation evidence")
    parser.add_argument("--db", required=True, help="Path to tracker SQLite database")
    parser.add_argument("--posting-id", action="append", required=True, help="Posting ID to prepare. Repeatable.")
    parser.add_argument("--json", action="store_true", help="Emit JSON results")
    return parser


def _validate_db_path(path: Path) -> None:
    if not path.exists() or not path.is_file():
        raise ValueError("database file does not exist")
    conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    try:
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='postings'"
        ).fetchone()
        if table is None:
            raise ValueError("database is missing required postings table")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(postings)").fetchall()}
        missing = sorted(_POSTINGS_COLUMNS - columns)
        if missing:
            raise ValueError("postings table is missing required columns: %s" % ", ".join(missing))
    finally:
        conn.close()


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    db_path = Path(args.db)
    try:
        _validate_db_path(db_path)
        conn = connect_tracker(db_path)
    except Exception as exc:
        print("invalid database: %s" % exc, file=sys.stderr)
        return 2

    provider = None
    try:
        provider = _provider_from_env()
    except SearchProviderUnavailable:
        provider = None

    results = []
    try:
        now = int(time.time())
        for posting_id in args.posting_id:
            results.append(prepare_posting(conn, posting_id, provider, now=now, fetch_jd=fetch_jd))
        conn.commit()
    finally:
        conn.close()

    if args.json:
        print(json.dumps(results, sort_keys=True))
    else:
        for result in results:
            print("{posting_id}: {status}".format(**result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
