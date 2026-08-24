import json
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


def test_connect_tracker_initializes_submission_attempts_schema(tmp_path: Path) -> None:
    conn = connect_tracker(tmp_path / "tracker.db")
    try:
        columns = {
            row[1]: row[2]
            for row in conn.execute("PRAGMA table_info(submission_attempts)").fetchall()
        }
        assert columns == {
            "attempt_id": "TEXT",
            "posting_id": "TEXT",
            "ats": "TEXT",
            "lane": "TEXT",
            "worker_id": "TEXT",
            "browser_mode": "TEXT",
            "policy_revision": "TEXT",
            "started_at": "INTEGER",
            "finished_at": "INTEGER",
            "duration_ms": "INTEGER",
            "outcome": "TEXT",
            "reason_code": "TEXT",
            "raw_reason": "TEXT",
            "click_attempted": "INTEGER",
            "confirmation_observed": "INTEGER",
            "artifact_refs_json": "TEXT",
            "unanswered_json": "TEXT",
        }
        indexes = {
            row[1]
            for row in conn.execute("PRAGMA index_list(submission_attempts)").fetchall()
        }
        assert "submission_attempts_posting_idx" in indexes
        assert "submission_attempts_ats_idx" in indexes
    finally:
        conn.close()


def test_manage_lanes_status_initializes_attempt_schema_without_preseed(tmp_path: Path, capsys) -> None:
    from manage_lanes import main

    db = tmp_path / "tracker.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE postings (
            posting_id TEXT PRIMARY KEY,
            company TEXT,
            title TEXT,
            status TEXT,
            url TEXT,
            outcome TEXT,
            last_attempt_at INTEGER,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            application_notes TEXT
        );
        CREATE TABLE emails (posting_id TEXT PRIMARY KEY, resume_pdf TEXT);
        CREATE TABLE applications (
            posting_id TEXT PRIMARY KEY, resume_path TEXT, ats TEXT,
            submitted_at INTEGER, confirmation TEXT, notes TEXT
        );
        """
    )
    conn.commit()
    conn.close()

    assert main(["--db", str(db), "status", "ashby"]) == 0
    capsys.readouterr()

    conn = sqlite3.connect(db)
    try:
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='submission_attempts'"
        ).fetchone() == (1,)
    finally:
        conn.close()


def test_ensure_submission_attempts_preserves_caller_transaction() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE caller_owned (id INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO caller_owned (id) VALUES (1)")

    ensure_submission_attempts(conn)

    assert conn.in_transaction
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM caller_owned").fetchone() == (0,)
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='submission_attempts'"
    ).fetchone() is None


def test_ensure_submission_attempts_adds_unanswered_json_to_existing_schema() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE submission_attempts (
            attempt_id TEXT PRIMARY KEY,
            posting_id TEXT NOT NULL,
            ats TEXT NOT NULL,
            lane TEXT NOT NULL,
            worker_id TEXT NOT NULL,
            browser_mode TEXT NOT NULL,
            policy_revision TEXT NOT NULL,
            started_at INTEGER NOT NULL,
            finished_at INTEGER,
            duration_ms INTEGER,
            outcome TEXT,
            reason_code TEXT,
            raw_reason TEXT,
            click_attempted INTEGER NOT NULL DEFAULT 0,
            confirmation_observed INTEGER NOT NULL DEFAULT 0,
            artifact_refs_json TEXT NOT NULL DEFAULT '{}'
        )
        """
    )
    conn.commit()

    ensure_submission_attempts(conn)
    ensure_submission_attempts(conn)

    columns = {row[1] for row in conn.execute("PRAGMA table_info(submission_attempts)").fetchall()}
    assert "unanswered_json" in columns


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
        unanswered=["Current location"],
        finished_at=110,
    )
    row = conn.execute(
        "SELECT posting_id,ats,lane,outcome,reason_code,click_attempted,confirmation_observed,unanswered_json "
        "FROM submission_attempts WHERE attempt_id='a1'"
    ).fetchone()
    assert row[:7] == ("p1", "greenhouse", "direct", "submitted", "confirmed", 1, 1)
    assert json.loads(row[7]) == ["Current location"]
    with pytest.raises(sqlite3.IntegrityError):
        start_attempt(conn, attempt_id="a1", posting_id="p1", ats="greenhouse", lane="direct",
                      worker_id="worker-2", browser_mode="isolated-headless",
                      policy_revision="dispatcher-v1", started_at=120)
