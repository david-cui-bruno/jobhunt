import sqlite3

from submission.lanes import classify_url, quarantine_unsupported


def test_lane_mapping_is_explicit() -> None:
    assert classify_url("https://boards.greenhouse.io/acme/jobs/1")[1].name == "direct"
    assert classify_url("https://acme.wd1.myworkdayjobs.com/job/1")[1].name == "workday"
    assert classify_url("https://jobs.ashbyhq.com/acme/id")[1].name == "ashby"
    assert classify_url("https://jobs.smartrecruiters.com/acme/1")[1].automatic is False
    assert classify_url("https://example.com/careers/1")[1].name == "unsupported"


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
            ("greenhouse", "queued", "https://boards.greenhouse.io/acme/jobs/1", None, None, None),
        ],
    )

    assert quarantine_unsupported(conn) == 1

    rows = {
        row["posting_id"]: dict(row)
        for row in conn.execute("SELECT * FROM postings ORDER BY posting_id")
    }
    assert rows["unknown"]["status"] == "manual"
    assert rows["unknown"]["outcome"] == "manual"
    assert rows["unknown"]["last_error"] == "no adapter for other"
    assert rows["greenhouse"]["status"] == "queued"
    assert rows["greenhouse"]["last_error"] is None
