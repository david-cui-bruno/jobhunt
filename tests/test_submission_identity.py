import sqlite3

import apply.jd
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


def test_canonical_posting_key_does_not_fetch_for_greenhouse_wrappers(monkeypatch) -> None:
    def fail_fetch(url: str, timeout: int = 25) -> str:
        raise AssertionError(f"unexpected network fetch: {url}")

    monkeypatch.setattr(apply.jd, "_get", fail_fetch)
    wrapper = "https://company.example/jobs/software-engineer?gh_jid=1234567"
    assert canonical_posting_key("wrapper", wrapper) == canonical_posting_key(
        "direct", "https://boards.greenhouse.io/embed/job_app?token=1234567"
    )


def test_greenhouse_token_key_is_independent_of_board_and_wrapper_host() -> None:
    wrapper = "https://company.example/jobs/software-engineer?gh_jid=1234567"
    board_direct = "https://job-boards.greenhouse.io/embed/job_app?for=acme&token=1234567"
    fallback_direct = "https://boards.greenhouse.io/embed/job_app?token=1234567"
    assert canonical_posting_key("wrapper", wrapper) == canonical_posting_key("board", board_direct)
    assert canonical_posting_key("board", board_direct) == canonical_posting_key("fallback", fallback_direct)


def test_tracking_query_does_not_change_canonical_posting_key() -> None:
    direct = "https://jobs.lever.co/acme/11111111-1111-1111-1111-111111111111"
    tracked = direct + "?lever-source=github&utm_source=listing"
    assert canonical_posting_key("direct", direct) == canonical_posting_key("tracked", tracked)
