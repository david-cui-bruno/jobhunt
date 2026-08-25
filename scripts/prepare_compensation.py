import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import List, Optional

from apply.jd import fetch_jd
from compensation.research import SearchProviderUnavailable, TavilySearchProvider, prepare_posting
from submission.database import connect_tracker


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


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        conn = connect_tracker(Path(args.db))
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
