from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass

from apply.jd import detect_ats


@dataclass(frozen=True)
class LanePolicy:
    name: str
    ats: frozenset[str]
    concurrency: int
    attempts_per_cycle: int
    automatic: bool


DIRECT = LanePolicy("direct", frozenset({"greenhouse", "lever", "workable", "rippling"}), 2, 8, True)
WORKDAY = LanePolicy("workday", frozenset({"workday"}), 1, 2, True)
ASHBY = LanePolicy("ashby", frozenset({"ashby"}), 1, 1, False)
MANUAL = LanePolicy("manual", frozenset({"smartrecruiters"}), 0, 0, False)
UNSUPPORTED = LanePolicy("unsupported", frozenset({"other", "icims"}), 0, 0, False)
POLICIES = (DIRECT, WORKDAY, ASHBY, MANUAL, UNSUPPORTED)


def lane_for(ats: str) -> LanePolicy:
    return next((policy for policy in POLICIES if ats in policy.ats), UNSUPPORTED)


def classify_url(url: str) -> tuple[str, LanePolicy]:
    ats = detect_ats(url)
    return ats, lane_for(ats)


def quarantine_unsupported(conn: sqlite3.Connection) -> int:
    """Move queued rows with no supported/preparable adapter to manual."""
    quarantined = 0
    rows = conn.execute("SELECT posting_id, url FROM postings WHERE status='queued'").fetchall()
    for row in rows:
        posting_id = row["posting_id"] if isinstance(row, sqlite3.Row) else row[0]
        url = row["url"] if isinstance(row, sqlite3.Row) else row[1]
        ats, lane = classify_url(url)
        if lane.name != "unsupported":
            continue
        changed = conn.execute(
            "UPDATE postings SET status='manual', outcome='manual', last_error=?, last_attempt_at=? "
            "WHERE posting_id=? AND status='queued'",
            (f"no adapter for {ats}", int(time.time()), posting_id),
        ).rowcount
        quarantined += changed
    conn.commit()
    return quarantined
