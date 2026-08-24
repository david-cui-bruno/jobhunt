import sqlite3
import sys
import types

import drip
from submission.lanes import (
    classify_url,
    preparation_destination,
    quarantine_unsupported,
    reconcile_nonautomatic_ready,
)


import pytest


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE postings (posting_id TEXT PRIMARY KEY, status TEXT, url TEXT, "
        "last_attempt_at INTEGER, outcome TEXT, last_error TEXT)"
    )
    try:
        yield connection
    finally:
        connection.close()


def test_manual_lane_is_preparable_but_not_automatic():
    ats, lane = classify_url("https://jobs.smartrecruiters.com/acme/1")
    assert ats == "smartrecruiters"
    assert lane.preparable is True
    assert lane.automatic is False


def test_reconcile_nonautomatic_ready_moves_smartrecruiters_to_manual(conn):
    conn.execute(
        "INSERT INTO postings(posting_id,status,url) VALUES (?,?,?)",
        ("sr-1", "ready", "https://jobs.smartrecruiters.com/acme/1"),
    )
    conn.commit()
    assert reconcile_nonautomatic_ready(conn) == 1
    assert conn.execute(
        "SELECT status,outcome,last_error FROM postings WHERE posting_id='sr-1'"
    ).fetchone() == (
        "manual",
        "manual",
        "prepared for manual completion: smartrecruiters",
    )


def test_lane_mapping_is_explicit() -> None:
    assert classify_url("https://boards.greenhouse.io/acme/jobs/1")[1].name == "direct"
    assert classify_url("https://acme.wd1.myworkdayjobs.com/job/1")[1].name == "workday"
    assert classify_url("https://jobs.ashbyhq.com/acme/id")[1].name == "ashby"
    assert classify_url("https://jobs.smartrecruiters.com/acme/1")[1].automatic is False
    assert classify_url("https://example.com/careers/1")[1].name == "unsupported"


def test_owned_ready_flow_lanes_are_explicit() -> None:
    _ats, email_lane = classify_url("https://news.ycombinator.com/item?id=123")
    _ats, waas_lane = classify_url("https://www.workatastartup.com/jobs/123/example-engineer")
    _ats, smartrecruiters_lane = classify_url("https://jobs.smartrecruiters.com/acme/1")

    assert email_lane.owns_ready_flow is True
    assert waas_lane.owns_ready_flow is True
    assert smartrecruiters_lane.owns_ready_flow is False


def test_hn_and_waas_sources_are_not_classified_as_unsupported() -> None:
    assert classify_url("https://news.ycombinator.com/item?id=123")[1].name == "email"
    assert classify_url("mailto:jobs@example.com")[1].name == "email"
    assert classify_url("https://www.workatastartup.com/jobs/123/example-engineer")[1].name == "waas"


def test_owned_ready_flow_destinations_remain_ready(conn):
    assert preparation_destination(conn, "https://news.ycombinator.com/item?id=123") == ("ready", None)
    assert preparation_destination(conn, "https://www.workatastartup.com/jobs/123/example-engineer") == (
        "ready",
        None,
    )


def test_reconcile_nonautomatic_ready_preserves_owned_ready_flows(conn):
    conn.executemany(
        "INSERT INTO postings(posting_id,status,url) VALUES (?,?,?)",
        [
            ("hn-1", "ready", "https://news.ycombinator.com/item?id=123"),
            ("waas-1", "ready", "https://www.workatastartup.com/jobs/123/example-engineer"),
            ("sr-1", "ready", "https://jobs.smartrecruiters.com/acme/1"),
            ("ashby-1", "ready", "https://jobs.ashbyhq.com/acme/id"),
        ],
    )
    conn.commit()

    assert reconcile_nonautomatic_ready(conn) == 2
    rows = {
        row[0]: row[1:]
        for row in conn.execute("SELECT posting_id,status,outcome,last_error FROM postings ORDER BY posting_id")
    }
    assert rows["hn-1"] == ("ready", None, None)
    assert rows["waas-1"] == ("ready", None, None)
    assert rows["sr-1"] == (
        "manual",
        "manual",
        "prepared for manual completion: smartrecruiters",
    )
    assert rows["ashby-1"] == (
        "manual",
        "manual",
        "prepared for manual completion: ashby",
    )


def test_waas_submit_worker_branch_requires_opt_in(monkeypatch) -> None:
    import submit_worker

    fake_waas = types.ModuleType("waas")

    def apply_waas(url: str, slug: str, dry_run: bool = True) -> dict:
        return {"ok": True, "submitted": dry_run, "reason": url}

    fake_waas.apply_waas = apply_waas
    monkeypatch.setitem(sys.modules, "waas", fake_waas)
    monkeypatch.delenv("JOBHUNT_WAAS", raising=False)

    assert submit_worker._adapter("other", "https://www.workatastartup.com/jobs/123/example-engineer") == (
        None,
        False,
        "waas_paused",
        "https://www.workatastartup.com/jobs/123/example-engineer",
    )

    monkeypatch.setenv("JOBHUNT_WAAS", "1")
    fn, waas, detected, target_url = submit_worker._adapter(
        "other", "https://www.workatastartup.com/jobs/123/example-engineer"
    )

    assert fn is apply_waas
    assert waas is True
    assert detected == "waas"
    assert target_url == "https://www.workatastartup.com/jobs/123/example-engineer"


def test_quarantine_unsupported_moves_unknown_queued_row_to_manual() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE postings (posting_id TEXT PRIMARY KEY, status TEXT, url TEXT, "
        "last_attempt_at INTEGER, outcome TEXT, last_error TEXT)"
    )
    conn.executemany(
        "INSERT INTO postings VALUES (?,?,?,?,?,?)",
        [
            ("unknown", "queued", "https://example.com/careers/1", None, None, None),
            ("ashby", "queued", "https://jobs.ashbyhq.com/acme/id", None, None, None),
            ("greenhouse", "queued", "https://boards.greenhouse.io/acme/jobs/1", None, None, None),
            ("hn", "queued", "https://news.ycombinator.com/item?id=123", None, None, None),
            ("waas", "queued", "https://www.workatastartup.com/jobs/123/example-engineer", None, None, None),
        ],
    )

    assert quarantine_unsupported(conn) == 2

    rows = {
        row["posting_id"]: dict(row)
        for row in conn.execute("SELECT * FROM postings ORDER BY posting_id")
    }
    assert rows["unknown"]["status"] == "manual"
    assert rows["unknown"]["outcome"] == "manual"
    assert rows["unknown"]["last_error"] == "no adapter for other"
    assert rows["ashby"]["status"] == "manual"
    assert rows["ashby"]["last_error"] == (
        "ashby automation disabled after spam rejection; apply manually from a trusted browser"
    )
    assert rows["greenhouse"]["status"] == "queued"
    assert rows["greenhouse"]["last_error"] is None
    assert rows["hn"]["status"] == "queued"
    assert rows["waas"]["status"] == "queued"


def test_enabled_ashby_row_can_enter_tailoring() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE postings (posting_id TEXT PRIMARY KEY, status TEXT, url TEXT, "
        "locations TEXT, first_seen INTEGER, last_attempt_at INTEGER, outcome TEXT, last_error TEXT)"
    )
    conn.execute("CREATE TABLE ats_lane_state (ats TEXT PRIMARY KEY, enabled INTEGER)")
    conn.execute("INSERT INTO ats_lane_state VALUES ('ashby', 1)")
    conn.execute(
        "INSERT INTO postings VALUES (?,?,?,?,?,?,?,?)",
        ("ashby", "queued", "https://jobs.ashbyhq.com/acme/id", "NYC", 1, None, None, None),
    )

    assert quarantine_unsupported(conn) == 0
    assert drip.pick_next(conn)["posting_id"] == "ashby"
