from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from submission.ashby_policy import INTERVAL_MINUTES, ensure_lane_state, load_state, record_result
from submission.attempts import ensure_submission_attempts, start_attempt


def create_db(path: Path) -> None:
    conn = sqlite3.connect(path)
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
    ensure_lane_state(conn)
    ensure_submission_attempts(conn)
    conn.commit()
    conn.close()


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "tracker.db"
    create_db(path)
    return path


def quality_pdf(tmp_path: Path, posting_id: str) -> str:
    pdf = tmp_path / f"{posting_id}.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    pdf.with_suffix(".quality.json").write_text(json.dumps({
        "posting_id": posting_id,
        "source": "deterministic_grounded",
        "review_required": False,
        "structural_validation": "passed",
        "page_count": 1,
    }))
    return str(pdf)


def seed(path: Path, tmp_path: Path, posting_id: str, *, url: str | None = None, status: str = "ready", quality: bool = True) -> None:
    conn = sqlite3.connect(path)
    resume = quality_pdf(tmp_path, posting_id) if quality else str(tmp_path / f"missing-{posting_id}.pdf")
    conn.execute(
        "INSERT INTO postings (posting_id, company, title, status, url) VALUES (?,?,?,?,?)",
        (posting_id, f"Company {posting_id}", "Engineer", status, url or f"https://jobs.ashbyhq.com/acme/{posting_id}"),
    )
    conn.execute("INSERT INTO emails VALUES (?,?)", (posting_id, resume))
    conn.commit()
    conn.close()


def enable_due(path: Path, now: int = 1000) -> None:
    conn = sqlite3.connect(path)
    conn.execute("UPDATE ats_lane_state SET enabled=1,next_attempt_at=?,blocked_until=0 WHERE ats='ashby'", (now,))
    conn.commit()
    conn.close()


def test_eligible_ashby_posting_obeys_policy_and_exclusions(db: Path, tmp_path: Path) -> None:
    from submission.dispatcher import eligible_ashby_posting

    seed(db, tmp_path, "old")
    seed(db, tmp_path, "new")
    assert eligible_ashby_posting(sqlite3.connect(db), now=1000) is None

    enable_due(db, now=2000)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE ats_lane_state SET next_attempt_at=3000 WHERE ats='ashby'")
    conn.commit()
    assert eligible_ashby_posting(conn, now=2000) is None
    conn.execute("UPDATE ats_lane_state SET next_attempt_at=1000 WHERE ats='ashby'")
    conn.execute("INSERT INTO submission_attempts (attempt_id,posting_id,ats,lane,worker_id,browser_mode,policy_revision,started_at,finished_at,outcome,reason_code,raw_reason) VALUES ('a1','old','ashby','ashby','w','b','p',1,2,'manual','manual','needs answers')")
    conn.execute("INSERT INTO applications VALUES ('new','r','ashby',1,'c','n')")
    conn.commit()
    assert eligible_ashby_posting(conn, now=2000) is None
    conn.close()


def test_eligible_ashby_posting_selects_oldest_of_multiple_due_candidates(db: Path, tmp_path: Path) -> None:
    from submission.dispatcher import eligible_ashby_posting

    seed(db, tmp_path, "newer")
    seed(db, tmp_path, "older")
    conn = sqlite3.connect(db)
    conn.execute("UPDATE postings SET last_attempt_at=200 WHERE posting_id='newer'")
    conn.execute("UPDATE postings SET last_attempt_at=100 WHERE posting_id='older'")
    conn.commit()
    conn.close()
    enable_due(db, now=0)

    conn = sqlite3.connect(db)
    assert eligible_ashby_posting(conn, now=1000) == "older"
    conn.close()

    seed(db, tmp_path, "bad-quality", quality=False)
    seed(db, tmp_path, "eligible")
    conn = sqlite3.connect(db)
    assert eligible_ashby_posting(conn, now=2000) == "eligible"
    conn.close()


def test_dispatch_cycle_attempts_one_ashby_and_records_only_finished_exact_attempt(db: Path, tmp_path: Path, monkeypatch) -> None:
    from submission.dispatcher import dispatch_cycle

    seed(db, tmp_path, "ashby-1")
    seed(db, tmp_path, "ashby-2")
    seed(db, tmp_path, "gh", url="https://boards.greenhouse.io/acme/jobs/1")
    enable_due(db, now=0)
    launched = []

    def fake_adapter(payload):
        if "ashbyhq.com" in payload["url"]:
            launched.append(payload["url"])
        return {"submitted": True, "outcome": "submitted", "reason": "confirmed", "detected_ats": "ashby", "click_attempted": True}

    monkeypatch.setattr("submission.executor.run_adapter", fake_adapter)
    monkeypatch.setattr("submission.executor._posting_dead", lambda url: False)
    monkeypatch.setattr("submission.dispatcher.time.time", lambda: 1000)
    monkeypatch.setattr("submission.executor.time.time", lambda: 1000)

    results = dispatch_cycle(db)

    assert len([r for r in results if r["lane"] == "ashby"]) == 1
    assert len(launched) == 1
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT attempt_id,finished_at,outcome,raw_reason FROM submission_attempts WHERE posting_id='ashby-1'").fetchone()
    assert row[1:] == (1000, "submitted", "confirmed")
    state = load_state(conn)
    assert state.consecutive_confirmed == 1
    assert state.next_attempt_at == 1000 + INTERVAL_MINUTES[0] * 60
    conn.close()


def test_dispatcher_pauses_on_missing_or_unfinished_attempt_and_keeps_other_lanes(db: Path, tmp_path: Path, monkeypatch) -> None:
    from submission.dispatcher import dispatch_cycle

    seed(db, tmp_path, "ashby-1")
    seed(db, tmp_path, "gh", url="https://boards.greenhouse.io/acme/jobs/1")
    enable_due(db, now=0)

    def fake_execute(posting_id, lane, **kwargs):
        if lane.name == "ashby":
            conn = sqlite3.connect(db)
            ensure_submission_attempts(conn)
            start_attempt(conn, attempt_id="unfinished", posting_id=posting_id, ats="ashby", lane="ashby", worker_id="w", browser_mode="b", policy_revision="p", started_at=1)
            conn.close()
            return {"outcome": "submitted", "reason": "confirmed", "attempt_id": "unfinished"}
        return {"outcome": "submitted", "reason": "confirmed"}

    monkeypatch.setattr("submission.dispatcher.execute", fake_execute)
    monkeypatch.setattr("submission.dispatcher.time.time", lambda: 2000)

    results = dispatch_cycle(db)

    assert {r["posting_id"] for r in results} == {"ashby-1", "gh"}
    conn = sqlite3.connect(db)
    state = load_state(conn)
    assert state.enabled is False
    assert state.consecutive_confirmed == 0
    conn.close()


def test_ashby_worker_result_missing_attempt_id_pauses_fail_closed(db: Path, tmp_path: Path, monkeypatch) -> None:
    from submission.dispatcher import dispatch_cycle

    seed(db, tmp_path, "ashby-1")
    seed(db, tmp_path, "gh", url="https://boards.greenhouse.io/acme/jobs/1")
    enable_due(db, now=0)

    def fake_execute(posting_id, lane, **kwargs):
        if lane.name == "ashby":
            return {"outcome": "failed", "reason": "dispatcher worker failed: RuntimeError: after launch"}
        return {"outcome": "submitted", "reason": "confirmed"}

    monkeypatch.setattr("submission.dispatcher.execute", fake_execute)

    results = dispatch_cycle(db)

    assert {r["posting_id"] for r in results} == {"ashby-1", "gh"}
    conn = sqlite3.connect(db)
    assert load_state(conn).enabled is False
    conn.close()


def test_pre_attempt_requires_structured_marker_not_reason_text(db: Path, tmp_path: Path) -> None:
    from submission.dispatcher import _record_ashby_policy_result

    seed(db, tmp_path, "ashby-1")
    enable_due(db, now=0)

    _record_ashby_policy_result(db, {"posting_id": "ashby-1", "lane": "ashby", "outcome": "manual", "reason": "resume quality gate: drifted"})

    conn = sqlite3.connect(db)
    assert load_state(conn).enabled is False
    conn.close()


def test_structured_pre_attempt_marker_skips_policy_without_attempt_id(db: Path, tmp_path: Path) -> None:
    from submission.dispatcher import _record_ashby_policy_result

    seed(db, tmp_path, "ashby-1")
    enable_due(db, now=0)

    _record_ashby_policy_result(db, {"posting_id": "ashby-1", "lane": "ashby", "outcome": "manual", "reason": "renamed pre-launch exit", "pre_attempt": True})

    conn = sqlite3.connect(db)
    assert load_state(conn).enabled is True
    conn.close()


def test_post_launch_looking_result_cannot_masquerade_as_pre_attempt(db: Path, tmp_path: Path) -> None:
    from submission.dispatcher import _record_ashby_policy_result

    seed(db, tmp_path, "ashby-1")
    enable_due(db, now=0)

    _record_ashby_policy_result(db, {"posting_id": "ashby-1", "lane": "ashby", "outcome": "failed", "reason": "adapter launch failed after browser start", "click_attempted": True, "pre_attempt": True})

    conn = sqlite3.connect(db)
    assert load_state(conn).enabled is False
    conn.close()


def test_submitted_result_cannot_masquerade_as_pre_attempt(db: Path, tmp_path: Path) -> None:
    from submission.dispatcher import _record_ashby_policy_result

    seed(db, tmp_path, "ashby-1")
    enable_due(db, now=0)

    _record_ashby_policy_result(db, {"posting_id": "ashby-1", "lane": "ashby", "outcome": "submitted", "reason": "confirmed", "pre_attempt": True})

    conn = sqlite3.connect(db)
    assert load_state(conn).enabled is False
    conn.close()


def test_claim_loss_quality_stale_missing_row_and_dry_run_do_not_update_policy(db: Path, tmp_path: Path, monkeypatch) -> None:
    from submission.dispatcher import _record_ashby_policy_result, dispatch_cycle

    seed(db, tmp_path, "ashby-1")
    enable_due(db, now=0)
    for result in (
        {"posting_id": "ashby-1", "lane": "ashby", "outcome": "skipped", "reason": "claim lost", "pre_attempt": True},
        {"posting_id": "ashby-1", "lane": "ashby", "outcome": "manual", "reason": "claimed row missing", "pre_attempt": True},
        {"posting_id": "ashby-1", "lane": "ashby", "outcome": "manual", "reason": "resume quality gate: missing", "pre_attempt": True},
        {"posting_id": "ashby-1", "lane": "ashby", "outcome": "stale", "reason": "liveness check marked posting stale", "pre_attempt": True},
    ):
        _record_ashby_policy_result(db, result)
        conn = sqlite3.connect(db)
        assert load_state(conn).enabled is True
        conn.close()

    called = []
    monkeypatch.setattr("submission.dispatcher._record_ashby_policy_result", lambda *args: called.append(args))
    monkeypatch.setattr(
        "submission.dispatcher.execute",
        lambda posting_id, lane, **kwargs: {
            "posting_id": posting_id,
            "lane": lane.name,
            "outcome": "skipped",
            "reason": "dry run",
            "pre_attempt": True,
        },
    )
    dispatch_cycle(db, dry_run=True)
    assert called == []


def test_adapter_exception_has_attempt_id_and_pauses_ashby(db: Path, tmp_path: Path, monkeypatch) -> None:
    from submission.dispatcher import dispatch_cycle

    seed(db, tmp_path, "ashby-1")
    enable_due(db, now=0)
    monkeypatch.setattr("submission.executor._posting_dead", lambda url: False)
    monkeypatch.setattr("submission.executor.run_adapter", lambda payload: (_ for _ in ()).throw(RuntimeError("boom")))

    [result] = dispatch_cycle(db)

    assert result["attempt_id"]
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT finished_at FROM submission_attempts WHERE attempt_id=?", (result["attempt_id"],)).fetchone()[0]
    assert load_state(conn).enabled is False
    conn.close()


def test_manage_lanes_json_and_nonexecuting(db: Path, tmp_path: Path) -> None:
    seed(db, tmp_path, "ashby-1")
    status = subprocess.run([sys.executable, "manage_lanes.py", "--db", str(db), "status", "ashby"], check=True, text=True, capture_output=True)
    data = json.loads(status.stdout)
    assert data["ats"] == "ashby"
    assert data["ready_depth"] == 1
    assert data["eligible_posting_id"] is None
    no_candidate = json.loads(subprocess.run([sys.executable, "manage_lanes.py", "--db", str(db), "preview", "ashby"], check=True, text=True, capture_output=True).stdout)
    assert no_candidate["candidate"] is None
    assert no_candidate["reason"]["code"] == "policy_not_due"

    subprocess.run([sys.executable, "manage_lanes.py", "--db", str(db), "enable-canary", "ashby"], check=True, text=True, capture_output=True)
    preview = json.loads(subprocess.run([sys.executable, "manage_lanes.py", "--db", str(db), "preview", "ashby"], check=True, text=True, capture_output=True).stdout)
    assert preview["posting_id"] == "ashby-1"
    subprocess.run([sys.executable, "manage_lanes.py", "--db", str(db), "pause", "ashby"], check=True, text=True, capture_output=True)
    conn = sqlite3.connect(db)
    assert load_state(conn).enabled is False
    assert conn.execute("SELECT status FROM postings WHERE posting_id='ashby-1'").fetchone()[0] == "ready"
    conn.close()


def test_manage_lanes_preview_reports_missing_candidate_detail(db: Path, tmp_path: Path, monkeypatch) -> None:
    import manage_lanes

    seed(db, tmp_path, "ashby-1")
    enable_due(db, now=0)
    monkeypatch.setattr(manage_lanes, "eligible_ashby_posting", lambda conn, now: "ashby-1")
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM emails WHERE posting_id='ashby-1'")
    conn.commit()
    conn.row_factory = sqlite3.Row

    try:
        preview = manage_lanes._preview(conn)
    finally:
        conn.close()

    assert preview == {"ats": "ashby", "candidate": None, "reason": {"code": "candidate_detail_missing", "posting_id": "ashby-1"}}


def test_legacy_submit_ready_skips_ashby_without_adapter(db: Path, tmp_path: Path, monkeypatch) -> None:
    import submit

    seed(db, tmp_path, "ashby-1")
    monkeypatch.setattr(submit, "DB", db)
    monkeypatch.setattr(submit, "_posting_already_applied", lambda *args: False)
    monkeypatch.setattr("submission.executor.execute_claimed_posting", lambda *args, **kwargs: pytest.fail("ashby adapter bypassed policy"))

    assert submit.submit_ready(limit=1, dry_run=False) == []
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT status FROM postings WHERE posting_id='ashby-1'").fetchone()[0] == "ready"
    conn.close()


def test_sprint_skips_ashby_before_tailoring_or_claiming(db: Path, tmp_path: Path, monkeypatch) -> None:
    import sprint

    seed(db, tmp_path, "ashby-1", status="queued")
    monkeypatch.setattr(sprint, "DB", db)
    monkeypatch.setattr("watcher.watch.run", lambda: {"new": [{"posting_id": "ashby-1"}]})
    monkeypatch.setattr("watcher.filter.run", lambda **kwargs: None)
    monkeypatch.setattr("drip.claim_posting", lambda *args, **kwargs: pytest.fail("claimed ashby in sprint"))

    assert sprint.run() == []
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT status FROM postings WHERE posting_id='ashby-1'").fetchone()[0] == "queued"
    conn.close()
