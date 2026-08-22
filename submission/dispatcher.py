from __future__ import annotations

import logging
import sqlite3
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from contextlib import ExitStack
from pathlib import Path

from drip import recover_stale_claims
from submission.database import DB, connect_tracker
from submission.executor import execute_claimed_posting
from submission.lanes import DIRECT, WORKDAY, LanePolicy, classify_url

LOG = logging.getLogger(__name__)
AUTOMATIC_POLICIES = (DIRECT, WORKDAY)


def select_for_lane(db_path: Path = DB, policy: LanePolicy = DIRECT, limit: int | None = None) -> list[str]:
    """Return ready posting IDs eligible for one automatic lane."""
    if not policy.automatic or policy.concurrency <= 0 or policy.attempts_per_cycle <= 0:
        return []
    remaining = policy.attempts_per_cycle if limit is None else min(limit, policy.attempts_per_cycle)
    selected: list[str] = []
    conn = connect_tracker(db_path)
    try:
        rows = conn.execute(
            """
            SELECT p.posting_id, p.url
            FROM postings p JOIN emails e USING(posting_id)
            WHERE p.status='ready'
            ORDER BY COALESCE(p.last_attempt_at, 0), p.posting_id
            """
        ).fetchall()
        for row in rows:
            _ats, lane = classify_url(row["url"])
            if lane.name != policy.name:
                continue
            selected.append(row["posting_id"])
            if len(selected) >= remaining:
                break
        return selected
    finally:
        conn.close()


def _claim(conn: sqlite3.Connection, posting_id: str) -> bool:
    changed = conn.execute(
        "UPDATE postings SET status='submitting', last_attempt_at=? WHERE posting_id=? AND status='ready'",
        (int(time.time()), posting_id),
    ).rowcount
    conn.commit()
    return changed == 1


def _claimed_row(conn: sqlite3.Connection, posting_id: str, *, dry_run: bool = False) -> sqlite3.Row | None:
    expected_status = "ready" if dry_run else "submitting"
    return conn.execute(
        """
        SELECT p.*, e.resume_pdf
        FROM postings p JOIN emails e USING(posting_id)
        WHERE p.posting_id=? AND p.status=?
        """,
        (posting_id, expected_status),
    ).fetchone()


def _mark_worker_failure(db_path: Path, posting_id: str, reason: str) -> None:
    conn = connect_tracker(db_path)
    try:
        conn.execute(
            """
            UPDATE postings
            SET status='failed', outcome='failed', last_error=?,
                attempt_count=COALESCE(attempt_count, 0) + 1,
                last_attempt_at=?
            WHERE posting_id=? AND status='submitting'
            """,
            (reason[:1000], int(time.time()), posting_id),
        )
        conn.commit()
    finally:
        conn.close()


def execute(posting_id: str, lane: LanePolicy, *, db_path: Path = DB, dry_run: bool = False) -> dict:
    """Claim and execute one posting using a fresh SQLite connection in this worker thread."""
    conn = connect_tracker(db_path)
    try:
        if not dry_run and not _claim(conn, posting_id):
            return {"posting_id": posting_id, "lane": lane.name, "outcome": "skipped", "reason": "claim lost"}
        row = _claimed_row(conn, posting_id, dry_run=dry_run)
        if row is None:
            return {"posting_id": posting_id, "lane": lane.name, "outcome": "skipped", "reason": "claimed row missing"}
        result = execute_claimed_posting(
            conn,
            row,
            lane=lane,
            dry_run=dry_run,
            worker_id=f"{lane.name}-{threading.get_ident()}",
        )
        return result
    finally:
        conn.close()


def _execute_by_id(db_path: Path, posting_id: str, lane: LanePolicy, dry_run: bool) -> dict:
    try:
        result = execute(posting_id, lane, db_path=db_path, dry_run=dry_run)
    except Exception as exc:  # isolate lane/posting failures from every other lane
        reason = f"dispatcher worker failed: {type(exc).__name__}: {exc}"
        LOG.exception("dispatcher worker failed for %s in %s", posting_id, lane.name)
        if not dry_run:
            _mark_worker_failure(db_path, posting_id, reason)
        result = {"outcome": "failed", "reason": reason}
    result.setdefault("posting_id", posting_id)
    result.setdefault("lane", lane.name)
    return result


def dispatch_cycle(db_path: Path = DB, *, dry_run: bool = False) -> list[dict]:
    """Run one concurrent submit dispatch cycle with a dedicated executor per automatic lane."""
    db_path = Path(db_path)
    if not dry_run:
        conn = connect_tracker(db_path)
        try:
            recover_stale_claims(conn)
        finally:
            conn.close()

    results: list[dict] = []
    futures: list[Future] = []
    with ExitStack() as stack:
        executors: dict[str, ThreadPoolExecutor] = {}
        for policy in AUTOMATIC_POLICIES:
            executors[policy.name] = stack.enter_context(
                ThreadPoolExecutor(max_workers=policy.concurrency, thread_name_prefix=f"submit-{policy.name}")
            )
            for posting_id in select_for_lane(db_path, policy, policy.attempts_per_cycle):
                futures.append(executors[policy.name].submit(_execute_by_id, db_path, posting_id, policy, dry_run))
        for future in as_completed(futures):
            results.append(future.result())
    return results


def run_forever(*, poll_seconds: float = 30.0, stop_event: threading.Event | None = None) -> None:
    """Run dispatch cycles until stop_event is set, recovering from cycle-level exceptions."""
    stop_event = stop_event or threading.Event()
    while not stop_event.is_set():
        try:
            dispatch_cycle()
        except Exception:
            LOG.exception("submit dispatch cycle failed")
        if stop_event.wait(poll_seconds):
            break
