import re
from pathlib import Path

import pytest

import apply.jd as jd
from apply.jd import detect_ats
from apply.oraclecloud_url import parse_oracle_posting_url
from submission.lanes import classify_url
from submission.identity import canonical_posting_key


ORACLE_JOB_URL = "https://egug.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/26011992"
ORACLE_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "oraclecloud"


@pytest.fixture
def fixture_text():
    def load(name: str) -> str:
        return (ORACLE_FIXTURE_DIR / name).read_text(encoding="utf-8")

    return load


def test_parse_oracle_candidate_experience_url():
    parsed = parse_oracle_posting_url(ORACLE_JOB_URL)
    assert parsed is not None
    assert parsed.host == "egug.fa.us2.oraclecloud.com"
    assert parsed.locale == "en"
    assert parsed.site == "CX_1"
    assert parsed.job_id == "26011992"


def test_oracle_parser_rejects_unverified_and_non_candidate_experience_urls():
    assert parse_oracle_posting_url("http://egug.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/26011992") is None
    assert parse_oracle_posting_url("https://oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/26011992") is None
    assert parse_oracle_posting_url("https://www.oracle.com/careers") is None
    assert parse_oracle_posting_url("https://example.oraclecloud.com/not-a-job") is None
    assert parse_oracle_posting_url("https://egug.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/notnumeric") is None


def test_oracle_detection_is_narrow():
    assert detect_ats(ORACLE_JOB_URL) == "oraclecloud"
    assert detect_ats("https://www.oracle.com/careers") == "other"
    assert detect_ats("https://example.oraclecloud.com/not-a-job") == "other"


def test_oracle_lane_is_isolated():
    ats, lane = classify_url(ORACLE_JOB_URL)
    assert ats == "oraclecloud"
    assert lane.name == "oracle"
    assert lane.concurrency == 1
    assert lane.attempts_per_cycle == 2


def test_oracle_identity_ignores_locale_case_and_tracking_queries():
    direct = ORACLE_JOB_URL
    tracked = "https://egug.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/EN/sites/cx_1/job/26011992?utm_source=foo&ref=bar"
    assert canonical_posting_key("direct", direct) == canonical_posting_key("tracked", tracked)


def test_oracle_jd_uses_public_og_description(monkeypatch, fixture_text):
    monkeypatch.setattr(jd, "_get", lambda _: fixture_text("job-open.html"))
    assert jd._oraclecloud(ORACLE_JOB_URL) == "Example job description"


def test_oracle_closed_marker_is_explicit(fixture_text):
    assert jd.oracle_closed_marker(fixture_text("job-closed.html")) == "job is no longer available"
    assert jd.oracle_closed_marker(fixture_text("job-open.html")) is None


def test_oracle_fixtures_are_sanitized_public_structures(fixture_text):
    combined = "\n".join(
        fixture_text(name)
        for name in ("job-open.html", "job-closed.html", "apply-anonymous.html")
    )

    forbidden_patterns = {
        "email": r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
        "phone": r"(?:\+?\d[\d .()/-]{7,}\d)",
        "cookie": r"\b(cookie|set-cookie|sessionid|jsessionid)\b",
        "secret_token": r"\b(bearer|authorization|access[_-]?token|refresh[_-]?token|api[_-]?key|client[_-]?secret)\b",
        "real_candidate_answer": r"\b(David Cui|davidcui|gmail\.com|linkedin\.com/in/)\b",
    }
    for label, pattern in forbidden_patterns.items():
        assert re.search(pattern, combined, re.I) is None, label

    assert "Example Company" in combined
    assert "Example job description" in combined
