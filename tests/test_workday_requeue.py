import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from scripts.requeue_workday_recoverable import apply_requeue, recoverable_workday_rows


COVERED_REASONS = (
    "resume upload zone never appeared",
    "apply button not found (posting closed?)",
)


WORKDAY_URL = "https://acme.wd5.myworkdayjobs.com/en-US/External/job/NYC/Role_R123"


def make_db(path: Path, *, attempts: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE postings (
            id TEXT PRIMARY KEY,
            company TEXT,
            title TEXT,
            url TEXT,
            canonical_url TEXT,
            source_status TEXT,
            status TEXT,
            outcome TEXT,
            last_error TEXT,
            attempt_count INTEGER DEFAULT 0
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE application_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            posting_id TEXT NOT NULL
        )
        """
    )
    if attempts:
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
                artifact_refs_json TEXT NOT NULL DEFAULT '{}',
                unanswered_json TEXT NOT NULL DEFAULT '[]'
            )
            """
        )
    conn.commit()
    return conn


def seed(conn, posting_id, *, url=WORKDAY_URL, canonical_url=None, source_status="failed", status=None, outcome=None, last_error=None, attempt_count=2):
    row_url = url
    if row_url == WORKDAY_URL:
        row_url = WORKDAY_URL + f"-{posting_id}"
    conn.execute(
        """
        INSERT INTO postings (id, company, title, url, canonical_url, source_status, status, outcome, last_error, attempt_count)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            posting_id,
            f"Company {posting_id}",
            f"Role {posting_id}",
            row_url,
            canonical_url or row_url,
            source_status,
            status,
            outcome,
            last_error or COVERED_REASONS[0] + ": timeout",
            attempt_count,
        ),
    )
    conn.commit()


def attempt(conn, posting_id, *, click_attempted=0, confirmation_observed=0, finished_at=100):
    conn.execute(
        """
        INSERT INTO submission_attempts (
            attempt_id, posting_id, ats, lane, worker_id, browser_mode, policy_revision,
            started_at, finished_at, click_attempted, confirmation_observed
        ) VALUES (?, ?, 'workday', 'workday', 'w1', 'headless', 'test', 1, ?, ?, ?)
        """,
        ("attempt-" + posting_id, posting_id, finished_at, click_attempted, confirmation_observed),
    )
    conn.commit()


def db_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_preview_returns_only_strictly_safe_workday_rows(tmp_path):
    db_path = tmp_path / "tracker.db"
    conn = make_db(db_path)
    seed(conn, "safe-upload", last_error=COVERED_REASONS[0] + ": missing")
    seed(conn, "safe-apply", source_status="manual", last_error=COVERED_REASONS[1] + ": missing")
    seed(conn, "non-workday", url="https://jobs.lever.co/acme/123", last_error=COVERED_REASONS[0])
    seed(conn, "click-uncertain", last_error="submit click attempted but confirmation was not observed")
    seed(conn, "submitted", source_status="submitted")
    seed(conn, "stale", source_status="stale")
    seed(conn, "unrelated", last_error="captcha required")
    seed(conn, "ledgered")
    conn.execute("INSERT INTO application_ledger (posting_id) VALUES ('ledgered')")
    seed(conn, "clicked")
    attempt(conn, "clicked", click_attempted=1)
    seed(conn, "confirmed")
    attempt(conn, "confirmed", confirmation_observed=1)
    seed(conn, "alias-safe", canonical_url="https://acme.wd5.myworkdayjobs.com/en-US/External/job/shared")
    seed(conn, "alias-active", canonical_url="https://acme.wd5.myworkdayjobs.com/en-US/External/job/shared", source_status="ready")
    seed(conn, "alias-applied", canonical_url="https://acme.wd5.myworkdayjobs.com/en-US/External/job/other", source_status="applied")
    seed(conn, "alias-blocked", canonical_url="https://acme.wd5.myworkdayjobs.com/en-US/External/job/other")

    rows = recoverable_workday_rows(conn)

    assert [row["posting_id"] for row in rows] == ["safe-upload", "safe-apply"]
    assert rows[0] == {
        "posting_id": "safe-upload",
        "company": "Company safe-upload",
        "title": "Role safe-upload",
        "tenant": "acme.wd5.myworkdayjobs.com/external",
        "reason": COVERED_REASONS[0] + ": missing",
        "attempt_count": 2,
        "url": WORKDAY_URL + "-safe-upload",
    }
    assert all("password" not in json.dumps(row).lower() for row in rows)


def test_pre_attempt_ledger_database_is_supported_without_weakening_when_present(tmp_path):
    db_path = tmp_path / "legacy.db"
    conn = make_db(db_path, attempts=False)
    seed(conn, "legacy-safe")

    assert [row["posting_id"] for row in recoverable_workday_rows(conn)] == ["legacy-safe"]


def test_unfinished_click_or_confirmation_attempt_blocks_requeue(tmp_path):
    db_path = tmp_path / "tracker.db"
    conn = make_db(db_path)
    seed(conn, "safe")
    seed(conn, "unfinished-click")
    attempt(conn, "unfinished-click", click_attempted=1, finished_at=None)
    seed(conn, "unfinished-confirmed")
    attempt(conn, "unfinished-confirmed", confirmation_observed=1, finished_at=None)

    assert [row["posting_id"] for row in recoverable_workday_rows(conn)] == ["safe"]


def test_submitted_canonical_alias_blocks_requeue(tmp_path):
    db_path = tmp_path / "tracker.db"
    conn = make_db(db_path)
    shared = "https://acme.wd5.myworkdayjobs.com/en-US/External/job/submitted-alias"
    seed(conn, "safe")
    seed(conn, "alias-candidate", canonical_url=shared)
    seed(conn, "alias-submitted", canonical_url=shared, source_status="submitted")

    assert [row["posting_id"] for row in recoverable_workday_rows(conn)] == ["safe"]


def test_cli_preview_is_default_json_and_does_not_mutate_database(tmp_path):
    db_path = tmp_path / "tracker.db"
    conn = make_db(db_path)
    seed(conn, "safe-upload")
    before = db_hash(db_path)

    result = subprocess.run(
        [sys.executable, "scripts/requeue_workday_recoverable.py", "--db", str(db_path), "--json"],
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert db_hash(db_path) == before
    payload = json.loads(result.stdout)
    assert payload["mode"] == "preview"
    assert payload["count"] == 1
    assert "wd_accounts" not in result.stdout
    assert "secret" not in result.stdout.lower()
    assert "raw_answer" not in result.stdout.lower()


def test_apply_requeues_only_requested_eligible_rows_preserving_ledgers_and_attempt_count(tmp_path):
    db_path = tmp_path / "tracker.db"
    conn = make_db(db_path)
    seed(conn, "requested-a", attempt_count=7)
    seed(conn, "requested-b")
    seed(conn, "not-requested")
    seed(conn, "drifted", source_status="ready", canonical_url="https://acme.wd5.myworkdayjobs.com/en-US/External/job/drifted")
    attempt(conn, "requested-a", click_attempted=0)

    result = apply_requeue(conn, ["requested-a", "drifted"])

    assert result == {"requested": 2, "updated": 1, "skipped": ["drifted"], "updated_ids": ["requested-a"]}
    row = conn.execute("SELECT source_status, outcome, last_error, attempt_count FROM postings WHERE id='requested-a'").fetchone()
    assert tuple(row) == ("ready", None, "requeued after Workday entry repair", 7)
    assert conn.execute("SELECT COUNT(*) FROM submission_attempts WHERE posting_id='requested-a'").fetchone()[0] == 1
    assert conn.execute("SELECT source_status FROM postings WHERE id='not-requested'").fetchone()[0] == "failed"

    second = apply_requeue(conn, ["requested-a"])
    assert second == {"requested": 1, "updated": 0, "skipped": ["requested-a"], "updated_ids": []}


def test_cli_apply_fails_closed_for_missing_database(tmp_path):
    result = subprocess.run(
        [sys.executable, "scripts/requeue_workday_recoverable.py", "--db", str(tmp_path / "missing.db"), "--apply", "--json"],
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is False


def test_cli_apply_rejects_unbounded_request(tmp_path):
    db_path = tmp_path / "tracker.db"
    conn = make_db(db_path)
    seed(conn, "safe")
    before = db_hash(db_path)

    result = subprocess.run(
        [sys.executable, "scripts/requeue_workday_recoverable.py", "--db", str(db_path), "--apply", "--json"],
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    assert db_hash(db_path) == before
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert "--posting-id" in payload["error"]
    assert "--limit" in payload["error"]
