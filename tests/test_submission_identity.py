import sqlite3

from submission.identity import canonical_posting_key, posting_already_applied


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE postings (posting_id TEXT PRIMARY KEY, company TEXT, url TEXT, status TEXT)")
    conn.execute("CREATE TABLE applications (posting_id TEXT PRIMARY KEY)")
    return conn


def test_distinct_roles_at_one_company_are_not_duplicates() -> None:
    conn = db()
    conn.executemany("INSERT INTO postings VALUES (?,?,?,?)", [
        ("p1", "Acme", "https://jobs.ashbyhq.com/acme/11111111-1111-1111-1111-111111111111", "submitted"),
        ("p2", "Acme", "https://jobs.ashbyhq.com/acme/22222222-2222-2222-2222-222222222222", "ready"),
    ])
    conn.execute("INSERT INTO applications VALUES ('p1')")
    assert posting_already_applied(conn, "p2", conn.execute(
        "SELECT url FROM postings WHERE posting_id='p2'"
    ).fetchone()[0]) is False


def test_wrapper_and_direct_url_for_same_job_share_one_key() -> None:
    direct = "https://jobs.ashbyhq.com/acme/11111111-1111-1111-1111-111111111111/application"
    wrapped = direct + "?embed=true"
    assert canonical_posting_key("a", direct) == canonical_posting_key("b", wrapped)
