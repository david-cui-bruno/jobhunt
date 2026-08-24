import re
from pathlib import Path

import pytest

import apply.jd as jd
from apply.jd import detect_ats
import apply.oraclecloud as oraclecloud
from apply.oraclecloud import apply_oraclecloud
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


class _FakeLocator:
    def __init__(self, page, name, *, visible=True, count=1, value="", attrs=None, on_click=None):
        self.page = page
        self.name = name
        self._visible = visible
        self._count = count
        self._value = value
        self.attrs = attrs or {}
        self.on_click = on_click
        self.files = []

    @property
    def first(self):
        return self

    def nth(self, index):
        return self.page.file_inputs[index]

    def count(self):
        return self._count

    def is_visible(self):
        return self._visible

    def click(self, timeout=None):
        if self.on_click:
            self.on_click()

    def scroll_into_view_if_needed(self):
        self.page.scrolled.append(self.name)

    def set_input_files(self, file_path):
        self.files.append(str(file_path))
        self.page.uploaded_to = self.name
        self.page.uploaded_file = str(file_path)

    def fill(self, value):
        self._value = value
        self.page.filled[self.name] = value

    def input_value(self):
        return self._value

    def get_attribute(self, name):
        return self.attrs.get(name)

    def evaluate(self, script):
        return self.attrs.get("near_text", "")


class _FakePage:
    def __init__(self, variant="anonymous"):
        self.variant = variant
        self.goto_calls = []
        self.default_timeouts = []
        self.waits = []
        self.scrolled = []
        self.apply_clicks = 0
        self.submit_clicks = 0
        self.filled = {}
        self.uploaded_to = None
        self.uploaded_file = None
        self.qa_evaluations = 0
        self.file_inputs = [
            _FakeLocator(self, "avatar", attrs={"accept": "image/png", "near_text": "Profile photo"}),
            _FakeLocator(self, "resume", attrs={"accept": "application/pdf", "near_text": "Resume upload"}),
        ]

    def set_default_timeout(self, value):
        self.default_timeouts.append(value)

    def set_default_navigation_timeout(self, value):
        self.default_timeouts.append(value)

    def goto(self, url, wait_until=None, timeout=None):
        self.goto_calls.append((url, wait_until, timeout))

    def wait_for_timeout(self, value):
        self.waits.append(value)

    def inner_text(self, selector):
        if self.variant == "closed":
            return "This job is no longer available."
        if self.variant == "account":
            return "Sign in to apply. You must create an account to continue."
        if self.variant == "captcha":
            return "Please verify you are human. reCAPTCHA"
        return "Example application form"

    def locator(self, selector):
        if selector == "input[type=file]":
            return _FakeLocator(self, "files", count=len(self.file_inputs))
        if selector in oraclecloud.APPLY_SELECTORS:
            visible = self.variant not in {"unsupported", "closed"} and selector == "button:has-text('Apply')"
            return _FakeLocator(self, selector, visible=visible, count=1 if visible else 0, on_click=self._click_apply)
        if "Submit" in selector or "submit" in selector:
            return _FakeLocator(self, "submit", on_click=self._click_submit)
        if "iframe" in selector or "recaptcha" in selector.lower() or "hcaptcha" in selector.lower():
            present = self.variant == "captcha"
            return _FakeLocator(self, "captcha", visible=present, count=1 if present else 0)
        return _FakeLocator(self, selector, count=0, visible=False)

    def get_by_label(self, label, exact=False):
        normalized = label.lower()
        names = {
            "first name": "first_name",
            "last name": "last_name",
            "email": "email",
            "phone": "phone",
            "phone number": "phone",
            "linkedin": "linkedin",
            "github": "github",
            "website": "website",
            "resume": "resume_label",
        }
        for key, name in names.items():
            if key in normalized:
                return _FakeLocator(self, name)
        return _FakeLocator(self, label, count=0, visible=False)

    def evaluate(self, script):
        text = str(script)
        if text == oraclecloud.qa.EXTRACT_JS:
            self.qa_evaluations += 1
            return [{"id": "workAuth", "name": "workAuth", "label": "Example eligibility question", "value": ""}]
        if "required" in text:
            return ["Example eligibility question"] if self.variant == "required" else []
        return []

    def _click_apply(self):
        self.apply_clicks += 1

    def _click_submit(self):
        self.submit_clicks += 1


def test_find_resume_input_ignores_cover_letter_pdf_before_contextual_resume():
    page = _FakePage("anonymous")
    page.file_inputs = [
        _FakeLocator(page, "cover_letter", attrs={"accept": "application/pdf", "near_text": "Cover letter upload"}),
        _FakeLocator(page, "resume", attrs={"accept": "application/pdf", "near_text": "Resume upload"}),
    ]

    found = oraclecloud._find_resume_input(page)

    assert found is page.file_inputs[1]


class _FakeBrowser:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _FakeContext:
    def __init__(self, page):
        self.page = page

    def new_page(self):
        return self.page


class _FakePlaywright:
    def __enter__(self):
        return object()

    def __exit__(self, exc_type, exc, tb):
        return False


@pytest.fixture
def pdf(tmp_path):
    path = tmp_path / "resume.pdf"
    path.write_bytes(b"%PDF-1.4 synthetic")
    return path


@pytest.fixture
def fake_oracle(monkeypatch):
    pages = []

    def install(variant="anonymous"):
        page = _FakePage(variant)
        pages.append(page)
        monkeypatch.setattr(oraclecloud, "sync_playwright", lambda: _FakePlaywright())
        monkeypatch.setattr(oraclecloud.stealth, "launch_stealth_context", lambda _pw: (_FakeBrowser(), _FakeContext(page)))
        monkeypatch.setattr(oraclecloud, "safe_screenshot", lambda page, slug, stage, root: str(root / f"{slug}-{stage}.png"))
        monkeypatch.setattr(oraclecloud.qa, "get_answers", lambda controls, context=None: [{"id_or_name": "workAuth", "answer": "Synthetic option"}])
        monkeypatch.setattr(oraclecloud.qa, "fill_answers", lambda page, controls, answers: ([a["id_or_name"] for a in answers], []))
        return page

    return install


def test_oracle_dry_run_reaches_submit_boundary_without_click(fake_oracle, pdf):
    page = fake_oracle("anonymous")

    result = apply_oraclecloud(ORACLE_JOB_URL, pdf, "oracle-dry", dry_run=True)

    assert page.goto_calls == [(ORACLE_JOB_URL, "domcontentloaded", 45000)]
    assert page.apply_clicks == 1
    assert page.uploaded_to == "resume"
    assert page.uploaded_file == str(pdf)
    assert page.filled["first_name"]
    assert page.filled["last_name"]
    assert page.filled["email"]
    assert page.filled["phone"]
    assert page.filled["linkedin"]
    assert page.qa_evaluations == 3
    assert result["ok"] is True
    assert result["submitted"] is False
    assert result["reason"] == "dry run - did not submit"
    assert result["unanswered"] == []
    assert result["artifact_refs"] == {"filled_form_screenshot": str(oraclecloud.SHOTS / "oracle-dry-filled.png")}
    assert page.submit_clicks == 0


def test_oracle_detects_closed_before_apply_click(fake_oracle, pdf):
    page = fake_oracle("closed")

    result = apply_oraclecloud(ORACLE_JOB_URL, pdf, "oracle-closed", dry_run=True)

    assert result["outcome"] == "stale"
    assert "no longer available" in result["reason"]
    assert page.apply_clicks == 0
    assert page.submit_clicks == 0


def test_oracle_account_gate_is_manual_fail_closed(fake_oracle, pdf):
    page = fake_oracle("account")

    result = apply_oraclecloud(ORACLE_JOB_URL, pdf, "oracle-account", dry_run=True)

    assert result["outcome"] == "manual"
    assert result["retryable"] is False
    assert result["click_attempted"] is False
    assert result["submission_uncertain"] is False
    assert "account" in result["reason"].lower()
    assert page.apply_clicks == 0
    assert page.submit_clicks == 0


def test_oracle_captcha_gate_is_manual_fail_closed(fake_oracle, pdf):
    page = fake_oracle("captcha")

    result = apply_oraclecloud(ORACLE_JOB_URL, pdf, "oracle-captcha", dry_run=True)

    assert result["outcome"] == "manual"
    assert result["retryable"] is False
    assert result["click_attempted"] is False
    assert result["submission_uncertain"] is False
    assert "captcha" in result["reason"].lower()
    assert page.apply_clicks == 0
    assert page.submit_clicks == 0


def test_oracle_unknown_apply_variant_is_manual_without_click(fake_oracle, pdf):
    page = fake_oracle("unsupported")

    result = apply_oraclecloud(ORACLE_JOB_URL, pdf, "oracle-unknown", dry_run=True)

    assert result["outcome"] == "manual"
    assert result["reason"] == "unsupported Oracle tenant variant: apply control not found"
    assert result["click_attempted"] is False
    assert result["submission_uncertain"] is False
    assert page.apply_clicks == 0
    assert page.submit_clicks == 0


def test_oracle_required_empty_returns_manual_without_submit(fake_oracle, pdf):
    page = fake_oracle("required")

    result = apply_oraclecloud(ORACLE_JOB_URL, pdf, "oracle-required", dry_run=True)

    assert result["outcome"] == "manual"
    assert result["reason"] == "needs answers: ['Example eligibility question']"
    assert result["unanswered"] == ["Example eligibility question"]
    assert result["click_attempted"] is False
    assert result["submission_uncertain"] is False
    assert page.submit_clicks == 0
