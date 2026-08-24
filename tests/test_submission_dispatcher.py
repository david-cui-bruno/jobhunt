from __future__ import annotations

import sqlite3
import sys
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


def test_workday_tenant_key_is_host_and_site():
    from submission.workday_tenant import workday_tenant_key

    assert (
        workday_tenant_key("https://acme.wd5.myworkdayjobs.com/en-US/External/job/NYC/Role_R123")
        == "acme.wd5.myworkdayjobs.com/external"
    )


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://acme.wd5.myworkdayjobs.com/External/job/NYC/Role_R123", "acme.wd5.myworkdayjobs.com/external"),
        ("https://acme.wd5.myworkdayjobs.com/en-US/Internal/job/NYC/Role_R123", "acme.wd5.myworkdayjobs.com/internal"),
        ("https://acme.wd5.myworkdayjobs.com/en-US/External/job/A_R1", "acme.wd5.myworkdayjobs.com/external"),
        ("https://acme.wd5.myworkdayjobs.com/en-US/Jobs/job/B_R2", "acme.wd5.myworkdayjobs.com/jobs"),
        ("https://boards.greenhouse.io/acme/jobs/1", ""),
        ("not a url", ""),
    ],
)
def test_workday_tenant_key_covers_url_shapes(url: str, expected: str) -> None:
    from submission.workday_tenant import workday_tenant_key

    assert workday_tenant_key(url) == expected


def test_selection_key_returns_workday_tenant_only():
    from submission.dispatcher import selection_key
    from submission.lanes import DIRECT, WORKDAY

    assert (
        selection_key(WORKDAY, "https://acme.wd5.myworkdayjobs.com/en-US/External/job/A_R1")
        == "acme.wd5.myworkdayjobs.com/external"
    )
    assert selection_key(DIRECT, "https://boards.greenhouse.io/acme/jobs/1") is None


def test_workday_cycle_never_selects_same_tenant_twice(dispatch_db):
    from submission.dispatcher import select_for_lane
    from submission.lanes import WORKDAY

    seed_ready(dispatch_db, "a", "https://acme.wd5.myworkdayjobs.com/en-US/External/job/A_R1")
    seed_ready(dispatch_db, "b", "https://acme.wd5.myworkdayjobs.com/en-US/External/job/B_R2")
    seed_ready(dispatch_db, "c", "https://other.wd5.myworkdayjobs.com/en-US/Jobs/job/C_R3")

    assert select_for_lane(dispatch_db, WORKDAY, limit=4) == ["a", "c"]


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


def test_workday_lane_uses_two_workers_for_four_distinct_tenants(dispatch_db, tmp_path, monkeypatch) -> None:
    from submission.dispatcher import dispatch_cycle, select_for_lane
    from submission.lanes import ASHBY, DIRECT, WORKDAY

    active = 0
    peak = 0
    attempted: list[str] = []
    lock = threading.Lock()
    two_workers_active = threading.Event()

    def fake_execute(posting_id, lane, **kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            attempted.append(posting_id)
            if active == 2:
                two_workers_active.set()
        two_workers_active.wait(timeout=0.2)
        with lock:
            active -= 1
        return {"outcome": "submitted"}

    monkeypatch.setattr("submission.dispatcher.execute", fake_execute)
    for idx, tenant in enumerate(("acme", "bravo", "charlie", "delta")):
        seed_ready(
            dispatch_db,
            f"wd-{idx}",
            f"https://{tenant}.wd5.myworkdayjobs.com/en-US/External/job/Role_R{idx}",
        )

    results = dispatch_cycle(dispatch_db)

    assert WORKDAY.concurrency == 2
    assert WORKDAY.attempts_per_cycle == 4
    assert DIRECT.concurrency == 2
    assert DIRECT.attempts_per_cycle == 8
    assert ASHBY.concurrency == 1
    assert ASHBY.attempts_per_cycle == 1
    assert peak == 2
    assert sorted(attempted) == ["wd-0", "wd-1", "wd-2", "wd-3"]
    assert sorted(result["posting_id"] for result in results) == ["wd-0", "wd-1", "wd-2", "wd-3"]

    same_tenant_db = tmp_path / "same-tenant.db"
    _create_dispatch_db(same_tenant_db)
    seed_ready(same_tenant_db, "wd-same-1", "https://acme.wd5.myworkdayjobs.com/en-US/External/job/Same_R1")
    seed_ready(same_tenant_db, "wd-same-2", "https://acme.wd5.myworkdayjobs.com/en-US/External/job/Same_R2")

    assert select_for_lane(same_tenant_db, WORKDAY, limit=4) == ["wd-same-1"]


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
    seed_ready(dispatch_db, "wd-2", "https://acme.wd1.myworkdayjobs.com/jobs/job/2")
    seed_ready(dispatch_db, "wd-3", "https://other.wd1.myworkdayjobs.com/jobs/job/3")

    worker_calls = []

    def fake_execute_claimed_posting(conn, row, *, lane, **kwargs):
        worker_calls.append((row["posting_id"], lane.name, conn))
        if lane.name == "direct":
            raise RuntimeError("direct boom")
        return {"outcome": "submitted", "posting_id": row["posting_id"], "lane": lane.name}

    monkeypatch.setattr("submission.dispatcher.execute_claimed_posting", fake_execute_claimed_posting)

    results = sorted(dispatch_cycle(dispatch_db), key=lambda result: result["posting_id"])

    assert [result["posting_id"] for result in results] == ["gh-1", "wd-1", "wd-3"]
    assert results[0]["outcome"] == "failed"
    assert "direct boom" in results[0]["reason"]
    assert results[1]["outcome"] == "submitted"
    assert results[2]["outcome"] == "submitted"
    assert sorted((posting_id, lane) for posting_id, lane, _conn in worker_calls) == [
        ("gh-1", "direct"),
        ("wd-1", "workday"),
        ("wd-3", "workday"),
    ]
    assert len({id(conn) for _posting_id, _lane, conn in worker_calls}) == len(worker_calls)


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


def test_daemon_rejects_dry_run_without_once_before_dispatch(monkeypatch) -> None:
    import submit_daemon

    calls = []
    monkeypatch.setattr(sys, "argv", ["submit_daemon.py", "--dry-run"])
    monkeypatch.setattr(submit_daemon, "run_forever", lambda: calls.append("forever"))
    monkeypatch.setattr(submit_daemon, "dispatch_cycle", lambda **kwargs: calls.append("cycle") or [])

    with pytest.raises(SystemExit) as excinfo:
        submit_daemon.main()

    assert excinfo.value.code != 0
    assert calls == []


def test_daemon_allows_once_dry_run(monkeypatch) -> None:
    import submit_daemon

    calls = []
    monkeypatch.setattr(sys, "argv", ["submit_daemon.py", "--once", "--dry-run"])
    monkeypatch.setattr(submit_daemon, "dispatch_cycle", lambda **kwargs: calls.append(kwargs) or [])

    assert submit_daemon.main() == 0
    assert calls == [{"dry_run": True}]


def test_claimed_row_missing_is_released_without_attempt_or_uncertainty(dispatch_db) -> None:
    from submission.dispatcher import execute
    from submission.lanes import DIRECT

    conn = sqlite3.connect(dispatch_db)
    conn.execute(
        "INSERT INTO postings (posting_id, company, title, status, url) VALUES (?,?,?,?,?)",
        ("orphan", "Orphan", "Engineer", "ready", "https://boards.greenhouse.io/acme/jobs/orphan"),
    )
    conn.commit()
    conn.close()

    result = execute("orphan", DIRECT, db_path=dispatch_db, dry_run=False)

    assert result["outcome"] == "manual"
    assert result["reason"] == "claimed row missing"
    conn = sqlite3.connect(dispatch_db)
    assert tuple(
        conn.execute("SELECT status,outcome,last_error,attempt_count FROM postings WHERE posting_id='orphan'").fetchone()
    ) == ("manual", "manual", "claimed row missing", 1)
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='submission_attempts'").fetchone() == (
        "submission_attempts",
    )
    conn.close()
