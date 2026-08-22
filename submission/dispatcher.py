from __future__ import annotations

import logging
import sqlite3
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from contextlib import ExitStack
from pathlib import Path

from drip import recover_stale_claims
from submission.ashby_policy import can_attempt, ensure_lane_state, load_state, record_result, set_enabled
from submission.database import DB, connect_tracker
from submission.executor import execute_claimed_posting
from submission.lanes import ASHBY, DIRECT, WORKDAY, LanePolicy, classify_url

LOG = logging.getLogger(__name__)
AUTOMATIC_POLICIES = (DIRECT, WORKDAY)


def eligible_ashby_posting(conn: sqlite3.Connection, *, now: int) -> str | None:
    """Return the oldest Ashby canary candidate without mutating state."""
    ensure_lane_state(conn)
    if not can_attempt(load_state(conn), now=now):
        return None
    attempts_exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='submission_attempts'").fetchone()
    prior_attempt_filter = (
        "AND NOT EXISTS (SELECT 1 FROM submission_attempts sa "
        "WHERE sa.posting_id=p.posting_id AND sa.finished_at IS NOT NULL)"
        if attempts_exists
        else ""
    )
    rows = conn.execute(
        f"""
        SELECT p.posting_id, p.url, e.resume_pdf
        FROM postings p JOIN emails e USING(posting_id)
        WHERE p.status='ready'
          AND NOT EXISTS (SELECT 1 FROM applications a WHERE a.posting_id=p.posting_id)
          {prior_attempt_filter}
        ORDER BY COALESCE(p.last_attempt_at, 0), p.rowid, p.posting_id
        """
    ).fetchall()
    for row in rows:
        posting_id = row["posting_id"] if isinstance(row, sqlite3.Row) else row[0]
        url = row["url"] if isinstance(row, sqlite3.Row) else row[1]
        resume_pdf = row["resume_pdf"] if isinstance(row, sqlite3.Row) else row[2]
        _ats, lane = classify_url(url)
        if lane.name != ASHBY.name:
            continue
        from submission.executor import _resume_quality_ready, _runtime_path

        ok, _reason = _resume_quality_ready(_runtime_path(resume_pdf), posting_id)
        if ok:
            return posting_id
    return None


def _verify_finished_attempt(conn: sqlite3.Connection, attempt_id: str, posting_id: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM submission_attempts
        WHERE attempt_id=? AND posting_id=? AND finished_at IS NOT NULL
        """,
        (attempt_id, posting_id),
    ).fetchone()


def _record_ashby_policy_result(db_path: Path, result: dict) -> None:
    if _is_pre_attempt_result(result):
        return
    attempt_id = result.get("attempt_id")
    conn = connect_tracker(db_path)
    try:
        if not attempt_id:
            set_enabled(conn, False, now=int(time.time()))
            return
        row = _verify_finished_attempt(conn, str(attempt_id), str(result.get("posting_id")))
        if row is None:
            set_enabled(conn, False, now=int(time.time()))
            return
        outcome = str(row["outcome"] or result.get("outcome") or "")
        reason = str(row["raw_reason"] or result.get("reason") or "")
        record_result(conn, outcome=outcome, reason=reason, now=int(time.time()))
    except Exception:
        LOG.exception("ashby policy update failed; pausing ashby")
        try:
            set_enabled(conn, False, now=int(time.time()))
        except Exception:
            LOG.exception("ashby fail-closed pause failed")
    finally:
        conn.close()


def _is_pre_attempt_result(result: dict) -> bool:
    return (
        result.get("pre_attempt") is True
        and str(result.get("outcome") or "") in {"skipped", "manual", "stale"}
        and not result.get("attempt_id")
        and not result.get("click_attempted")
        and not result.get("submission_uncertain")
        and not result.get("launched")
    )


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


def _release_missing_claim(conn: sqlite3.Connection, posting_id: str, reason: str) -> bool:
    changed = conn.execute(
        """
        UPDATE postings
        SET status='manual', outcome='manual', last_error=?,
            attempt_count=COALESCE(attempt_count, 0) + 1,
            last_attempt_at=?
        WHERE posting_id=? AND status='submitting'
        """,
        (reason[:1000], int(time.time()), posting_id),
    ).rowcount
    conn.commit()
    return changed == 1


def execute(posting_id: str, lane: LanePolicy, *, db_path: Path = DB, dry_run: bool = False) -> dict:
    """Claim and execute one posting using a fresh SQLite connection in this worker thread."""
    conn = connect_tracker(db_path)
    try:
        if not dry_run and not _claim(conn, posting_id):
            return {"posting_id": posting_id, "lane": lane.name, "outcome": "skipped", "reason": "claim lost", "pre_attempt": True}
        row = _claimed_row(conn, posting_id, dry_run=dry_run)
        if row is None:
            if not dry_run:
                _release_missing_claim(conn, posting_id, "claimed row missing")
                return {"posting_id": posting_id, "lane": lane.name, "outcome": "manual", "reason": "claimed row missing", "pre_attempt": True}
            return {"posting_id": posting_id, "lane": lane.name, "outcome": "skipped", "reason": "claimed row missing", "pre_attempt": True}
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
        ashby_candidate = None
        try:
            conn = connect_tracker(db_path)
            try:
                ashby_candidate = eligible_ashby_posting(conn, now=int(time.time()))
            finally:
                conn.close()
        except Exception:
            LOG.exception("ashby candidate selection failed")
        if ashby_candidate is not None:
            executors[ASHBY.name] = stack.enter_context(ThreadPoolExecutor(max_workers=1, thread_name_prefix="submit-ashby"))
            futures.append(executors[ASHBY.name].submit(_execute_by_id, db_path, ashby_candidate, ASHBY, dry_run))
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            if result.get("lane") == ASHBY.name and not dry_run:
                _record_ashby_policy_result(db_path, result)
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
