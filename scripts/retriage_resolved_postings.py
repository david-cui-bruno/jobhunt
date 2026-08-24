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


def safe_candidate(candidate: dict) -> dict:
    return {key: candidate[key] for key in SAFE_KEYS}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Preview or apply safe resolved posting re-triage")
    parser.add_argument("--db", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--preview", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--json", action="store_true", required=True)
    args = parser.parse_args(argv)

    conn = sqlite3.connect(args.db, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        candidates = retriage_candidates(conn)
        payload = {
            "mode": "apply" if args.apply else "preview",
            "count": len(candidates),
            "candidates": [safe_candidate(candidate) for candidate in candidates],
        }
        if args.apply:
            payload["result"] = apply_retriage(conn, [candidate["posting_id"] for candidate in candidates])
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
