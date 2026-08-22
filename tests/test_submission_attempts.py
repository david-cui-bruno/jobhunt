import sqlite3
from pathlib import Path

import pytest

from submission.attempts import ensure_submission_attempts, finish_attempt, start_attempt
from submission.database import connect_tracker


def test_connect_tracker_enables_wal_and_busy_timeout(tmp_path: Path) -> None:
    conn = connect_tracker(tmp_path / "tracker.db")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 5000
    conn.close()


def test_connect_tracker_does_not_enable_foreign_key_enforcement(tmp_path: Path) -> None:
    conn = connect_tracker(tmp_path / "tracker.db")
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 0
    finally:
        conn.close()


def test_connect_tracker_initializes_disabled_ashby_lane_state(tmp_path: Path) -> None:
    conn = connect_tracker(tmp_path / "tracker.db")
    try:
        row = conn.execute(
            "SELECT enabled, tier, consecutive_confirmed, policy_revision FROM ats_lane_state WHERE ats='ashby'"
        ).fetchone()
        assert tuple(row) == (0, 0, 0, "ashby-canary-v1")
    finally:
        conn.close()


def test_attempt_ledger_is_append_only_and_finishes_once() -> None:
    conn = sqlite3.connect(":memory:")
    ensure_submission_attempts(conn)
    start_attempt(
        conn,
        attempt_id="a1",
        posting_id="p1",
        ats="greenhouse",
        lane="direct",
        worker_id="worker-1",
        browser_mode="isolated-headless",
        policy_revision="dispatcher-v1",
        started_at=100,
    )
    finish_attempt(
        conn,
        attempt_id="a1",
        outcome="submitted",
        reason_code="confirmed",
        raw_reason="confirmed",
        click_attempted=True,
        confirmation_observed=True,
        artifact_refs={"screenshot": "out/screenshots/p1.png"},
        finished_at=110,
    )
    row = conn.execute(
        "SELECT posting_id,ats,lane,outcome,reason_code,click_attempted,confirmation_observed "
        "FROM submission_attempts WHERE attempt_id='a1'"
    ).fetchone()
    assert row == ("p1", "greenhouse", "direct", "submitted", "confirmed", 1, 1)
    with pytest.raises(sqlite3.IntegrityError):
        start_attempt(conn, attempt_id="a1", posting_id="p1", ats="greenhouse", lane="direct",
                      worker_id="worker-2", browser_mode="isolated-headless",
                      policy_revision="dispatcher-v1", started_at=120)
