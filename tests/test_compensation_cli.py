import json
import sqlite3

import pytest


class FakeProvider:
    def __init__(self, results=(), exc=None):
        self.results = list(results)
        self.exc = exc
        self.queries = []

    def search(self, query, include_domains):
        self.queries.append((query, tuple(include_domains)))
        if self.exc:
            raise self.exc
        return list(self.results)


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "tracker.db"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE postings (
            posting_id TEXT PRIMARY KEY,
            company TEXT,
            title TEXT,
            locations TEXT,
            url TEXT,
            status TEXT,
            last_error TEXT,
            attempt_count INTEGER DEFAULT 0,
            application_count INTEGER DEFAULT 0
        )
        """
    )
    conn.execute(
        """
        INSERT INTO postings (posting_id, company, title, locations, url, status, last_error)
        VALUES ('p1', 'Acme', 'Software Engineer Intern', 'New York, NY',
                'https://acme.com/jobs/1', 'manual',
                'manual unanswered: What is your desired hourly rate? compensation required')
        """
    )
    conn.commit()
    yield conn
    conn.close()


def _count_evidence(conn):
    return conn.execute("SELECT COUNT(*) FROM compensation_evidence").fetchone()[0]


def test_prepare_posting_uses_employer_range_without_search(conn):
    from compensation.research import prepare_posting

    provider = FakeProvider([])
    result = prepare_posting(conn, "p1", provider, now=100, fetch_jd=lambda _url: "Pay range $40-$50 per hour")

    assert result["status"] == "stored"
    assert result["amount"] == "45"
    assert result["period"] == "hour"
    assert result["method"] == "employer_midpoint"
    assert result["source_urls"] == ["https://acme.com/jobs/1"]
    assert provider.queries == []


def test_prepare_posting_searches_only_public_job_context(conn):
    from compensation.research import prepare_posting

    provider = FakeProvider([
        {"url": "https://levels.fyi/a", "title": "Acme Software Engineer Intern New York", "content": "$40-$50 per hour"},
        {"url": "https://indeed.com/a", "title": "Acme Software Engineer Intern New York", "content": "$44-$54 per hour"},
    ])
    result = prepare_posting(conn, "p1", provider, now=100, fetch_jd=lambda _url: "No pay listed for David david@example.com")

    query = provider.queries[0][0]
    assert "Acme" in query and "Software Engineer Intern" in query and "New York" in query
    assert "david" not in query.lower() and "@" not in query
    assert provider.queries[0][1] == ("levels.fyi", "indeed.com", "glassdoor.com", "salary.com")
    assert result["status"] == "stored"
    assert result["amount"] == "47"


@pytest.mark.parametrize(("posting_id", "expected"), [("missing", "posting_not_found")])
def test_prepare_posting_absent_posting_stores_no_evidence(conn, posting_id, expected):
    from compensation.research import prepare_posting

    result = prepare_posting(conn, posting_id, FakeProvider([]), now=100, fetch_jd=lambda _url: "")

    assert result["status"] == expected
    assert _count_evidence(conn) == 0


def test_prepare_posting_requires_manual_or_failed_compensation_blocker(conn):
    from compensation.research import prepare_posting

    conn.execute("UPDATE postings SET status='ready', last_error='manual unanswered: desired hourly rate' WHERE posting_id='p1'")
    ready = prepare_posting(conn, "p1", FakeProvider([]), now=100, fetch_jd=lambda _url: "$40-$50 per hour")
    conn.execute("UPDATE postings SET status='manual', last_error='manual unanswered: portfolio URL' WHERE posting_id='p1'")
    non_comp = prepare_posting(conn, "p1", FakeProvider([]), now=100, fetch_jd=lambda _url: "$40-$50 per hour")

    assert ready["status"] == "not_manual_or_failed"
    assert non_comp["status"] == "not_compensation_blocker"
    assert _count_evidence(conn) == 0


def test_prepare_posting_missing_provider_or_timeout_stores_no_evidence(conn):
    from compensation.research import prepare_posting, SearchProviderUnavailable

    no_provider = prepare_posting(conn, "p1", None, now=100, fetch_jd=lambda _url: "No pay listed")
    timeout = prepare_posting(conn, "p1", FakeProvider(exc=TimeoutError("slow")), now=100, fetch_jd=lambda _url: "No pay listed")
    unavailable = prepare_posting(conn, "p1", FakeProvider(exc=SearchProviderUnavailable("missing key")), now=100, fetch_jd=lambda _url: "No pay listed")

    assert no_provider["status"] == "search_provider_unavailable"
    assert timeout["status"] == "search_timeout"
    assert unavailable["status"] == "search_provider_unavailable"
    assert _count_evidence(conn) == 0


def test_prepare_posting_filters_malformed_hostile_and_unapproved_results(conn):
    from compensation.research import prepare_posting

    provider = FakeProvider([
        {"url": "https://levels.fyi/a", "title": "Acme Software Engineer Intern New York", "content": "$40-$50 per hour"},
        {"url": "https://indeed.com/a", "title": "Acme Software Engineer Intern New York", "content": "$44-$54 per hour"},
        {"url": "http://glassdoor.com/a", "title": "Acme Software Engineer Intern New York", "content": "$99-$100 per hour"},
        {"url": "https://evil.com/a", "title": "Acme Software Engineer Intern New York", "content": "$10-$11 per hour"},
        {"url": "https://salary.com/a", "title": 12, "content": "$1-$2 per hour"},
        "bad",
    ])
    result = prepare_posting(conn, "p1", provider, now=100, fetch_jd=lambda _url: "No pay listed")

    assert result["status"] == "stored"
    assert result["source_urls"] == ["https://levels.fyi/a", "https://indeed.com/a"]


def test_prepare_posting_insufficient_evidence_and_ambiguous_period_do_not_store(conn):
    from compensation.research import prepare_posting

    insufficient = prepare_posting(conn, "p1", FakeProvider([
        {"url": "https://levels.fyi/a", "title": "Acme Software Engineer Intern New York", "content": "$40-$50 per hour"},
    ]), now=100, fetch_jd=lambda _url: "No pay listed")
    conn.execute("UPDATE postings SET last_error='manual unanswered: desired compensation' WHERE posting_id='p1'")
    ambiguous = prepare_posting(conn, "p1", FakeProvider([]), now=100, fetch_jd=lambda _url: "$40-$50 per hour")

    assert insufficient["status"] == "insufficient_evidence"
    assert ambiguous["status"] == "manual_period_unknown"
    assert _count_evidence(conn) == 0


def test_prepare_posting_never_authorizes_non_usd_or_ambiguous_evidence(conn):
    from compensation.research import prepare_posting

    provider = FakeProvider([
        {"url": "https://levels.fyi/a", "title": "Acme Software Engineer Intern New York", "content": "CAD $40-$50 per hour"},
        {"url": "https://indeed.com/a", "title": "Acme Software Engineer Intern New York", "content": "40 to 50 per hour"},
    ])
    result = prepare_posting(conn, "p1", provider, now=100, fetch_jd=lambda _url: "No pay listed")

    assert result["status"] == "insufficient_evidence"
    assert _count_evidence(conn) == 0


def test_prepare_posting_does_not_mutate_postings(conn):
    from compensation.research import prepare_posting

    before = dict(conn.execute("SELECT * FROM postings WHERE posting_id='p1'").fetchone())
    prepare_posting(conn, "p1", FakeProvider([]), now=100, fetch_jd=lambda _url: "$40-$50 per hour")
    after = dict(conn.execute("SELECT * FROM postings WHERE posting_id='p1'").fetchone())

    assert after == before


def test_cli_requires_key_only_when_search_needed_and_outputs_json(tmp_path, monkeypatch):
    import scripts.prepare_compensation as cli

    db = tmp_path / "tracker.db"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE postings (posting_id TEXT PRIMARY KEY, company TEXT, title TEXT, locations TEXT, url TEXT, status TEXT, last_error TEXT)")
    conn.execute("INSERT INTO postings VALUES ('p1','Acme','Software Engineer Intern','New York, NY','https://acme.com/jobs/1','manual','manual unanswered: desired hourly rate compensation')")
    conn.commit(); conn.close()
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setattr(cli, "fetch_jd", lambda _url: "$40-$50 per hour")

    code = cli.main(["--db", str(db), "--posting-id", "p1", "--json"])

    assert code == 0


def test_tavily_provider_filters_transport_results_without_live_network(monkeypatch):
    from compensation.research import TavilySearchProvider

    captured = {}

    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def read(self):
            return json.dumps({"results": [
                {"url": "https://levels.fyi/a", "title": "ok", "content": "$40-$50 per hour"},
                {"url": "http://indeed.com/a", "title": "bad", "content": "$40-$50 per hour"},
                {"url": "https://evil.com/a", "title": "bad", "content": "$40-$50 per hour"},
                {"url": "https://indeed.com/a", "title": 1, "content": "$40-$50 per hour"},
            ]}).encode()

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["payload"] = json.loads(request.data.decode())
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    provider = TavilySearchProvider("key", timeout=3)

    rows = provider.search("Acme intern pay", ["levels.fyi", "indeed.com"])

    assert captured["url"] == "https://api.tavily.com/search"
    assert captured["timeout"] == 3
    assert captured["payload"]["api_key"] == "key"
    assert captured["payload"]["include_domains"] == ["levels.fyi", "indeed.com"]
    assert rows == [{"url": "https://levels.fyi/a", "title": "ok", "content": "$40-$50 per hour"}]


def test_cli_rejects_missing_db_without_creating_file(tmp_path, capsys):
    import scripts.prepare_compensation as cli

    missing = tmp_path / "missing.db"

    code = cli.main(["--db", str(missing), "--posting-id", "p1", "--json"])

    assert code == 2
    assert not missing.exists()
    assert "invalid database" in capsys.readouterr().err


def test_cli_rejects_db_without_required_postings_schema(tmp_path, capsys):
    import scripts.prepare_compensation as cli

    db = tmp_path / "tracker.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE other (id TEXT)")
    conn.commit()
    conn.close()

    code = cli.main(["--db", str(db), "--posting-id", "p1"])

    assert code == 2
    assert "postings" in capsys.readouterr().err


def test_prepare_posting_unexpected_provider_exception_fails_closed_and_continues(conn):
    from compensation.research import prepare_posting

    result = prepare_posting(conn, "p1", FakeProvider(exc=RuntimeError("secret token david@example.com")), now=100, fetch_jd=lambda _url: "No pay listed")

    assert result == {"posting_id": "p1", "status": "search_failed"}
    assert _count_evidence(conn) == 0


def test_prepare_posting_multiple_employer_ranges_do_not_search(conn):
    from compensation.research import prepare_posting

    provider = FakeProvider([
        {"url": "https://levels.fyi/a", "title": "Acme Software Engineer Intern New York", "content": "$40-$50 per hour"},
        {"url": "https://indeed.com/a", "title": "Acme Software Engineer Intern New York", "content": "$44-$54 per hour"},
    ])

    result = prepare_posting(conn, "p1", provider, now=100, fetch_jd=lambda _url: "$40-$50 per hour and $44-$54 per hour")

    assert result["status"] == "ambiguous_employer_evidence"
    assert provider.queries == []
    assert _count_evidence(conn) == 0


def test_prepare_posting_bare_rate_marker_does_not_pass_compensation_gate(conn):
    from compensation.research import prepare_posting

    conn.execute("UPDATE postings SET last_error='manual unanswered: What is your website conversion rate?' WHERE posting_id='p1'")

    result = prepare_posting(conn, "p1", FakeProvider([]), now=100, fetch_jd=lambda _url: "$40-$50 per hour")

    assert result["status"] == "not_compensation_blocker"
    assert _count_evidence(conn) == 0
