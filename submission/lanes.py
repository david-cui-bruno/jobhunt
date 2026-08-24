from __future__ import annotations

import sqlite3
import time
import urllib.parse
from dataclasses import dataclass

from apply.jd import detect_ats


@dataclass(frozen=True)
class LanePolicy:
    name: str
    ats: frozenset[str]
    concurrency: int
    attempts_per_cycle: int
    automatic: bool
    preparable: bool
    owns_ready_flow: bool


DIRECT = LanePolicy("direct", frozenset({"greenhouse", "lever", "workable", "rippling"}), 2, 8, True, True, True)
WORKDAY = LanePolicy("workday", frozenset({"workday"}), 2, 4, True, True, True)
ORACLE = LanePolicy("oracle", frozenset({"oraclecloud"}), 1, 2, True, True, True)
ASHBY = LanePolicy("ashby", frozenset({"ashby"}), 1, 1, False, True, False)
MANUAL = LanePolicy("manual", frozenset({"smartrecruiters"}), 0, 0, False, True, False)
EMAIL = LanePolicy("email", frozenset({"email"}), 0, 0, False, False, True)
WAAS = LanePolicy("waas", frozenset({"waas"}), 0, 0, False, False, True)
UNSUPPORTED = LanePolicy("unsupported", frozenset({"other", "icims"}), 0, 0, False, False, False)
POLICIES = (DIRECT, WORKDAY, ORACLE, ASHBY, MANUAL, EMAIL, WAAS, UNSUPPORTED)


def lane_for(ats: str) -> LanePolicy:
    return next((policy for policy in POLICIES if ats in policy.ats), UNSUPPORTED)


def classify_url(url: str) -> tuple[str, LanePolicy]:
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower()
    if parsed.scheme == "mailto" or host.endswith("news.ycombinator.com"):
        return "email", EMAIL
    if host.endswith("workatastartup.com"):
        return "waas", WAAS
    ats = detect_ats(url)
    return ats, lane_for(ats)


def ashby_enabled(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute(
            "SELECT enabled FROM ats_lane_state WHERE ats='ashby'"
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    return bool(row and row[0])


def preparation_destination(conn: sqlite3.Connection, url: str) -> tuple[str, str | None]:
    ats, lane = classify_url(url)
    if lane.owns_ready_flow or (lane.name == ASHBY.name and ashby_enabled(conn)):
        return "ready", None
    if lane.preparable:
        return "manual", f"prepared for manual completion: {ats}"
    return "manual", f"no adapter for {ats}"


def reconcile_nonautomatic_ready(conn: sqlite3.Connection) -> int:
    reconciled = 0
    rows = conn.execute("SELECT posting_id, url FROM postings WHERE status='ready'").fetchall()
    for row in rows:
        posting_id = row["posting_id"] if isinstance(row, sqlite3.Row) else row[0]
        url = row["url"] if isinstance(row, sqlite3.Row) else row[1]
        destination, reason = preparation_destination(conn, url)
        if destination == "ready":
            continue
        changed = conn.execute(
            "UPDATE postings SET status=?, outcome='manual', last_error=?, last_attempt_at=? "
            "WHERE posting_id=? AND status='ready'",
            (destination, reason, int(time.time()), posting_id),
        ).rowcount
        reconciled += changed
    conn.commit()
    return reconciled


def quarantine_unsupported(conn: sqlite3.Connection) -> int:
    """Move queued rows that cannot enter an active automatic lane to manual."""
    quarantined = 0
    rows = conn.execute("SELECT posting_id, url FROM postings WHERE status='queued'").fetchall()
    for row in rows:
        posting_id = row["posting_id"] if isinstance(row, sqlite3.Row) else row[0]
        url = row["url"] if isinstance(row, sqlite3.Row) else row[1]
        ats, lane = classify_url(url)
        if lane.name == "ashby" and not ashby_enabled(conn):
            reason = (
                "ashby automation disabled after spam rejection; "
                "apply manually from a trusted browser"
            )
        elif lane.name == "unsupported":
            reason = f"no adapter for {ats}"
        else:
            continue
        changed = conn.execute(
            "UPDATE postings SET status='manual', outcome='manual', last_error=?, last_attempt_at=? "
            "WHERE posting_id=? AND status='queued'",
            (reason, int(time.time()), posting_id),
        ).rowcount
        quarantined += changed
    conn.commit()
    return quarantined
