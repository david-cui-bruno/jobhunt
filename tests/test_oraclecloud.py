import re
from pathlib import Path

import pytest

import apply.jd as jd
from apply.jd import detect_ats
import apply.oraclecloud as oraclecloud
from apply.oraclecloud import apply_oraclecloud
from apply.oraclecloud_url import parse_oracle_posting_url
from submission.lanes import classify_url
from submission.identity import canonical_posting_key, _canonical_identity_material


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
    assert parse_oracle_posting_url("https://egug.fa.us2.oraclecloud.com:443/hcmUI/CandidateExperience/en/sites/CX_1/job/26011992") is None


def test_oracle_parser_rejects_username_or_password_userinfo():
    userinfo_urls = [
        "https://candidate@egug.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/26011992",
        "https://:secret@egug.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/26011992",
        "https://candidate:secret@egug.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/26011992",
    ]
    for url in userinfo_urls:
        assert parse_oracle_posting_url(url) is None


def test_oracle_userinfo_cannot_split_canonical_identity():
    userinfo_url = "https://candidate@egug.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/26011992"
    assert not _canonical_identity_material("userinfo", userinfo_url).startswith("oraclecloud:")

    clean_key = canonical_posting_key("clean", ORACLE_JOB_URL)
    userinfo_key = canonical_posting_key("userinfo", userinfo_url)
    assert userinfo_key != clean_key


def test_oracle_userinfo_cannot_route_to_oracle_adapter():
    ats, lane = classify_url(
        "https://candidate:secret@egug.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/26011992"
    )
    assert ats == "other"
    assert lane.name == "unsupported"


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
        self.checked = False

    @property
    def first(self):
        return self

    def nth(self, index):
        if self.name == "next_buttons":
            return self.page.next_buttons[index]
        return self.page.file_inputs[index]

    def count(self):
        return self._count

    def is_visible(self):
        return self._visible

    def click(self, timeout=None, force=False):
        self.page.events.append(f"click:{self.name}")
        if self.on_click:
            self.on_click()

    def check(self, timeout=None, force=False):
        if self.page.variant in {
            "email_gate_hidden_legal_requires_label",
            "email_gate_missing_legal_label",
            "email_gate_ambiguous_legal_label",
            "email_gate_label_click_does_not_check_legal",
            "email_gate_legal_label_click_raises",
        } and self.name == "legal-disclaimer-checkbox":
            raise Exception("Element is outside of the viewport")
        self.checked = True
        self.page.checked[self.name] = {"force": force, "timeout": timeout}

    def is_checked(self):
        return self.name in self.page.checked

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
        self.checked = {}
        self.next_clicks = 0
        self.role_queries = []
        self.uploaded_to = None
        self.uploaded_file = None
        self.qa_evaluations = 0
        self.events = []
        self.url = ORACLE_JOB_URL
        self.confirmation_body = "Your application has been submitted successfully."
        self.raise_on_submit = variant == "submit_timeout"
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
        if self.variant == "submit_timeout" and self.submit_clicks:
            raise TimeoutError("confirmation wait timed out")

    def inner_text(self, selector):
        if self.variant == "closed":
            return "This job is no longer available."
        if self.variant == "account":
            return "Sign in to apply. You must create an account to continue."
        if self.variant == "captcha":
            return "Please verify you are human. reCAPTCHA"
        if self.submit_clicks and self.variant == "confirmed":
            return self.confirmation_body
        if self.variant.startswith("email_gate") and not self.next_clicks:
            return "You don't need to have an account Email Address I agree with the terms and conditions Next"
        return "Example application form"

    def locator(self, selector):
        if selector == "input[type=file]":
            return _FakeLocator(self, "files", count=0 if self.variant.startswith("email_gate") and not self.next_clicks else len(self.file_inputs))
        if selector == "input[type=email][name='primary-email']":
            present = self.variant in {
                "email_gate",
                "email_gate_accessible_next",
                "email_gate_duplicate_accessible_next",
                "email_gate_missing_next",
                "email_gate_ambiguous_next",
                "email_gate_missing_legal",
                "email_gate_hidden_legal_requires_label",
                "email_gate_missing_legal_label",
                "email_gate_ambiguous_legal_label",
                "email_gate_label_click_does_not_check_legal",
                "email_gate_legal_label_click_raises",
            } and not self.next_clicks
            return _FakeLocator(self, "primary-email", visible=present, count=1 if present else 0)
        if selector == "#legal-disclaimer-checkbox":
            present = self.variant in {
                "email_gate",
                "email_gate_accessible_next",
                "email_gate_duplicate_accessible_next",
                "email_gate_missing_next",
                "email_gate_ambiguous_next",
                "email_gate_missing_email",
                "email_gate_hidden_legal_requires_label",
                "email_gate_missing_legal_label",
                "email_gate_ambiguous_legal_label",
                "email_gate_label_click_does_not_check_legal",
                "email_gate_legal_label_click_raises",
            } and not self.next_clicks
            return _FakeLocator(self, "legal-disclaimer-checkbox", visible=False, count=1 if present else 0)
        if selector == "label[for='legal-disclaimer-checkbox']":
            if not self.variant.startswith("email_gate") or self.next_clicks:
                return _FakeLocator(self, "legal-disclaimer-label", visible=False, count=0)
            if self.variant == "email_gate_missing_legal_label":
                count = 0
            elif self.variant == "email_gate_ambiguous_legal_label":
                count = 2
            else:
                count = 1

            def click_label():
                if self.variant == "email_gate_legal_label_click_raises":
                    raise Exception("label click failed")
                if self.variant != "email_gate_label_click_does_not_check_legal":
                    self.checked["legal-disclaimer-checkbox"] = {"via": "label"}

            return _FakeLocator(self, "legal-disclaimer-label", visible=True, count=count, on_click=click_label)
        if selector == "button:text-is('Next')":
            if not self.variant.startswith("email_gate") or self.next_clicks or self.variant in {"email_gate_accessible_next", "email_gate_duplicate_accessible_next"}:
                return _FakeLocator(self, "next_buttons", visible=False, count=0)
            count = 0 if self.variant == "email_gate_missing_next" else (2 if self.variant == "email_gate_ambiguous_next" else 1)
            self.next_buttons = [_FakeLocator(self, "next", visible=True, on_click=self._click_next) for _ in range(count)]
            return _FakeLocator(self, "next_buttons", visible=count > 0, count=count)
        if selector in oraclecloud.APPLY_SELECTORS:
            delayed_visible = self.variant == "delayed_apply" and len(self.waits) > 1 and selector == "button:has-text('Apply')"
            visible = (
                delayed_visible
                or (self.variant not in {"unsupported", "closed", "delayed_apply"} and selector == "button:has-text('Apply')")
            )
            return _FakeLocator(self, selector, visible=visible, count=1 if visible else 0, on_click=self._click_apply)
        if selector in oraclecloud.SUBMIT_SELECTORS:
            visible = self.variant not in {"missing_submit"} and selector == oraclecloud.SUBMIT_SELECTORS[0]
            count = 2 if self.variant == "ambiguous_submit" and visible else (1 if visible else 0)
            return _FakeLocator(self, "submit", visible=visible, count=count, on_click=self._click_submit)
        if "Submit" in selector or "submit" in selector:
            return _FakeLocator(self, "non_exact_submit", visible=False, count=0)
        if "iframe" in selector or "recaptcha" in selector.lower() or "hcaptcha" in selector.lower():
            present = self.variant == "captcha"
            return _FakeLocator(self, "captcha", visible=present, count=1 if present else 0)
        return _FakeLocator(self, selector, count=0, visible=False)

    def get_by_role(self, role, name=None, exact=False):
        self.role_queries.append({"role": role, "name": name, "exact": exact})
        if role == "button" and name == "Next" and exact is True and self.variant.startswith("email_gate") and not self.next_clicks:
            if self.variant == "email_gate_missing_next":
                count = 0
            elif self.variant in {"email_gate_ambiguous_next", "email_gate_duplicate_accessible_next"}:
                count = 2
            else:
                count = 1
            self.next_buttons = [_FakeLocator(self, "next", visible=True, on_click=self._click_next) for _ in range(count)]
            return _FakeLocator(self, "next_buttons", visible=count > 0, count=count)
        return _FakeLocator(self, str(name or role), count=0, visible=False)

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
        if self.variant.startswith("email_gate"):
            self.url = f"{ORACLE_JOB_URL}/apply/email"

    def _click_next(self):
        self.next_clicks += 1
        self.url = f"{ORACLE_JOB_URL}/apply/resume"

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


def test_oracle_anonymous_email_gate_advances_to_resume_without_submit(fake_oracle, pdf):
    page = fake_oracle("email_gate")

    result = apply_oraclecloud(ORACLE_JOB_URL, pdf, "oracle-email-gate", dry_run=True)

    assert page.apply_clicks == 1
    assert page.filled["primary-email"] == oraclecloud.PROFILE["email"]
    assert page.checked["legal-disclaimer-checkbox"]["force"] is True
    assert page.next_clicks == 1
    assert page.uploaded_to == "resume"
    assert result["ok"] is True
    assert result["submitted"] is False
    assert result["reason"] == "dry run - did not submit"
    assert page.submit_clicks == 0


def test_oracle_anonymous_email_gate_clicks_exact_visible_legal_label_when_hidden_input_is_outside_viewport(fake_oracle, pdf):
    page = fake_oracle("email_gate_hidden_legal_requires_label")

    result = apply_oraclecloud(ORACLE_JOB_URL, pdf, "oracle-email-gate-hidden-legal", dry_run=True)

    assert page.events == ["click:button:has-text('Apply')", "click:legal-disclaimer-label", "click:next"]
    assert page.checked["legal-disclaimer-checkbox"] == {"via": "label"}
    assert page.next_clicks == 1
    assert page.uploaded_to == "resume"
    assert result["ok"] is True
    assert result["submitted"] is False
    assert result["reason"] == "dry run - did not submit"
    assert page.submit_clicks == 0


def test_oracle_email_gate_legal_label_fallback_fail_closed_when_missing_ambiguous_or_unverified(fake_oracle, pdf):
    for variant in (
        "email_gate_missing_legal_label",
        "email_gate_ambiguous_legal_label",
        "email_gate_label_click_does_not_check_legal",
    ):
        page = fake_oracle(variant)

        result = apply_oraclecloud(ORACLE_JOB_URL, pdf, f"oracle-{variant}", dry_run=True)

        assert result["outcome"] == "manual"
        assert "anonymous email gate" in result["reason"]
        assert page.next_clicks == 0
        assert page.uploaded_to is None
        assert page.submit_clicks == 0


def test_oracle_email_gate_legal_label_fallback_fail_closed_when_label_click_raises(fake_oracle, pdf):
    page = fake_oracle("email_gate_legal_label_click_raises")

    result = apply_oraclecloud(ORACLE_JOB_URL, pdf, "oracle-legal-label-click-raises", dry_run=True)

    assert result["outcome"] == "manual"
    assert "anonymous email gate" in result["reason"]
    assert page.events == ["click:button:has-text('Apply')", "click:legal-disclaimer-label"]
    assert page.next_clicks == 0
    assert page.uploaded_to is None
    assert page.submit_clicks == 0


def test_oracle_anonymous_email_gate_uses_exact_accessible_next_button(fake_oracle, pdf):
    page = fake_oracle("email_gate_accessible_next")

    result = apply_oraclecloud(ORACLE_JOB_URL, pdf, "oracle-accessible-next", dry_run=True)

    assert page.role_queries == [{"role": "button", "name": "Next", "exact": True}]
    assert page.next_clicks == 1
    assert page.uploaded_to == "resume"
    assert result["ok"] is True
    assert result["submitted"] is False
    assert result["reason"] == "dry run - did not submit"
    assert page.submit_clicks == 0


def test_oracle_anonymous_email_gate_rejects_multiple_visible_exact_accessible_next_buttons(fake_oracle, pdf):
    page = fake_oracle("email_gate_duplicate_accessible_next")

    result = apply_oraclecloud(ORACLE_JOB_URL, pdf, "oracle-duplicate-accessible-next", dry_run=True)

    assert page.role_queries == [{"role": "button", "name": "Next", "exact": True}]
    assert result["outcome"] == "manual"
    assert "anonymous email gate" in result["reason"]
    assert page.next_clicks == 0
    assert page.uploaded_to is None
    assert page.submit_clicks == 0


def test_oracle_email_gate_missing_or_ambiguous_controls_fail_closed_without_next_or_submit(fake_oracle, pdf):
    for variant in (
        "email_gate_missing_email",
        "email_gate_missing_legal",
        "email_gate_missing_next",
        "email_gate_ambiguous_next",
    ):
        page = fake_oracle(variant)

        result = apply_oraclecloud(ORACLE_JOB_URL, pdf, f"oracle-{variant}", dry_run=True)

        assert result["outcome"] == "manual"
        assert "anonymous email gate" in result["reason"]
        assert page.next_clicks == 0
        assert page.submit_clicks == 0


def test_oracle_dry_run_waits_for_delayed_visible_apply(fake_oracle, pdf):
    page = fake_oracle("delayed_apply")

    result = apply_oraclecloud(ORACLE_JOB_URL, pdf, "oracle-delayed-apply", dry_run=True)

    assert page.apply_clicks == 1
    assert page.waits
    assert result["ok"] is True
    assert result["submitted"] is False
    assert result["reason"] == "dry run - did not submit"
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


def test_oracle_dry_run_requires_exact_visible_submit(fake_oracle, pdf):
    page = fake_oracle("anonymous")

    result = apply_oraclecloud(ORACLE_JOB_URL, pdf, "oracle-dry", dry_run=True)

    assert result["ok"] is True
    assert result["submitted"] is False
    assert result["reason"] == "dry run - did not submit"
    assert page.submit_clicks == 0
    assert "mark_submit_attempted" not in page.events


def test_oracle_missing_or_ambiguous_submit_is_manual_without_marker_or_click(fake_oracle, pdf):
    for variant in ("missing_submit", "ambiguous_submit"):
        page = fake_oracle(variant)

        result = apply_oraclecloud(ORACLE_JOB_URL, pdf, f"oracle-{variant}", dry_run=True)

        assert result["outcome"] == "manual"
        assert result["click_attempted"] is False
        assert result["submission_uncertain"] is False
        assert page.submit_clicks == 0
        assert "mark_submit_attempted" not in page.events


def test_oracle_live_submit_marks_immediately_before_one_click_and_confirms(fake_oracle, pdf, monkeypatch):
    page = fake_oracle("confirmed")
    monkeypatch.setattr(oraclecloud, "mark_submit_attempted", lambda: page.events.append("mark_submit_attempted"))

    result = apply_oraclecloud(ORACLE_JOB_URL, pdf, "oracle-confirmed", dry_run=False)

    assert page.events[-2:] == ["mark_submit_attempted", "click:submit"]
    assert page.submit_clicks == 1
    assert result["ok"] is True
    assert result["submitted"] is True
    assert result["reason"] == "confirmed"
    assert result["click_attempted"] is True
    assert result["submission_uncertain"] is False


def test_oracle_unconfirmed_submit_marks_uncertain_non_retryable(fake_oracle, pdf, monkeypatch):
    page = fake_oracle("anonymous")
    monkeypatch.setattr(oraclecloud, "mark_submit_attempted", lambda: page.events.append("mark_submit_attempted"))

    result = apply_oraclecloud(ORACLE_JOB_URL, pdf, "oracle-unconfirmed", dry_run=False)

    assert page.events[-2:] == ["mark_submit_attempted", "click:submit"]
    assert page.submit_clicks == 1
    assert result["outcome"] == "manual"
    assert result["ok"] is False
    assert result["submitted"] is False
    assert result["retryable"] is False
    assert result["click_attempted"] is True
    assert result["submission_uncertain"] is True


def test_oracle_click_timeout_after_marker_is_uncertain_non_retryable(fake_oracle, pdf, monkeypatch):
    page = fake_oracle("submit_timeout")
    monkeypatch.setattr(oraclecloud, "mark_submit_attempted", lambda: page.events.append("mark_submit_attempted"))

    result = apply_oraclecloud(ORACLE_JOB_URL, pdf, "oracle-timeout", dry_run=False)

    assert page.events[-2:] == ["mark_submit_attempted", "click:submit"]
    assert result["outcome"] == "manual"
    assert result["retryable"] is False
    assert result["click_attempted"] is True
    assert result["submission_uncertain"] is True
