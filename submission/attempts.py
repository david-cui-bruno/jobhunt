import json
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS submission_attempts (
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
);
CREATE INDEX IF NOT EXISTS submission_attempts_posting_idx
ON submission_attempts(posting_id, started_at);
CREATE INDEX IF NOT EXISTS submission_attempts_ats_idx
ON submission_attempts(ats, started_at);
"""


def ensure_submission_attempts(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def start_attempt(conn, *, attempt_id, posting_id, ats, lane, worker_id,
                  browser_mode, policy_revision, started_at=None) -> None:
    conn.execute(
        "INSERT INTO submission_attempts "
        "(attempt_id,posting_id,ats,lane,worker_id,browser_mode,policy_revision,started_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (attempt_id, posting_id, ats, lane, worker_id, browser_mode,
         policy_revision, started_at or int(time.time())),
    )
    conn.commit()


def finish_attempt(conn, *, attempt_id, outcome, reason_code, raw_reason,
                   click_attempted, confirmation_observed, artifact_refs=None,
                   finished_at=None) -> None:
    end = finished_at or int(time.time())
    changed = conn.execute(
        "UPDATE submission_attempts SET finished_at=?, "
        "duration_ms=(?-started_at)*1000, outcome=?, reason_code=?, raw_reason=?, "
        "click_attempted=?, confirmation_observed=?, artifact_refs_json=? "
        "WHERE attempt_id=? AND finished_at IS NULL",
        (end, end, outcome, reason_code, raw_reason[:1000], int(click_attempted),
         int(confirmation_observed), json.dumps(artifact_refs or {}, sort_keys=True), attempt_id),
    ).rowcount
    if changed != 1:
        conn.rollback()
        raise sqlite3.IntegrityError(f"attempt already finished or missing: {attempt_id}")
    conn.commit()
