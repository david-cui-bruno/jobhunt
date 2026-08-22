import sqlite3

import pytest

from submission.attempts import ensure_submission_attempts
from submission.metrics import attempt_metrics, queue_metrics


@pytest.fixture
def metrics_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_submission_attempts(conn)
    conn.execute(
        "CREATE TABLE postings (posting_id TEXT PRIMARY KEY, url TEXT, status TEXT)"
    )
    yield conn
    conn.close()


def seed_attempt(conn, *, ats, outcome, duration_ms, started_at=10, confirmation_observed=None):
    confirmed = int(outcome == "submitted") if confirmation_observed is None else int(confirmation_observed)
    conn.execute(
        "INSERT INTO submission_attempts "
        "(attempt_id, posting_id, ats, lane, worker_id, browser_mode, policy_revision, "
        "started_at, finished_at, duration_ms, outcome, reason_code, raw_reason, "
        "click_attempted, confirmation_observed) "
        "VALUES (?, ?, ?, ?, 'worker', 'headless', 'test', ?, ?, ?, ?, '', '', 1, ?)",
        (f"{ats}-{outcome}-{duration_ms}-{started_at}", f"p-{ats}-{duration_ms}", ats,
         "direct", started_at, started_at + 1, duration_ms, outcome, confirmed),
    )
    conn.commit()


def test_attempt_metrics_group_by_ats_and_outcome(metrics_db) -> None:
    seed_attempt(metrics_db, ats="greenhouse", outcome="submitted", duration_ms=1000)
    seed_attempt(metrics_db, ats="greenhouse", outcome="manual", duration_ms=3000)

    rows = attempt_metrics(metrics_db, since=0)

    greenhouse = next(row for row in rows if row["ats"] == "greenhouse")
    assert greenhouse["attempts"] == 2
    assert greenhouse["confirmed"] == 1
    assert greenhouse["confirmation_rate"] == 0.5
    assert greenhouse["manual"] == 1
    assert greenhouse["p50_duration_ms"] == 2000
    assert greenhouse["p95_duration_ms"] == 3000


def test_attempt_metrics_requires_observed_confirmation_for_submitted(metrics_db) -> None:
    seed_attempt(
        metrics_db,
        ats="greenhouse",
        outcome="submitted",
        duration_ms=1000,
        confirmation_observed=0,
    )
    seed_attempt(
        metrics_db,
        ats="greenhouse",
        outcome="submitted",
        duration_ms=2000,
        confirmation_observed=1,
    )

    rows = attempt_metrics(metrics_db, since=0)

    greenhouse = next(row for row in rows if row["ats"] == "greenhouse")
    assert greenhouse["attempts"] == 2
    assert greenhouse["submitted"] == 2
    assert greenhouse["confirmed"] == 1
    assert greenhouse["confirmation_rate"] == 0.5


def test_attempt_metrics_normalizes_unknown_ats(metrics_db) -> None:
    seed_attempt(metrics_db, ats="", outcome="failed", duration_ms=500)
    seed_attempt(metrics_db, ats="unknownvendor", outcome="submitted", duration_ms=700)

    rows = attempt_metrics(metrics_db, since=0)

    other = next(row for row in rows if row["ats"] == "other")
    assert other["attempts"] == 2
    assert other["confirmed"] == 1
    assert other["failed"] == 1


def test_queue_metrics_uses_shared_lane_classification(metrics_db) -> None:
    metrics_db.executemany(
        "INSERT INTO postings (posting_id, url, status) VALUES (?, ?, ?)",
        [
            ("g1", "https://boards.greenhouse.io/acme/jobs/1", "ready"),
            ("l1", "https://jobs.lever.co/acme/1", "queued"),
            ("w1", "https://acme.wd1.myworkdayjobs.com/jobs/job/1", "ready"),
            ("a1", "https://jobs.ashbyhq.com/acme/1", "manual"),
            ("x1", "https://example.com/jobs/1", "queued"),
            ("done", "https://boards.greenhouse.io/acme/jobs/2", "submitted"),
        ],
    )
    metrics_db.commit()

    rows = {row["lane"]: row for row in queue_metrics(metrics_db)}

    assert rows["direct"]["depth"] == 2
    assert rows["direct"]["automatic"] is True
    assert rows["workday"]["depth"] == 1
    assert rows["ashby"]["depth"] == 1
    assert rows["unsupported"]["depth"] == 1
