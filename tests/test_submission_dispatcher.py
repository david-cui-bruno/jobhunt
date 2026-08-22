from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from submission.database import connect_tracker


def _create_dispatch_db(path: Path) -> None:
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
    conn.commit()
    conn.close()


@pytest.fixture
def dispatch_db(tmp_path: Path) -> Path:
    db = tmp_path / "tracker.db"
    _create_dispatch_db(db)
    return db


def seed_ready(db: Path, posting_id: str, url: str, *, resume_pdf: str | None = None) -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO postings (posting_id, company, title, status, url) VALUES (?,?,?,?,?)",
        (posting_id, f"Company {posting_id}", "Engineer", "ready", url),
    )
    conn.execute("INSERT INTO emails VALUES (?, ?)", (posting_id, resume_pdf or f"/tmp/{posting_id}.pdf"))
    conn.commit()
    conn.close()


def seed_many_greenhouse(db: Path, *, count: int) -> None:
    for idx in range(count):
        seed_ready(db, f"gh-{idx}", f"https://boards.greenhouse.io/acme/jobs/{idx}")


def seed_many_workday(db: Path, *, count: int) -> None:
    for idx in range(count):
        seed_ready(db, f"wd-{idx}", f"https://acme.wd1.myworkdayjobs.com/jobs/job/{idx}")


def test_blocked_ashby_does_not_block_direct_lane(dispatch_db, monkeypatch) -> None:
    from submission.dispatcher import dispatch_cycle

    seed_ready(dispatch_db, "ashby-1", "https://jobs.ashbyhq.com/acme/id")
    seed_ready(dispatch_db, "gh-1", "https://boards.greenhouse.io/acme/jobs/1")
    calls = []
    monkeypatch.setattr(
        "submission.dispatcher.execute",
        lambda posting_id, lane, **kw: calls.append((posting_id, lane.name)) or {"outcome": "submitted"},
    )

    results = dispatch_cycle(dispatch_db, dry_run=False)

    assert ("gh-1", "direct") in calls
    assert all(posting_id != "ashby-1" for posting_id, _ in calls)
    assert [result["posting_id"] for result in results] == ["gh-1"]


def test_direct_lane_never_exceeds_two_workers(dispatch_db, monkeypatch) -> None:
    from submission.dispatcher import dispatch_cycle

    active = 0
    peak = 0
    lock = threading.Lock()

    def fake_execute(*args, **kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return {"outcome": "submitted"}

    monkeypatch.setattr("submission.dispatcher.execute", fake_execute)
    seed_many_greenhouse(dispatch_db, count=6)

    dispatch_cycle(dispatch_db)

    assert peak == 2


def test_workday_lane_never_exceeds_one_worker(dispatch_db, monkeypatch) -> None:
    from submission.dispatcher import dispatch_cycle

    active = 0
    peak = 0
    lock = threading.Lock()

    def fake_execute(*args, **kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return {"outcome": "submitted"}

    monkeypatch.setattr("submission.dispatcher.execute", fake_execute)
    seed_many_workday(dispatch_db, count=4)

    dispatch_cycle(dispatch_db)

    assert peak == 1


def test_each_attempted_failure_consumes_direct_lane_attempt_slot(dispatch_db, monkeypatch) -> None:
    from submission.dispatcher import dispatch_cycle

    attempted = []

    def fake_execute(posting_id, lane, **kwargs):
        attempted.append((posting_id, lane.name))
        return {"outcome": "retryable_failure", "reason": "network"}

    monkeypatch.setattr("submission.dispatcher.execute", fake_execute)
    seed_many_greenhouse(dispatch_db, count=10)

    results = dispatch_cycle(dispatch_db)

    assert len(attempted) == 8
    assert len(results) == 8
    assert all(lane == "direct" for _, lane in attempted)


def test_direct_and_workday_failures_are_isolated(dispatch_db, monkeypatch) -> None:
    from submission.dispatcher import dispatch_cycle

    seed_ready(dispatch_db, "gh-1", "https://boards.greenhouse.io/acme/jobs/1")
    seed_ready(dispatch_db, "wd-1", "https://acme.wd1.myworkdayjobs.com/jobs/job/1")

    def fake_execute(posting_id, lane, **kwargs):
        if lane.name == "direct":
            raise RuntimeError("direct boom")
        return {"outcome": "submitted"}

    monkeypatch.setattr("submission.dispatcher.execute", fake_execute)

    results = sorted(dispatch_cycle(dispatch_db), key=lambda result: result["posting_id"])

    assert [result["posting_id"] for result in results] == ["gh-1", "wd-1"]
    assert results[0]["outcome"] == "failed"
    assert "direct boom" in results[0]["reason"]
    assert results[1]["outcome"] == "submitted"


def test_run_forever_recovers_from_cycle_exception(monkeypatch) -> None:
    from submission.dispatcher import run_forever

    calls = 0
    stop = threading.Event()

    def boom_then_stop(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("cycle boom")
        stop.set()
        return []

    monkeypatch.setattr("submission.dispatcher.dispatch_cycle", boom_then_stop)

    run_forever(poll_seconds=0.0, stop_event=stop)

    assert calls == 2


def test_dispatcher_poll_interval_default_is_30_seconds() -> None:
    import inspect
    from submission.dispatcher import run_forever

    assert inspect.signature(run_forever).parameters["poll_seconds"].default == 30.0
