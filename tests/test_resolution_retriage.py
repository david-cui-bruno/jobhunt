from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import apply.jd
import watcher.abc_startups
import watcher.bigco
import watcher.startups
from submission.identity import canonical_conflict_reason
from submission.database import connect_tracker
from submission.resolutions import cached_resolution, ensure_resolution_schema, record_resolution
from submission.lanes import classify_url
from submission.identity import canonical_posting_key, posting_already_applied
from watcher import watch
from watcher.url_resolver import ResolutionResult


GREENHOUSE_HTML = """
<html><body>
<a class="job-cta-secondary" href="https://job-boards.greenhouse.io/acme/jobs/1234567">
View original posting
</a>
</body></html>
"""


GREENHOUSE_URL = "https://job-boards.greenhouse.io/acme/jobs/1234567"
GREENHOUSE_ALIAS = "https://boards.greenhouse.io/acme/jobs/1234567"
DISTINCT_GREENHOUSE_URL = "https://job-boards.greenhouse.io/acme/jobs/7654321"
SOURCE_URL = "https://www.dreamworkhq.com/job/11111111-1111-1111-1111-111111111111"


def conn() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE postings ("
        "posting_id TEXT PRIMARY KEY, company TEXT, url TEXT, status TEXT, outcome TEXT"
        ")"
    )
    db.execute("CREATE TABLE applications (posting_id TEXT PRIMARY KEY)")
    return db


def seed_posting(db: sqlite3.Connection, posting_id: str, url: str, status: str, outcome: str | None = None) -> None:
    db.execute(
        "INSERT INTO postings (posting_id, company, url, status, outcome) VALUES (?,?,?,?,?)",
        (posting_id, "Acme", url, status, outcome),
    )


def seed_applied(db: sqlite3.Connection, posting_id: str, url: str) -> None:
    seed_posting(db, posting_id, url, "submitted")
    db.execute("INSERT INTO applications (posting_id) VALUES (?)", (posting_id,))


def test_resolution_schema_is_idempotent() -> None:
    db = conn()
    ensure_resolution_schema(db)
    ensure_resolution_schema(db)
    columns = {row[1] for row in db.execute("PRAGMA table_info(posting_url_resolutions)")}
    assert columns == {
        "posting_id",
        "source_url",
        "resolved_url",
        "resolver",
        "resolved_at",
        "last_error",
        "source_hash",
    }


def test_ensure_resolution_schema_preserves_caller_transaction() -> None:
    db = conn()
    db.execute("CREATE TABLE caller_owned (id INTEGER PRIMARY KEY)")
    db.execute("INSERT INTO caller_owned (id) VALUES (1)")

    ensure_resolution_schema(db)

    assert db.in_transaction
    db.rollback()
    assert db.execute("SELECT COUNT(*) FROM caller_owned").fetchone() == (0,)
    assert db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='posting_url_resolutions'"
    ).fetchone() is None


def test_connect_tracker_initializes_resolution_schema(tmp_path: Path) -> None:
    db = connect_tracker(tmp_path / "tracker.db")
    try:
        assert tuple(db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='posting_url_resolutions'"
        ).fetchone()) == (1,)
    finally:
        db.close()


def test_watch_init_db_initializes_resolution_schema_without_committing_caller_transaction() -> None:
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE caller_owned (id INTEGER PRIMARY KEY)")
    db.execute("INSERT INTO caller_owned (id) VALUES (1)")

    watch.init_db(db)

    assert db.in_transaction
    db.rollback()
    assert db.execute("SELECT COUNT(*) FROM caller_owned").fetchone() == (0,)
    assert db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='posting_url_resolutions'"
    ).fetchone() is None


def test_cached_resolution_requires_matching_source_and_target() -> None:
    db = conn()
    ensure_resolution_schema(db)
    result = ResolutionResult(
        source_url=SOURCE_URL,
        resolved_url=GREENHOUSE_URL,
        resolver="dreamwork-original-v1",
        source_hash="a" * 64,
        error="",
    )
    record_resolution(db, result, posting_id="dw-1")

    assert cached_resolution(db, "dw-1", SOURCE_URL) == GREENHOUSE_URL
    assert cached_resolution(db, "dw-1", SOURCE_URL + "?other=1") is None
    assert cached_resolution(db, "other", SOURCE_URL) is None


def test_cached_resolution_ignores_empty_target_and_record_sanitizes_errors() -> None:
    db = conn()
    ensure_resolution_schema(db)
    result = ResolutionResult(
        source_url=SOURCE_URL,
        resolved_url=None,
        resolver="dreamwork-original-v1",
        source_hash="b" * 64,
        error="line one\n" + "x" * 600,
    )
    record_resolution(db, result, posting_id="dw-1")

    assert cached_resolution(db, "dw-1", SOURCE_URL) is None
    row = db.execute(
        "SELECT last_error FROM posting_url_resolutions WHERE posting_id='dw-1'"
    ).fetchone()
    assert "\n" not in row[0]
    assert len(row[0]) == 500


def test_record_resolution_updates_existing_posting_id() -> None:
    db = conn()
    ensure_resolution_schema(db)
    first = ResolutionResult(SOURCE_URL, None, "dreamwork-original-v1", "c" * 64, "missing")
    second = ResolutionResult(SOURCE_URL, GREENHOUSE_URL, "dreamwork-original-v1", "d" * 64, "")

    record_resolution(db, first, posting_id="dw-1")
    record_resolution(db, second, posting_id="dw-1")

    row = db.execute(
        "SELECT source_url, resolved_url, last_error, source_hash FROM posting_url_resolutions WHERE posting_id='dw-1'"
    ).fetchone()
    assert row == (SOURCE_URL, GREENHOUSE_URL, "", "d" * 64)


def test_canonical_conflict_detects_applied_alias() -> None:
    db = conn()
    seed_applied(db, "done", GREENHOUSE_URL)
    assert canonical_conflict_reason(db, "wrapper", GREENHOUSE_ALIAS) == "canonical posting already applied"


def test_canonical_conflict_detects_current_posting_application_even_when_ready() -> None:
    db = conn()
    seed_posting(db, "wrapper", GREENHOUSE_ALIAS, "ready")
    db.execute("INSERT INTO applications (posting_id) VALUES ('wrapper')")

    assert canonical_conflict_reason(db, "wrapper", GREENHOUSE_ALIAS) == "canonical posting already applied"


def test_canonical_conflict_detects_active_submitting_claim() -> None:
    db = conn()
    seed_posting(db, "active", GREENHOUSE_URL, "submitting")
    assert canonical_conflict_reason(db, "wrapper", GREENHOUSE_ALIAS) == "canonical posting already claimed"


def test_canonical_conflict_detects_active_sprinting_claim() -> None:
    db = conn()
    seed_posting(db, "active", GREENHOUSE_URL, "sprinting")
    assert canonical_conflict_reason(db, "wrapper", GREENHOUSE_ALIAS) == "canonical posting already claimed"


def test_canonical_conflict_detects_terminal_stale_alias() -> None:
    db = conn()
    seed_posting(db, "stale", GREENHOUSE_URL, "manual", "stale")
    assert canonical_conflict_reason(db, "wrapper", GREENHOUSE_ALIAS) == "canonical posting already terminal"


def test_canonical_conflict_ignores_current_row_itself() -> None:
    db = conn()
    seed_posting(db, "wrapper", GREENHOUSE_URL, "submitting")
    assert canonical_conflict_reason(db, "wrapper", GREENHOUSE_ALIAS) is None


def test_canonical_conflict_ignores_distinct_ats_id() -> None:
    db = conn()
    seed_applied(db, "done", DISTINCT_GREENHOUSE_URL)
    assert canonical_conflict_reason(db, "wrapper", GREENHOUSE_ALIAS) is None


def test_dreamwork_watcher_reapplies_cached_resolution_before_filter() -> None:
    db = conn()
    ensure_resolution_schema(db)
    seed_posting(db, "dw-1", SOURCE_URL, "manual", "manual")
    record_resolution(
        db,
        ResolutionResult(SOURCE_URL, GREENHOUSE_URL, "dreamwork-original-v1", "e" * 64, ""),
        posting_id="dw-1",
    )

    def fail_fetch(url: str) -> str:
        raise AssertionError(f"unexpected page fetch: {url}")

    result = watch.resolve_dreamwork_postings(db, fetch_page=fail_fetch)

    assert result == {"cached": 1, "resolved": 0, "failed": 0, "conflicts": 0}
    assert db.execute("SELECT url FROM postings WHERE posting_id='dw-1'").fetchone()[0] == GREENHOUSE_URL
    assert db.execute("SELECT status FROM postings WHERE posting_id='dw-1'").fetchone()[0] == "manual"


def test_dreamwork_watcher_resolution_conflict_never_rewrites_or_requeues() -> None:
    db = conn()
    ensure_resolution_schema(db)
    seed_applied(db, "done", GREENHOUSE_URL)
    seed_posting(db, "dw-1", SOURCE_URL, "manual", "manual")

    result = watch.resolve_dreamwork_postings(db, fetch_page=lambda _: GREENHOUSE_HTML)

    assert result["conflicts"] == 1
    assert db.execute("SELECT url FROM postings WHERE posting_id='dw-1'").fetchone()[0] == SOURCE_URL
    assert db.execute("SELECT status FROM postings WHERE posting_id='dw-1'").fetchone()[0] == "manual"


def test_classify_identity_and_duplicate_hot_paths_never_fetch(monkeypatch) -> None:
    invoked = []

    def fail_network(url: str, timeout: int = 25) -> str:
        invoked.append(url)
        raise AssertionError(f"unexpected network fetch: {url}")

    monkeypatch.setattr(apply.jd, "_get", fail_network)
    monkeypatch.setattr(watcher.abc_startups, "_get", fail_network)
    monkeypatch.setattr(watcher.bigco, "_get", fail_network)
    monkeypatch.setattr(watcher.startups, "_get", fail_network)
    monkeypatch.setattr(watch, "_fetch", fail_network)
    db = conn()
    seed_applied(db, "done", GREENHOUSE_URL)

    for url in (SOURCE_URL, GREENHOUSE_URL):
        classify_url(url)
        canonical_posting_key("dw-1", url)
        posting_already_applied(db, "dw-1", url)

    assert invoked == []


def retriage_conn(tmp_path: Path | None = None) -> sqlite3.Connection:
    path = ':memory:' if tmp_path is None else str(tmp_path / 'tracker.db')
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute(
        "CREATE TABLE postings ("
        "posting_id TEXT PRIMARY KEY, company TEXT, title TEXT, url TEXT, status TEXT, "
        "outcome TEXT, last_error TEXT, last_attempt_at INTEGER, first_seen INTEGER"
        ")"
    )
    db.execute("CREATE TABLE applications (posting_id TEXT PRIMARY KEY)")
    db.execute(
        "CREATE TABLE submission_attempts ("
        "attempt_id TEXT PRIMARY KEY, posting_id TEXT NOT NULL, ats TEXT NOT NULL, lane TEXT NOT NULL, "
        "worker_id TEXT NOT NULL, browser_mode TEXT NOT NULL, policy_revision TEXT NOT NULL, "
        "started_at INTEGER NOT NULL, finished_at INTEGER"
        ")"
    )
    ensure_resolution_schema(db)
    return db


def seed_retriage_row(
    db: sqlite3.Connection,
    posting_id: str,
    resolved_url: str,
    *,
    last_error: str = "no adapter for other",
    status: str = "manual",
    outcome: str | None = "manual",
) -> None:
    db.execute(
        "INSERT INTO postings "
        "(posting_id, company, title, url, status, outcome, last_error, last_attempt_at, first_seen) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 100, 50)",
        (posting_id, "Acme", f"Role {posting_id}", SOURCE_URL + '/' + posting_id, status, outcome, last_error),
    )
    record_resolution(
        db,
        ResolutionResult(SOURCE_URL + '/' + posting_id, resolved_url, "dreamwork-original-v1", posting_id[:1] * 64, ""),
        posting_id=posting_id,
    )


def posting_snapshot(db: sqlite3.Connection, posting_id: str) -> tuple:
    return tuple(
        db.execute(
            "SELECT posting_id, company, title, url, status, outcome, last_error, last_attempt_at, first_seen "
            "FROM postings WHERE posting_id=?",
            (posting_id,),
        ).fetchone()
    )


def seed_retriage_fixture(db: sqlite3.Connection) -> list[str]:
    from submission.resolutions import retriage_candidates

    del retriage_candidates
    seed_retriage_row(db, "greenhouse", GREENHOUSE_URL)
    seed_retriage_row(db, "smart", "https://jobs.smartrecruiters.com/acme/123-engineer")
    seed_retriage_row(db, "click", "https://jobs.lever.co/acme/123", last_error="click uncertainty after submit")
    seed_retriage_row(db, "stale", "https://jobs.lever.co/acme/124", outcome="stale")
    seed_retriage_row(db, "finished", "https://jobs.lever.co/acme/125")
    db.execute(
        "INSERT INTO submission_attempts "
        "(attempt_id, posting_id, ats, lane, worker_id, browser_mode, policy_revision, started_at, finished_at) "
        "VALUES ('attempt-finished', 'finished', 'lever', 'direct', 'worker', 'browser', 'v1', 1, 2)"
    )
    seed_retriage_row(db, "alias", "https://jobs.lever.co/acme/applied-alias")
    seed_applied(db, "done", "https://jobs.lever.co/acme/applied-alias")
    seed_retriage_row(db, "nontechnical", "https://jobs.lever.co/acme/126", last_error="needs resume revision")
    return ["click", "stale", "finished", "alias", "nontechnical", "done"]


def test_retriage_candidates_preview_only_safe_resolved_manual_rows() -> None:
    from submission.resolutions import retriage_candidates

    db = retriage_conn()
    seed_retriage_fixture(db)

    candidates = retriage_candidates(db)

    assert [candidate["posting_id"] for candidate in candidates] == ["greenhouse", "smart"]
    assert candidates[0]["destination"] == "ready"
    assert candidates[0]["ats"] == "greenhouse"
    assert candidates[0]["reason"] == "no adapter for other"
    assert candidates[1]["destination"] == "manual"
    assert candidates[1]["ats"] == "smartrecruiters"


def test_apply_retriage_updates_only_selected_safe_rows_and_preserves_manual_smartrecruiters() -> None:
    from submission.resolutions import apply_retriage

    db = retriage_conn()
    excluded = seed_retriage_fixture(db)
    before = {posting_id: posting_snapshot(db, posting_id) for posting_id in excluded}
    db.commit()

    result = apply_retriage(db, ["greenhouse", "smart", "click", "stale", "finished", "alias", "nontechnical"])

    assert result == {"requested": 7, "updated": 2, "skipped": 5}
    assert posting_snapshot(db, "greenhouse")[4:7] == ("queued", None, "")
    assert posting_snapshot(db, "smart")[4:7] == (
        "manual",
        "manual",
        "prepared for manual completion: smartrecruiters",
    )
    assert {posting_id: posting_snapshot(db, posting_id) for posting_id in excluded} == before


def db_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_retriage_cli_preview_json_does_not_mutate_database_hash(tmp_path: Path) -> None:
    db = retriage_conn(tmp_path)
    seed_retriage_fixture(db)
    db.commit()
    db.close()
    db_path = tmp_path / "tracker.db"
    before = db_hash(db_path)

    completed = subprocess.run(
        [sys.executable, "scripts/retriage_resolved_postings.py", "--db", str(db_path), "--preview", "--json"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert db_hash(db_path) == before
    payload = json.loads(completed.stdout)
    assert payload == {
        "mode": "preview",
        "count": 2,
        "candidates": [
            {
                "posting_id": "greenhouse",
                "company": "Acme",
                "title": "Role greenhouse",
                "source_url": SOURCE_URL + "/greenhouse",
                "resolved_url": GREENHOUSE_URL,
                "ats": "greenhouse",
                "destination": "ready",
                "reason": "no adapter for other",
            },
            {
                "posting_id": "smart",
                "company": "Acme",
                "title": "Role smart",
                "source_url": SOURCE_URL + "/smart",
                "resolved_url": "https://jobs.smartrecruiters.com/acme/123-engineer",
                "ats": "smartrecruiters",
                "destination": "manual",
                "reason": "no adapter for other",
            },
        ],
    }


def test_retriage_cli_apply_scope_and_idempotence(tmp_path: Path) -> None:
    db = retriage_conn(tmp_path)
    excluded = seed_retriage_fixture(db)
    before = {posting_id: posting_snapshot(db, posting_id) for posting_id in excluded}
    db.commit()
    db.close()
    db_path = tmp_path / "tracker.db"

    first = subprocess.run(
        [sys.executable, "scripts/retriage_resolved_postings.py", "--db", str(db_path), "--apply", "--json"],
        check=True,
        capture_output=True,
        text=True,
    )
    second = subprocess.run(
        [sys.executable, "scripts/retriage_resolved_postings.py", "--db", str(db_path), "--apply", "--json"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(first.stdout)["result"] == {"requested": 2, "updated": 2, "skipped": 0}
    assert json.loads(second.stdout)["result"] == {"requested": 0, "updated": 0, "skipped": 0}
    db = sqlite3.connect(db_path)
    assert posting_snapshot(db, "greenhouse")[4:7] == ("queued", None, "")
    assert posting_snapshot(db, "smart")[4:7] == (
        "manual",
        "manual",
        "prepared for manual completion: smartrecruiters",
    )
    assert {posting_id: posting_snapshot(db, posting_id) for posting_id in excluded} == before
