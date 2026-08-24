from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from submission.lanes import DIRECT, ASHBY
from submission.executor import execute_claimed_posting


def _write_quality(pdf: Path, posting_id: str) -> None:
    pdf.write_bytes(b"%PDF-1.4\n% one page fixture\n")
    pdf.with_suffix(".quality.json").write_text(json.dumps({
        "version": 1,
        "posting_id": posting_id,
        "source": "deterministic_grounded",
        "review_required": False,
        "structural_validation": "passed",
        "page_count": 1,
    }))


def ready_row(tmp_path: Path, *, ats: str = "greenhouse", posting_id: str = "one") -> tuple[sqlite3.Connection, sqlite3.Row]:
    db = tmp_path / "tracker.db"
    pdf = tmp_path / f"{posting_id}.pdf"
    _write_quality(pdf, posting_id)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
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
            last_error TEXT
        );
        CREATE TABLE emails (posting_id TEXT PRIMARY KEY, resume_pdf TEXT);
        CREATE TABLE applications (
            posting_id TEXT PRIMARY KEY, resume_path TEXT, ats TEXT,
            submitted_at INTEGER, confirmation TEXT, notes TEXT
        );
        """
    )
    url = f"https://boards.{ats}.io/embed/job_app?token={posting_id}"
    if ats == "ashby":
        url = f"https://jobs.ashbyhq.com/example/{posting_id}"
    conn.execute(
        "INSERT INTO postings (posting_id,company,title,status,url) VALUES (?,?,?,?,?)",
        (posting_id, "Example", "Engineer", "submitting", url),
    )
    conn.execute("INSERT INTO emails VALUES (?, ?)", (posting_id, str(pdf)))
    conn.commit()
    row = conn.execute(
        "SELECT p.*, e.resume_pdf FROM postings p JOIN emails e USING(posting_id) WHERE p.posting_id=?",
        (posting_id,),
    ).fetchone()
    return conn, row


def attempt(conn: sqlite3.Connection) -> sqlite3.Row:
    return conn.execute("SELECT * FROM submission_attempts").fetchone()


def test_executor_records_manual_attempt_and_releases_other_lanes(tmp_path, monkeypatch) -> None:
    conn, row = ready_row(tmp_path, ats="greenhouse")
    monkeypatch.setattr("submission.executor._posting_dead", lambda url: False)
    monkeypatch.setattr("submission.executor.run_adapter", lambda payload: {
        "outcome": "manual",
        "detected_ats": "greenhouse",
        "reason": "needs answers: ['Current location']",
        "unanswered": ["Current location"],
        "click_attempted": False,
        "artifact_refs": {"filled_form_screenshot": "/local/shot.png"},
    })
    notices: list[tuple[str, str]] = []
    monkeypatch.setattr("submission.executor._send_notice", lambda subject, body: notices.append((subject, body)) or True)

    result = execute_claimed_posting(conn, row, lane=DIRECT, dry_run=False, worker_id="direct-1")

    assert result["outcome"] == "manual"
    assert result["unanswered"] == ["Current location"]
    assert result["artifact_refs"] == {"filled_form_screenshot": "/local/shot.png"}
    assert notices and "/local/shot.png" not in notices[0][1]
    assert conn.execute("SELECT status FROM postings WHERE posting_id=?", (row["posting_id"],)).fetchone()[0] == "manual"
    assert conn.execute("SELECT COUNT(*) FROM submission_attempts").fetchone()[0] == 1
    rec = attempt(conn)
    assert rec["lane"] == "direct"
    assert rec["worker_id"] == "direct-1"
    assert rec["outcome"] == "manual"
    assert rec["click_attempted"] == 0
    assert json.loads(rec["unanswered_json"]) == ["Current location"]
    assert json.loads(rec["artifact_refs_json"]) == {"filled_form_screenshot": "/local/shot.png"}


def test_executor_records_confirmed_submission_and_application(tmp_path, monkeypatch) -> None:
    conn, row = ready_row(tmp_path, ats="greenhouse")
    monkeypatch.setattr("submission.executor._posting_dead", lambda url: False)
    monkeypatch.setattr("submission.executor.run_adapter", lambda payload: {
        "outcome": "submitted",
        "ok": True,
        "submitted": True,
        "detected_ats": "greenhouse",
        "reason": "confirmed",
        "click_attempted": True,
    })

    result = execute_claimed_posting(conn, row, lane=DIRECT, dry_run=False, worker_id="direct-1")

    assert result["outcome"] == "submitted"
    assert conn.execute("SELECT status FROM postings WHERE posting_id='one'").fetchone()[0] == "submitted"
    assert tuple(conn.execute("SELECT ats, notes FROM applications WHERE posting_id='one'").fetchone()) == ("greenhouse", "")
    rec = attempt(conn)
    assert rec["outcome"] == "submitted"
    assert rec["click_attempted"] == 1
    assert rec["confirmation_observed"] == 1


def test_executor_leaves_retryable_before_click_ready(tmp_path, monkeypatch) -> None:
    conn, row = ready_row(tmp_path, ats="greenhouse")
    monkeypatch.setattr("submission.executor._posting_dead", lambda url: False)
    monkeypatch.setattr("submission.executor.run_adapter", lambda payload: {
        "outcome": "retryable_failure",
        "ok": False,
        "submitted": False,
        "retryable": True,
        "detected_ats": "greenhouse",
        "reason": "network before click",
        "click_attempted": False,
    })

    result = execute_claimed_posting(conn, row, lane=DIRECT, dry_run=False, worker_id="direct-1")

    assert result["outcome"] == "retryable_failure"
    assert tuple(conn.execute("SELECT status,outcome,attempt_count FROM postings WHERE posting_id='one'").fetchone()) == ("ready", "retryable_failure", 1)
    rec = attempt(conn)
    assert rec["outcome"] == "retryable_failure"
    assert rec["click_attempted"] == 0


def test_executor_adapter_exception_fails_closed_to_manual_uncertain(tmp_path, monkeypatch) -> None:
    conn, row = ready_row(tmp_path, ats="greenhouse")
    monkeypatch.setattr("submission.executor._posting_dead", lambda url: False)

    def launch_error(payload):
        raise RuntimeError("browser launch died after unknown click state")

    monkeypatch.setattr("submission.executor.run_adapter", launch_error)
    notices: list[tuple[str, str]] = []
    monkeypatch.setattr("submission.executor._send_notice", lambda subject, body: notices.append((subject, body)) or True)

    result = execute_claimed_posting(conn, row, lane=DIRECT, dry_run=False, worker_id="direct-1")

    assert result["outcome"] == "manual"
    assert result["submission_uncertain"] is True
    assert result["click_attempted"] is True
    assert "adapter launch failed" in result["reason"]
    assert "verify possible prior submission" in result["reason"]
    assert tuple(conn.execute("SELECT status,outcome,attempt_count FROM postings WHERE posting_id='one'").fetchone()) == ("manual", "manual", 1)
    assert notices and "verify possible submission" in notices[0][0]
    rec = attempt(conn)
    assert rec["outcome"] == "manual"
    assert rec["click_attempted"] == 1
    assert rec["confirmation_observed"] == 0


def test_executor_quarantines_uncertain_after_click_and_never_retries(tmp_path, monkeypatch) -> None:
    conn, row = ready_row(tmp_path, ats="greenhouse")
    monkeypatch.setattr("submission.executor._posting_dead", lambda url: False)
    monkeypatch.setattr("submission.executor.run_adapter", lambda payload: {
        "outcome": "retryable_failure",
        "ok": False,
        "submitted": False,
        "retryable": True,
        "detected_ats": "greenhouse",
        "reason": "confirmation missing",
        "click_attempted": True,
    })
    notices: list[tuple[str, str]] = []
    monkeypatch.setattr("submission.executor._send_notice", lambda subject, body: notices.append((subject, body)) or True)

    result = execute_claimed_posting(conn, row, lane=DIRECT, dry_run=False, worker_id="direct-1")

    assert result["outcome"] == "manual"
    assert result["submission_uncertain"] is True
    assert "verify possible prior submission" in result["reason"]
    assert tuple(conn.execute("SELECT status,outcome,attempt_count FROM postings WHERE posting_id='one'").fetchone()) == ("manual", "manual", 1)
    assert notices and "verify possible submission" in notices[0][0]
    rec = attempt(conn)
    assert rec["outcome"] == "manual"
    assert rec["click_attempted"] == 1
    assert rec["confirmation_observed"] == 0


def test_executor_records_definitive_rejection_without_uncertain_notice(tmp_path, monkeypatch) -> None:
    conn, row = ready_row(tmp_path, ats="ashby")
    monkeypatch.setattr("submission.executor._posting_dead", lambda url: False)
    monkeypatch.setattr("submission.executor.run_adapter", lambda payload: {
        "outcome": "manual",
        "ok": False,
        "submitted": False,
        "retryable": False,
        "detected_ats": "ashby",
        "reason": "Ashby rejected the submission as possible spam; retry manually from a trusted browser and network",
        "click_attempted": True,
        "definitive_rejection": True,
        "submission_uncertain": False,
    })
    notice = pytest.fail
    monkeypatch.setattr("submission.executor._send_notice", lambda *args, **kwargs: notice("notice should not be sent"))

    result = execute_claimed_posting(conn, row, lane=ASHBY, dry_run=False, worker_id="ashby-1")

    assert result["outcome"] == "manual"
    assert result.get("submission_uncertain") is False
    assert conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 0
    assert tuple(conn.execute("SELECT status,outcome,attempt_count FROM postings WHERE posting_id='one'").fetchone()) == ("manual", "manual", 1)
    rec = attempt(conn)
    assert rec["ats"] == "ashby"
    assert rec["outcome"] == "manual"
    assert rec["click_attempted"] == 1


def test_executor_marks_stale_posting_before_adapter_without_attempt(tmp_path, monkeypatch) -> None:
    conn, row = ready_row(tmp_path, ats="greenhouse")
    monkeypatch.setattr("submission.executor._posting_dead", lambda url: True)
    monkeypatch.setattr("submission.executor.run_adapter", lambda payload: pytest.fail("adapter should not run"))

    result = execute_claimed_posting(conn, row, lane=DIRECT, dry_run=False, worker_id="direct-1")

    assert result["outcome"] == "stale"
    assert tuple(conn.execute("SELECT status,outcome,attempt_count FROM postings WHERE posting_id='one'").fetchone()) == ("filtered_out", "stale", 1)
    assert conn.execute("SELECT COUNT(*) FROM submission_attempts").fetchone()[0] == 0


def test_executor_quality_gate_uses_normalized_outcome_without_attempt(tmp_path, monkeypatch) -> None:
    conn, row = ready_row(tmp_path, ats="greenhouse")
    monkeypatch.setattr("submission.executor._resume_quality_ready", lambda pdf, posting_id: (False, "metadata missing"))
    monkeypatch.setattr("submission.executor.run_adapter", lambda payload: pytest.fail("adapter should not run"))

    result = execute_claimed_posting(conn, row, lane=DIRECT, dry_run=False, worker_id="direct-1")

    assert result["outcome"] == "manual"
    assert result["reason"] == "resume quality gate: metadata missing"
    assert tuple(
        conn.execute("SELECT status,outcome,last_error,attempt_count FROM postings WHERE posting_id='one'").fetchone()
    ) == ("manual", "manual", "resume quality gate: metadata missing", 1)
    assert conn.execute("SELECT COUNT(*) FROM submission_attempts").fetchone()[0] == 0


def test_executor_claim_lost_after_confirmed_submit_finishes_attempt_as_manual(tmp_path, monkeypatch) -> None:
    conn, row = ready_row(tmp_path, ats="greenhouse")
    monkeypatch.setattr("submission.executor._posting_dead", lambda url: False)

    def adapter(payload):
        conn.execute(
            "UPDATE postings SET status='manual', outcome='manual', last_error='claimed elsewhere' WHERE posting_id='one'"
        )
        conn.commit()
        return {
            "outcome": "submitted",
            "ok": True,
            "submitted": True,
            "detected_ats": "greenhouse",
            "reason": "confirmed after lost claim",
            "click_attempted": True,
        }

    monkeypatch.setattr("submission.executor.run_adapter", adapter)

    result = execute_claimed_posting(conn, row, lane=DIRECT, dry_run=False, worker_id="direct-1")

    assert result["outcome"] == "manual"
    assert result["reason"] == "submission claim lost; verify"
    rec = attempt(conn)
    assert rec["outcome"] == "manual"
    assert rec["confirmation_observed"] == 0
    assert conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 0


def test_executor_dry_run_rechecks_ready_status_before_adapter(tmp_path, monkeypatch) -> None:
    conn, row = ready_row(tmp_path, ats="greenhouse")
    conn.execute("UPDATE postings SET status='submitted', outcome='submitted' WHERE posting_id='one'")
    conn.commit()
    monkeypatch.setattr("submission.executor._posting_dead", lambda url: False)
    monkeypatch.setattr("submission.executor.run_adapter", lambda payload: pytest.fail("adapter should not run"))

    result = execute_claimed_posting(conn, row, lane=DIRECT, dry_run=True, worker_id="direct-1")

    assert result == {
        "company": "Example",
        "ats": "unknown",
        "outcome": "skipped",
        "reason": "dry-run row no longer ready",
    }
    assert conn.execute("SELECT COUNT(*) FROM submission_attempts").fetchone()[0] == 0
