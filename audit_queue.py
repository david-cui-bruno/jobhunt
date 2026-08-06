"""Audit active job postings and permanently filter stale URLs.

This is intentionally separate from submit.py. It checks every queued, ready, and
failed posting without opening Playwright or retrying an application adapter.
"""
from __future__ import annotations

import concurrent.futures
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from submit import DB, _ensure_outcome_columns, _posting_dead  # noqa: E402

STATUSES = ("queued", "ready", "failed")


def audit() -> dict[str, int]:
    conn = sqlite3.connect(DB)
    _ensure_outcome_columns(conn)
    rows = conn.execute(
        "SELECT posting_id, company, url, status FROM postings WHERE status IN (?,?,?)",
        STATUSES,
    ).fetchall()
    conn.close()

    def check(row):
        posting_id, company, url, status = row
        return row, _posting_dead(url)

    stale = 0
    checked = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for row, is_stale in pool.map(check, rows):
            checked += 1
            if not is_stale:
                continue
            conn = sqlite3.connect(DB)
            conn.execute(
                """UPDATE postings
                      SET status='filtered_out', outcome='stale',
                          last_attempt_at=strftime('%s','now'),
                          last_error='queue audit marked posting stale'
                    WHERE posting_id=? AND status IN ('queued','ready','failed')""",
                (row[0],),
            )
            conn.commit()
            conn.close()
            stale += 1
            print(f"stale: {row[1]} [{row[3]}] {row[2]}")
    return {"checked": checked, "stale": stale, "preserved": checked - stale}


if __name__ == "__main__":
    print(audit())
