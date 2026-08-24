import re
from pathlib import Path

import pytest

from watcher.url_resolver import resolve_dreamwork_html, validate_public_target


DREAMWORK_URL = "https://jobs.dreamwork.com/acme/software-engineer"
FIXTURE_DIR = Path(__file__).parent / "fixtures" / "dreamwork"
PUBLIC_ERROR = "resolved target is not a public http(s) URL"


@pytest.fixture
def fixture_text():
    def read_fixture(name: str) -> str:
        return (FIXTURE_DIR / name).read_text(encoding="utf-8")

    return read_fixture


def anchor_html(href: str, text: str = "View original posting", class_name: str = "job-cta-secondary") -> str:
    return f'<html><body><a class="{class_name}" href="{href}">{text}</a></body></html>'


def test_dreamwork_parser_extracts_original_posting(fixture_text):
    html = fixture_text("job-greenhouse.html")

    result = resolve_dreamwork_html(DREAMWORK_URL, html)

    assert result.source_url == DREAMWORK_URL
    assert result.resolved_url == "https://job-boards.greenhouse.io/acme/jobs/1234567"
    assert result.resolver == "dreamwork-original-v1"
    assert result.error == ""
    assert len(result.source_hash) == 64


def test_dreamwork_parser_rejects_private_target(fixture_text):
    result = resolve_dreamwork_html(DREAMWORK_URL, fixture_text("job-unsafe.html"))

    assert result.source_url == DREAMWORK_URL
    assert result.resolved_url is None
    assert result.error == PUBLIC_ERROR
    assert len(result.source_hash) == 64


def test_dreamwork_parser_resolves_relative_original_posting_links():
    result = resolve_dreamwork_html(DREAMWORK_URL, anchor_html("/apply/jobs/123?gh_jid=123#details"))

    assert result.resolved_url == "https://jobs.dreamwork.com/apply/jobs/123?gh_jid=123"
    assert result.error == ""


def test_dreamwork_parser_reports_missing_original_posting_link():
    result = resolve_dreamwork_html(DREAMWORK_URL, "<html><body><p>No target</p></body></html>")

    assert result.resolved_url is None
    assert result.error == "original posting link not found"


@pytest.mark.parametrize(
    "target",
    [
        "javascript:alert(1)",
        "file:///etc/passwd",
        "https://user:secret@example.com/jobs/1",
        "https://localhost/jobs/1",
        "https://internal.localhost/jobs/1",
        "http://10.0.0.5/jobs/1",
        "http://172.16.0.5/jobs/1",
        "http://192.168.1.5/jobs/1",
        "http://[::1]/jobs/1",
        "http://[fd00::1]/jobs/1",
    ],
)
def test_validate_public_target_rejects_unsafe_targets(target):
    with pytest.raises(ValueError, match=re.escape(PUBLIC_ERROR)):
        validate_public_target(target)


def test_dreamwork_parser_rejects_wrong_anchor_text():
    html = anchor_html("https://job-boards.greenhouse.io/acme/jobs/1234567", text="Apply now")

    result = resolve_dreamwork_html(DREAMWORK_URL, html)

    assert result.resolved_url is None
    assert result.error == "original posting link not found"


def test_dreamwork_parser_ignores_wrong_anchor_class():
    html = anchor_html("https://job-boards.greenhouse.io/acme/jobs/1234567", class_name="job-cta-primary")

    result = resolve_dreamwork_html(DREAMWORK_URL, html)

    assert result.resolved_url is None
    assert result.error == "original posting link not found"
