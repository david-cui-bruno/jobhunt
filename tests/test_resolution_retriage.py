from __future__ import annotations

import sqlite3
from pathlib import Path

from submission.identity import canonical_conflict_reason
from submission.database import connect_tracker
from submission.resolutions import cached_resolution, ensure_resolution_schema, record_resolution
from watcher import watch
from watcher.url_resolver import ResolutionResult


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
