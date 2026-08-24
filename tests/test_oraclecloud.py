import re
import sys
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
        if self.name == "verify_buttons":
            return self.page.verify_buttons[index]
        return self.page.file_inputs[index]

    def count(self):
        return self._count

    def is_visible(self):
        return self._visible

    def click(self, timeout=None, force=False):
        self.page.events.append(f"click:{self.name}")
        if self.page.variant == "email_gate_identity_verify_click_raises" and self.name == "verify":
            raise Exception("verify click failed")
        if self.on_click:
            self.on_click()

    def check(self, timeout=None, force=False):
        if self.page.variant in {
            "email_gate_hidden_legal_requires_label",
            "email_gate_missing_legal_label",
            "email_gate_ambiguous_legal_label",
            "email_gate_label_click_does_not_check_legal",
            "email_gate_legal_label_click_raises",
            "email_gate_legal_label_misses_proxy_toggles",
            "email_gate_missing_legal_proxy",
            "email_gate_ambiguous_legal_proxy",
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
        if self.page.variant == "email_gate_identity_pin_fill_raises" and self.name == "#pin-code-3":
            raise Exception("pin fill failed")
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
        self.verify_clicks = 0
        self.role_queries = []
        self.uploaded_to = None
        self.uploaded_file = None
        self.qa_evaluations = 0
        self.events = []
        self.url = ORACLE_JOB_URL
        self.app_page_index = 1
        self.filled_pages = []
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
        if self.variant == "multipage_closed_after_transition" and self.app_page_index == 2:
            return "This job is no longer accepting applications."
        if self.variant == "multipage_account_after_transition" and self.app_page_index == 2:
            return "Create an account to continue."
        if self.variant == "multipage_captcha_after_transition" and self.app_page_index == 2:
            return "Please verify you are human. reCAPTCHA"
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
        if self.variant.startswith("email_gate_identity") and self.next_clicks and not self.verify_clicks:
            return (
                "Confirm Your Identity The verification code was sent to this email address: "
                "Send New Code VERIFY"
            )
        if self.variant == "email_gate_identity_delayed_resume" and self.verify_clicks and len(self.waits) < 7:
            return (
                "Confirm Your Identity The verification code was sent to this email address: "
                "Send New Code VERIFY"
            )
        if self.variant == "email_gate_identity_unchanged" and self.verify_clicks:
            return (
                "Confirm Your Identity The verification code was sent to this email address: "
                "Send New Code VERIFY"
            )
        if self.variant == "email_gate_identity_rate_limited" and self.verify_clicks:
            return (
                "Too Many Attempts. Try Again Later. "
                "You reached the maximum number of attempts. Try again in 30 minutes. CONTINUE"
            )
        if self.variant.startswith("multipage"):
            return f"Example application form page {self.app_page_index}"
        return "Example application form"

    def locator(self, selector):
        if selector == "input[type=file]":
            identity_waiting = self.variant.startswith("email_gate_identity") and self.verify_clicks
            delayed_ready = self.variant == "email_gate_identity_delayed_resume" and len(self.waits) >= 7
            count = len(self.file_inputs)
            if self.variant.startswith("email_gate") and not self.next_clicks:
                count = 0
            elif identity_waiting and not delayed_ready and self.variant != "email_gate_identity":
                count = 0
            return _FakeLocator(self, "files", count=count)
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
                "email_gate_legal_label_misses_proxy_toggles",
                "email_gate_missing_legal_proxy",
                "email_gate_ambiguous_legal_proxy",
                "email_gate_identity",
                "email_gate_identity_missing_pin",
                "email_gate_identity_ambiguous_pin",
                "email_gate_identity_ambiguous_verify",
                "email_gate_identity_unchanged",
                "email_gate_identity_delayed_resume",
                "email_gate_identity_rate_limited",
                "email_gate_identity_pin_fill_raises",
                "email_gate_identity_verify_click_raises",
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
                "email_gate_legal_label_misses_proxy_toggles",
                "email_gate_missing_legal_proxy",
                "email_gate_ambiguous_legal_proxy",
                "email_gate_identity",
                "email_gate_identity_missing_pin",
                "email_gate_identity_ambiguous_pin",
                "email_gate_identity_ambiguous_verify",
                "email_gate_identity_unchanged",
                "email_gate_identity_delayed_resume",
                "email_gate_identity_rate_limited",
                "email_gate_identity_pin_fill_raises",
                "email_gate_identity_verify_click_raises",
            } and not self.next_clicks
            return _FakeLocator(self, "legal-disclaimer-checkbox", visible=False, count=1 if present else 0)
        if selector.startswith("#pin-code-"):
            if not self.variant.startswith("email_gate_identity") or not self.next_clicks:
                return _FakeLocator(self, selector, visible=False, count=0)
            if self.verify_clicks and self.variant != "email_gate_identity_delayed_resume":
                return _FakeLocator(self, selector, visible=False, count=0)
            digit = selector.rsplit("-", 1)[-1]
            if self.variant == "email_gate_identity_missing_pin" and digit == "6":
                return _FakeLocator(self, selector, visible=False, count=0)
            count = 2 if self.variant == "email_gate_identity_ambiguous_pin" and digit == "3" else 1
            return _FakeLocator(self, selector, visible=True, count=count, attrs={
                "type": "number",
                "autocomplete": "off",
                "aria-label": f"Enter verification code digit {digit} of six.",
            })
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
                if self.variant not in {
                    "email_gate_label_click_does_not_check_legal",
                    "email_gate_legal_label_misses_proxy_toggles",
                    "email_gate_missing_legal_proxy",
                    "email_gate_ambiguous_legal_proxy",
                }:
                    self.checked["legal-disclaimer-checkbox"] = {"via": "label"}

            return _FakeLocator(self, "legal-disclaimer-label", visible=True, count=count, on_click=click_label)
        if selector == "label[for='legal-disclaimer-checkbox'] .apply-flow-input-checkbox__button":
            if not self.variant.startswith("email_gate") or self.next_clicks:
                return _FakeLocator(self, "legal-disclaimer-proxy", visible=False, count=0)
            if self.variant == "email_gate_missing_legal_proxy":
                count = 0
            elif self.variant == "email_gate_ambiguous_legal_proxy":
                count = 2
            else:
                count = 1

            def click_proxy():
                self.checked["legal-disclaimer-checkbox"] = {"via": "proxy"}

            return _FakeLocator(self, "legal-disclaimer-proxy", visible=True, count=count, on_click=click_proxy)
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
            if self.variant.startswith("multipage"):
                visible = selector == oraclecloud.SUBMIT_SELECTORS[0] and self.app_page_index == self._multipage_total_pages()
            else:
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
        if role == "button" and name == "Next" and exact is True and self.variant.startswith("multipage"):
            if self.app_page_index >= self._multipage_total_pages():
                count = 0
            elif self.variant == "multipage_ambiguous_next" and self.app_page_index == 1:
                count = 2
            else:
                count = 1
            self.next_buttons = [_FakeLocator(self, "next", visible=True, on_click=self._click_next) for _ in range(count)]
            return _FakeLocator(self, "next_buttons", visible=count > 0, count=count)
        if role == "button" and name == "Next" and exact is True and self.variant.startswith("email_gate") and not self.next_clicks:
            if self.variant == "email_gate_missing_next":
                count = 0
            elif self.variant in {"email_gate_ambiguous_next", "email_gate_duplicate_accessible_next"}:
                count = 2
            else:
                count = 1
            self.next_buttons = [_FakeLocator(self, "next", visible=True, on_click=self._click_next) for _ in range(count)]
            return _FakeLocator(self, "next_buttons", visible=count > 0, count=count)
        if role == "button" and name == "Verify" and exact is True:
            if not self.variant.startswith("email_gate_identity") or not self.next_clicks or self.verify_clicks:
                return _FakeLocator(self, "verify_buttons", visible=False, count=0)
            count = 2 if self.variant == "email_gate_identity_ambiguous_verify" else 1
            self.verify_buttons = [_FakeLocator(self, "verify", visible=True, on_click=self._click_verify) for _ in range(count)]
            return _FakeLocator(self, "verify_buttons", visible=count > 0, count=count)
        return _FakeLocator(self, str(name or role), count=0, visible=False)

    def get_by_label(self, label, exact=False):
        normalized = label.lower()
        if self.variant.startswith("multipage") and normalized.startswith("page "):
            return _FakeLocator(self, f"page_{self.app_page_index}_field")
        if "resume" in normalized and self.variant.startswith("email_gate_identity") and self.verify_clicks:
            delayed_ready = self.variant == "email_gate_identity_delayed_resume" and len(self.waits) >= 7
            if self.variant != "email_gate_identity" and not delayed_ready:
                return _FakeLocator(self, label, count=0, visible=False)
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
            if self.variant.startswith("multipage"):
                return [{"id": f"page{self.app_page_index}", "name": f"page{self.app_page_index}", "label": f"Page {self.app_page_index} field", "value": ""}]
            return [{"id": "workAuth", "name": "workAuth", "label": "Example eligibility question", "value": ""}]
        if "required" in text:
            if self.variant == "multipage_required_page_2" and self.app_page_index == 2:
                return ["Page 2 required"]
            return ["Example eligibility question"] if self.variant == "required" else []
        return []

    def _click_apply(self):
        self.apply_clicks += 1
        if self.variant.startswith("email_gate"):
            self.url = f"{ORACLE_JOB_URL}/apply/email"

    def _click_next(self):
        self.next_clicks += 1
        if self.variant.startswith("email_gate_identity"):
            self.url = f"{ORACLE_JOB_URL}/apply/email"
        elif self.variant.startswith("multipage"):
            if self.variant != "multipage_unchanged_after_next":
                self.app_page_index += 1
                self.url = f"{ORACLE_JOB_URL}/apply/page/{self.app_page_index}"
        else:
            self.url = f"{ORACLE_JOB_URL}/apply/resume"

    def _multipage_total_pages(self):
        if self.variant == "multipage_five_pages":
            return 5
        return 4

    def _click_verify(self):
        self.verify_clicks += 1
        if self.variant != "email_gate_identity_unchanged":
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
        monkeypatch.setattr(
            oraclecloud.qa,
            "get_answers",
            lambda controls, context=None: [{"id_or_name": controls[0].get("id") or "workAuth", "answer": "Synthetic option"}],
        )
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


def test_oracle_four_page_dry_run_clicks_next_three_times_uploads_once_and_aggregates_qa(fake_oracle, pdf):
    page = fake_oracle("multipage_four")

    result = apply_oraclecloud(ORACLE_JOB_URL, pdf, "oracle-multipage", dry_run=True)

    assert page.next_clicks == 3
    assert page.uploaded_file == str(pdf)
    assert page.qa_evaluations == 12
    assert result["reason"] == "dry run - did not submit"
    assert result["qa_filled"] == ["page1", "page2", "page3", "page4"]
    assert page.submit_clicks == 0
    assert "mark_submit_attempted" not in page.events


def test_oracle_required_field_on_page_two_stops_before_next(fake_oracle, pdf):
    page = fake_oracle("multipage_required_page_2")

    result = apply_oraclecloud(ORACLE_JOB_URL, pdf, "oracle-required-page-two", dry_run=True)

    assert result["outcome"] == "manual"
    assert result["unanswered"] == ["Page 2 required"]
    assert page.next_clicks == 1
    assert page.submit_clicks == 0


def test_oracle_qa_failure_on_middle_page_stops_before_next_and_surfaces_label(fake_oracle, pdf, monkeypatch):
    page = fake_oracle("multipage_four")

    def fail_page_two(page, controls, answers):
        if page.app_page_index == 2:
            return ([], ["Page 2 QA"])
        return ([a["id_or_name"] for a in answers], [])

    monkeypatch.setattr(oraclecloud.qa, "fill_answers", fail_page_two)

    result = apply_oraclecloud(ORACLE_JOB_URL, pdf, "oracle-qa-page-two", dry_run=True)

    assert result["outcome"] == "manual"
    assert result["unanswered"] == ["Page 2 QA"]
    assert result["qa_failed"] == ["Page 2 QA"]
    assert page.next_clicks == 1
    assert page.submit_clicks == 0


def test_oracle_ambiguous_next_fails_closed_without_transition(fake_oracle, pdf):
    page = fake_oracle("multipage_ambiguous_next")

    result = apply_oraclecloud(ORACLE_JOB_URL, pdf, "oracle-ambiguous-next", dry_run=True)

    assert result["outcome"] == "manual"
    assert "Next" in result["reason"]
    assert page.next_clicks == 0
    assert page.submit_clicks == 0


def test_oracle_unchanged_page_after_next_fails_closed(fake_oracle, pdf):
    page = fake_oracle("multipage_unchanged_after_next")

    result = apply_oraclecloud(ORACLE_JOB_URL, pdf, "oracle-unchanged-next", dry_run=True)

    assert result["outcome"] == "manual"
    assert "did not advance" in result["reason"]
    assert page.next_clicks == 1
    assert page.submit_clicks == 0


def test_oracle_more_than_four_pages_fails_closed_before_fourth_next(fake_oracle, pdf):
    page = fake_oracle("multipage_five_pages")

    result = apply_oraclecloud(ORACLE_JOB_URL, pdf, "oracle-five-pages", dry_run=True)

    assert result["outcome"] == "manual"
    assert "four" in result["reason"].lower()
    assert page.next_clicks == 3
    assert page.submit_clicks == 0


@pytest.mark.parametrize(
    ("variant", "reason"),
    [
        ("multipage_closed_after_transition", "no longer accepting applications"),
        ("multipage_account_after_transition", "account"),
        ("multipage_captcha_after_transition", "CAPTCHA"),
    ],
)
def test_oracle_gates_after_transition_stop_before_further_filling_or_clicking(fake_oracle, pdf, variant, reason):
    page = fake_oracle(variant)

    result = apply_oraclecloud(ORACLE_JOB_URL, pdf, variant, dry_run=True)

    assert result["outcome"] == ("stale" if "closed" in variant else "manual")
    assert reason.lower() in result["reason"].lower()
    assert page.next_clicks == 1
    assert page.qa_evaluations == 3
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



def test_oracle_identity_code_gate_fills_six_digits_and_advances_to_resume(fake_oracle, pdf, monkeypatch):
    page = fake_oracle("email_gate_identity")
    monkeypatch.setattr(oraclecloud, "_fetch_oracle_identity_code", lambda requested_at_ms, timeout_s=60: "123456")

    result = apply_oraclecloud(ORACLE_JOB_URL, pdf, "oracle-identity", dry_run=True)

    assert page.next_clicks == 1
    assert page.filled["#pin-code-1"] == "1"
    assert page.filled["#pin-code-6"] == "6"
    assert page.verify_clicks == 1
    assert {"role": "button", "name": "Verify", "exact": True} in page.role_queries
    assert "click:Send New Code" not in page.events
    assert page.uploaded_to == "resume"
    assert result["reason"] == "dry run - did not submit"
    assert page.submit_clicks == 0


def test_oracle_identity_code_gate_polls_full_window_before_classifying_unchanged(fake_oracle, pdf, monkeypatch):
    page = fake_oracle("email_gate_identity_delayed_resume")
    monkeypatch.setattr(oraclecloud, "_fetch_oracle_identity_code", lambda requested_at_ms, timeout_s=60: "123456")

    result = apply_oraclecloud(ORACLE_JOB_URL, pdf, "oracle-identity-delayed", dry_run=True)

    assert page.verify_clicks == 1
    assert page.waits.count(500) >= 4
    assert page.uploaded_to == "resume"
    assert result["reason"] == "dry run - did not submit"
    assert page.submit_clicks == 0


def test_oracle_identity_code_gate_pin_fill_exception_fails_closed_without_uncertain_submit(fake_oracle, pdf, monkeypatch):
    page = fake_oracle("email_gate_identity_pin_fill_raises")
    monkeypatch.setattr(oraclecloud, "_fetch_oracle_identity_code", lambda requested_at_ms, timeout_s=60: "123456")

    result = apply_oraclecloud(ORACLE_JOB_URL, pdf, "oracle-pin-fill-raises", dry_run=True)

    assert result["outcome"] == "manual"
    assert result["click_attempted"] is False
    assert result["submission_uncertain"] is False
    assert page.verify_clicks == 0
    assert page.uploaded_to is None
    assert page.submit_clicks == 0


def test_oracle_identity_code_gate_verify_click_exception_fails_closed_without_uncertain_submit(fake_oracle, pdf, monkeypatch):
    page = fake_oracle("email_gate_identity_verify_click_raises")
    monkeypatch.setattr(oraclecloud, "_fetch_oracle_identity_code", lambda requested_at_ms, timeout_s=60: "123456")

    result = apply_oraclecloud(ORACLE_JOB_URL, pdf, "oracle-verify-click-raises", dry_run=True)

    assert result["outcome"] == "manual"
    assert result["click_attempted"] is False
    assert result["submission_uncertain"] is False
    assert page.verify_clicks == 0
    assert page.uploaded_to is None
    assert page.submit_clicks == 0


def test_oracle_identity_code_gate_rejects_absent_stale_or_malformed_code(fake_oracle, pdf, monkeypatch):
    for code in (None, "12345", "123456 654321"):
        page = fake_oracle("email_gate_identity")
        monkeypatch.setattr(oraclecloud, "_fetch_oracle_identity_code", lambda requested_at_ms, timeout_s=60, code=code: code)

        result = apply_oraclecloud(ORACLE_JOB_URL, pdf, f"oracle-code-{code}", dry_run=True)

        assert result["outcome"] == "manual"
        assert "Oracle identity verification" in result["reason"]
        assert page.verify_clicks == 0
        assert page.uploaded_to is None
        assert page.submit_clicks == 0


def test_oracle_identity_code_fetch_rejects_stale_ambiguous_malformed_and_gmail_errors(monkeypatch):
    class FakeMailer:
        def __init__(self, messages):
            self.messages = messages

        def _call(self, path):
            if path.startswith("/messages?"):
                return {"messages": [{"id": key} for key in self.messages]}
            key = path.split("/messages/", 1)[1].split("?", 1)[0]
            value = self.messages[key]
            if isinstance(value, Exception):
                raise value
            return value

        def extract_plain(self, full):
            return full.get("body", "")

    def install_mailer(messages):
        monkeypatch.setitem(sys.modules, "mailer", FakeMailer(messages))

    profile_email = oraclecloud.PROFILE["email"]
    good = {
        "internalDate": "2000",
        "payload": {"headers": [
            {"name": "Subject", "value": "Please confirm your identity"},
            {"name": "From", "value": "Amex Careers <careers@recruitment.americanexpress.com>"},
            {"name": "To", "value": profile_email},
        ]},
        "body": "confirm your identity using the one-time passcode below: 123456",
    }
    install_mailer({"good": good})
    assert oraclecloud._fetch_oracle_identity_code(1900, timeout_s=0) == "123456"

    stale = dict(good, internalDate="1000")
    install_mailer({"stale": stale})
    assert oraclecloud._fetch_oracle_identity_code(40000, timeout_s=0) is None

    ambiguous = dict(good, body="confirm your identity using the one-time passcode below: 123456 and 654321")
    install_mailer({"ambiguous": ambiguous})
    assert oraclecloud._fetch_oracle_identity_code(1900, timeout_s=0) is None

    malformed = dict(good, body="confirm your identity using the one-time passcode below: 12345")
    install_mailer({"malformed": malformed})
    assert oraclecloud._fetch_oracle_identity_code(1900, timeout_s=0) is None

    install_mailer({"boom": RuntimeError("gmail failed")})
    assert oraclecloud._fetch_oracle_identity_code(1900, timeout_s=0) is None


def test_oracle_identity_code_fetch_accepts_other_oracle_tenant_sender(monkeypatch):
    class FakeMailer:
        def _call(self, path):
            if path.startswith("/messages?"):
                return {"messages": [{"id": "good"}]}
            return {
                "internalDate": "2000",
                "payload": {"headers": [
                    {"name": "Subject", "value": "Please confirm your identity"},
                    {"name": "From", "value": "Example Careers <careers@example.oraclecloud.invalid>"},
                    {"name": "To", "value": oraclecloud.PROFILE["email"]},
                ]},
                "body": "confirm your identity using the one-time passcode below: 123456",
            }

        def extract_plain(self, full):
            return full.get("body", "")

    monkeypatch.setitem(sys.modules, "mailer", FakeMailer())

    assert oraclecloud._fetch_oracle_identity_code(1900, timeout_s=0) == "123456"


def test_oracle_identity_code_fetch_rejects_near_match_recipient(monkeypatch):
    class FakeMailer:
        def _call(self, path):
            if path.startswith("/messages?"):
                return {"messages": [{"id": "near"}]}
            return {
                "internalDate": "2000",
                "payload": {"headers": [
                    {"name": "Subject", "value": "Please confirm your identity"},
                    {"name": "From", "value": "Example Careers <careers@example.oraclecloud.invalid>"},
                    {"name": "To", "value": f"prefix-{oraclecloud.PROFILE['email']}"},
                    {"name": "Cc", "value": "other@example.invalid"},
                    {"name": "Delivered-To", "value": "different@example.invalid"},
                ]},
                "body": "confirm your identity using the one-time passcode below: 123456",
            }

        def extract_plain(self, full):
            return full.get("body", "")

    monkeypatch.setitem(sys.modules, "mailer", FakeMailer())

    assert oraclecloud._fetch_oracle_identity_code(1900, timeout_s=0) is None


def test_oracle_identity_code_fetch_rejects_three_second_prior_code_but_accepts_current(monkeypatch):
    class FakeMailer:
        def _call(self, path):
            if path.startswith("/messages?"):
                return {"messages": [{"id": "stale"}, {"id": "current"}]}
            key = path.split("/messages/", 1)[1].split("?", 1)[0]
            return messages[key]

        def extract_plain(self, full):
            return full.get("body", "")

    def message(internal_date, code):
        return {
            "internalDate": str(internal_date),
            "payload": {"headers": [
                {"name": "Subject", "value": "Please confirm your identity"},
                {"name": "From", "value": "Example Careers <careers@example.oraclecloud.invalid>"},
                {"name": "To", "value": oraclecloud.PROFILE["email"]},
            ]},
            "body": f"confirm your identity using the one-time passcode below: {code}",
        }

    requested_at = 10_000
    messages = {"stale": message(requested_at - 3000, "111111")}
    monkeypatch.setitem(sys.modules, "mailer", FakeMailer())
    assert oraclecloud._fetch_oracle_identity_code(requested_at, timeout_s=0) is None

    messages["current"] = message(requested_at, "222222")
    assert oraclecloud._fetch_oracle_identity_code(requested_at, timeout_s=0) == "222222"


def test_oracle_identity_code_gate_rejects_missing_or_ambiguous_pin_controls(fake_oracle, pdf, monkeypatch):
    monkeypatch.setattr(oraclecloud, "_fetch_oracle_identity_code", lambda requested_at_ms, timeout_s=60: "123456")
    for variant in ("email_gate_identity_missing_pin", "email_gate_identity_ambiguous_pin"):
        page = fake_oracle(variant)

        result = apply_oraclecloud(ORACLE_JOB_URL, pdf, f"oracle-{variant}", dry_run=True)

        assert result["outcome"] == "manual"
        assert "Oracle identity verification" in result["reason"]
        assert page.verify_clicks == 0
        assert page.submit_clicks == 0


def test_oracle_identity_code_gate_rejects_ambiguous_verify_without_send_new_code(fake_oracle, pdf, monkeypatch):
    page = fake_oracle("email_gate_identity_ambiguous_verify")
    monkeypatch.setattr(oraclecloud, "_fetch_oracle_identity_code", lambda requested_at_ms, timeout_s=60: "123456")

    result = apply_oraclecloud(ORACLE_JOB_URL, pdf, "oracle-ambiguous-verify", dry_run=True)

    assert result["outcome"] == "manual"
    assert "Oracle identity verification" in result["reason"]
    assert page.verify_clicks == 0
    assert "click:Send New Code" not in page.events
    assert page.submit_clicks == 0


def test_oracle_identity_code_gate_rejects_unchanged_gate_after_verify(fake_oracle, pdf, monkeypatch):
    page = fake_oracle("email_gate_identity_unchanged")
    monkeypatch.setattr(oraclecloud, "_fetch_oracle_identity_code", lambda requested_at_ms, timeout_s=60: "123456")

    result = apply_oraclecloud(ORACLE_JOB_URL, pdf, "oracle-identity-unchanged", dry_run=True)

    assert result["outcome"] == "manual"
    assert "Oracle identity verification" in result["reason"]
    assert page.verify_clicks == 1
    assert page.uploaded_to is None
    assert page.submit_clicks == 0


def test_oracle_identity_code_gate_classifies_rate_limit_as_retryable_manual(fake_oracle, pdf, monkeypatch):
    page = fake_oracle("email_gate_identity_rate_limited")
    monkeypatch.setattr(oraclecloud, "_fetch_oracle_identity_code", lambda requested_at_ms, timeout_s=60: "123456")

    result = apply_oraclecloud(ORACLE_JOB_URL, pdf, "oracle-rate-limit", dry_run=True)

    assert result["outcome"] == "manual"
    assert result["retryable"] is True
    assert result["click_attempted"] is False
    assert result["submission_uncertain"] is False
    assert result["reason"] == "Oracle identity verification rate limited; retry after 30 minutes"
    assert page.verify_clicks == 1
    assert page.uploaded_to is None
    assert page.submit_clicks == 0

def test_oracle_anonymous_email_gate_clicks_exact_visible_legal_proxy_when_hidden_input_is_outside_viewport(fake_oracle, pdf):
    page = fake_oracle("email_gate_hidden_legal_requires_label")

    result = apply_oraclecloud(ORACLE_JOB_URL, pdf, "oracle-email-gate-hidden-legal", dry_run=True)

    assert page.events == ["click:button:has-text('Apply')", "click:legal-disclaimer-proxy", "click:next"]
    assert page.checked["legal-disclaimer-checkbox"] == {"via": "proxy"}
    assert page.next_clicks == 1
    assert page.uploaded_to == "resume"
    assert result["ok"] is True
    assert result["submitted"] is False
    assert result["reason"] == "dry run - did not submit"
    assert page.submit_clicks == 0


def test_oracle_email_gate_legal_label_fallback_fail_closed_when_missing_or_ambiguous(fake_oracle, pdf):
    for variant in (
        "email_gate_missing_legal_label",
        "email_gate_ambiguous_legal_label",
    ):
        page = fake_oracle(variant)

        result = apply_oraclecloud(ORACLE_JOB_URL, pdf, f"oracle-{variant}", dry_run=True)

        assert result["outcome"] == "manual"
        assert "anonymous email gate" in result["reason"]
        assert page.next_clicks == 0
        assert page.uploaded_to is None
        assert page.submit_clicks == 0


def test_oracle_email_gate_legal_proxy_fallback_does_not_click_label_when_label_click_would_raise(fake_oracle, pdf):
    page = fake_oracle("email_gate_legal_label_click_raises")

    result = apply_oraclecloud(ORACLE_JOB_URL, pdf, "oracle-legal-label-click-raises", dry_run=True)

    assert page.events == ["click:button:has-text('Apply')", "click:legal-disclaimer-proxy", "click:next"]
    assert page.checked["legal-disclaimer-checkbox"] == {"via": "proxy"}
    assert page.next_clicks == 1
    assert page.uploaded_to == "resume"
    assert result["ok"] is True
    assert result["submitted"] is False
    assert result["reason"] == "dry run - did not submit"
    assert page.submit_clicks == 0


def test_oracle_anonymous_email_gate_clicks_exact_visible_proxy_when_label_click_does_not_toggle(fake_oracle, pdf):
    page = fake_oracle("email_gate_legal_label_misses_proxy_toggles")

    result = apply_oraclecloud(ORACLE_JOB_URL, pdf, "oracle-legal-proxy-toggles", dry_run=True)

    assert page.events == ["click:button:has-text('Apply')", "click:legal-disclaimer-proxy", "click:next"]
    assert page.checked["legal-disclaimer-checkbox"] == {"via": "proxy"}
    assert page.next_clicks == 1
    assert page.uploaded_to == "resume"
    assert result["ok"] is True
    assert result["submitted"] is False
    assert result["reason"] == "dry run - did not submit"
    assert page.submit_clicks == 0


def test_oracle_email_gate_legal_proxy_fallback_fail_closed_when_missing_or_ambiguous(fake_oracle, pdf):
    for variant in ("email_gate_missing_legal_proxy", "email_gate_ambiguous_legal_proxy"):
        page = fake_oracle(variant)

        result = apply_oraclecloud(ORACLE_JOB_URL, pdf, f"oracle-{variant}", dry_run=True)

        assert result["outcome"] == "manual"
        assert "anonymous email gate" in result["reason"]
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


def test_shared_qa_passes_skip_populated_controls_but_fill_empty_and_label_only(monkeypatch):
    controls = [
        {"id": "address-line-1", "name": "", "label": "Address Line 1", "value": "Imported tenant address", "chosen": ""},
        {"id": "city", "name": "city", "label": "City", "value": "", "chosen": ""},
        {"id": "", "name": "", "label": "Start date", "value": "", "chosen": ""},
    ]
    answers = [
        {"id_or_name": "address-line-1", "label": "Address Line 1", "answer": "Generated address must not overwrite"},
        {"id_or_name": "city", "label": "City", "answer": "Austin"},
        {"id_or_name": "Start date", "label": "Start date", "answer": "Immediately"},
    ]
    fill_calls = []

    class Page:
        def __init__(self):
            self.pass_index = 0

        def evaluate(self, script):
            assert script == oraclecloud.qa.EXTRACT_JS
            return [dict(control) for control in controls]

        def wait_for_timeout(self, value):
            self.pass_index += 1

    def fake_get_answers(seen_controls, context):
        assert seen_controls == controls
        assert context == {"slug": "oracle-task-2", "url": ORACLE_JOB_URL}
        return list(answers)

    def fake_fill_answers(page, seen_controls, todo):
        fill_calls.append([answer["label"] for answer in todo])
        for answer in todo:
            matched = next(
                control for control in controls
                if answer["id_or_name"] in {control["id"], control["name"]}
                or answer["label"] == control["label"]
            )
            matched["value"] = answer["answer"]
        return [answer["label"] for answer in todo], []

    monkeypatch.setattr(oraclecloud.qa, "get_answers", fake_get_answers)
    monkeypatch.setattr(oraclecloud.qa, "fill_answers", fake_fill_answers)

    filled, failed = oraclecloud._run_shared_qa_passes(Page(), "oracle-task-2", ORACLE_JOB_URL)

    assert fill_calls == [["City", "Start date"], [], []]
    assert controls[0]["value"] == "Imported tenant address"
    assert controls[1]["value"] == "Austin"
    assert controls[2]["value"] == "Immediately"
    assert filled == ["City", "Start date"]
    assert failed == []


class _OracleControlLocator:
    def __init__(self, page, name, *, value="", visible=True, attrs=None, count=1, text=""):
        self.page = page
        self.name = name
        self._value = value
        self._visible = visible
        self.attrs = attrs or {}
        self._count = count
        self.text = text

    @property
    def first(self):
        return self.nth(0)

    def nth(self, index):
        if self.name == "collection":
            return self.page.collection[index]
        if self.name == "suggestions":
            return self.page.suggestions[index]
        return self

    def count(self): return self._count
    def is_visible(self): return self._visible
    def input_value(self): return self._value
    def fill(self, value):
        self.page.events.append(f"fill:{self.name}:{value}")
        self._value = value
    def press_sequentially(self, value, delay=None):
        self.page.events.append(f"press:{self.name}:{value}")
        self._value = value
    def click(self, timeout=None):
        self.page.events.append(f"click:{self.name}")
        if self.name.startswith("suggestion"):
            self.page.address._value = self.page.street
    def get_attribute(self, name): return self.attrs.get(name)
    def inner_text(self, timeout=None): return self.text


class _OracleControlsPage:
    def __init__(self, controls=None, suggestions=None, street="123 Example Ave"):
        self.events = []
        self.street = street
        self.all_controls = controls or []
        self.collection = list(self.all_controls)
        self.suggestions = suggestions or []
        self.address = _OracleControlLocator(self, "address", value="", attrs={"type": "text"})

    def get_by_label(self, label, exact=False):
        normalized = label.lower()
        if normalized in {"phone", "phone number", "mobile"}:
            self.collection = [control for control in self.all_controls if "phone" in control.attrs.get("label", "").lower()]
            return _OracleControlLocator(self, "collection", count=len(self.collection))
        if exact and normalized == "address line 1":
            self.collection = [self.address]
            return _OracleControlLocator(self, "collection", count=1)
        return _OracleControlLocator(self, label, visible=False, count=0)

    def locator(self, selector):
        if "role='option'" in selector or 'role="option"' in selector or "[role=option]" in selector:
            return _OracleControlLocator(self, "suggestions", count=len(self.suggestions))
        return _OracleControlLocator(self, selector, count=0, visible=False)

    def wait_for_timeout(self, value): self.events.append(f"wait:{value}")


def test_oracle_phone_skips_country_code_and_fills_one_national_digits_candidate(monkeypatch):
    controls = [
        _OracleControlLocator(None, "country", attrs={"label": "Phone Country code", "role": "combobox", "type": "text"}),
        _OracleControlLocator(None, "phone", attrs={"label": "Phone Number", "type": "tel"}),
    ]
    page = _OracleControlsPage(controls)
    for control in controls: control.page = page
    monkeypatch.setattr(oraclecloud, "PROFILE", {"phone": "+1 (555) 010-2345", "name": {}, "links": {}})

    oraclecloud._fill_basics(page)

    assert not any(event.startswith("fill:country") for event in page.events)
    assert controls[1].input_value() == "5550102345"


def test_oracle_phone_preserves_nonempty_existing_value(monkeypatch):
    phone = _OracleControlLocator(None, "phone", value="already set", attrs={"label": "Phone", "type": "tel"})
    page = _OracleControlsPage([phone]); phone.page = page
    monkeypatch.setattr(oraclecloud, "PROFILE", {"phone": "5550102345", "name": {}, "links": {}})

    oraclecloud._fill_basics(page)

    assert phone.input_value() == "already set"
    assert page.events == []


def test_oracle_phone_ambiguous_candidates_fail_closed(monkeypatch):
    one = _OracleControlLocator(None, "phone1", attrs={"label": "Phone", "type": "tel"})
    two = _OracleControlLocator(None, "phone2", attrs={"label": "Phone Number", "type": "text"})
    page = _OracleControlsPage([one, two]); one.page = two.page = page
    monkeypatch.setattr(oraclecloud, "PROFILE", {"phone": "5550102345", "name": {}, "links": {}})

    oraclecloud._fill_basics(page)

    assert one.input_value() == ""
    assert two.input_value() == ""
    assert page.events == []


def test_oracle_address_preserves_populated_line_without_keypress_or_click(monkeypatch):
    page = _OracleControlsPage(street="123 Example Ave")
    page.address._value = "Imported address"
    monkeypatch.setattr(oraclecloud, "PROFILE", {"address": {"street": "123 Example Ave"}})

    assert oraclecloud._fill_oracle_address_line1(page) is False

    assert page.address.input_value() == "Imported address"
    assert page.events == []


def test_oracle_address_types_street_selects_unique_matching_visible_suggestion(monkeypatch):
    page = _OracleControlsPage(street="123 Example Ave")
    page.suggestions = [_OracleControlLocator(page, "suggestion-1", text="123 Example Ave, Example City, ST")]
    monkeypatch.setattr(oraclecloud, "PROFILE", {"address": {"street": "123 Example Ave"}})

    assert oraclecloud._fill_oracle_address_line1(page) is True

    assert page.events[:2] == ["press:address:123 Example Ave", "wait:600"]
    assert page.events[-1] == "click:suggestion-1"
    assert page.address.input_value() == "123 Example Ave"


@pytest.mark.parametrize("suggestions", [[], ["456 Other Rd"], ["123 Example Ave", "123 Example Ave Apt 2"]])
def test_oracle_address_suggestion_zero_nonmatching_or_ambiguous_fails_closed(monkeypatch, suggestions):
    page = _OracleControlsPage(street="123 Example Ave")
    page.suggestions = [_OracleControlLocator(page, f"suggestion-{i}", text=text) for i, text in enumerate(suggestions)]
    monkeypatch.setattr(oraclecloud, "PROFILE", {"address": {"street": "123 Example Ave"}})

    assert oraclecloud._fill_oracle_address_line1(page) is False

    assert page.address.input_value() == "123 Example Ave"
    assert not any(event.startswith("click:suggestion") for event in page.events)


def test_oracle_owned_answer_filter_prevents_optional_phone_qa_failed(monkeypatch):
    controls = [{"id": "workAuth", "name": "workAuth", "label": "Work authorization", "value": "", "chosen": ""}]
    answers = [
        {"id_or_name": "phone", "label": "phone", "answer": "5550102345", "required": False},
        {"id_or_name": "workAuth", "label": "Work authorization", "answer": "Yes", "required": True},
    ]
    calls = []
    class Page:
        def evaluate(self, script): return controls
        def wait_for_timeout(self, value): pass
    monkeypatch.setattr(oraclecloud.qa, "get_answers", lambda seen, context: list(answers))
    def fake_fill(page, seen, todo):
        calls.append([a["label"] for a in todo])
        return [a["label"] for a in todo], ["phone"]
    monkeypatch.setattr(oraclecloud.qa, "fill_answers", fake_fill)

    filled, failed = oraclecloud._run_shared_qa_passes(Page(), "slug", ORACLE_JOB_URL)

    assert calls == [["Work authorization"], ["Work authorization"], ["Work authorization"]]
    assert failed == []


def test_oracle_required_owned_empty_address_still_blocks_adapter(fake_oracle, pdf, monkeypatch):
    page = fake_oracle("anonymous")
    monkeypatch.setattr(oraclecloud, "_fill_oracle_address_line1", lambda page: False)
    original_evaluate = page.evaluate
    def evaluate(script):
        if script == oraclecloud.REQUIRED_EMPTY_JS: return ["Address Line 1 *"]
        return original_evaluate(script)
    page.evaluate = evaluate

    result = apply_oraclecloud(ORACLE_JOB_URL, pdf, "oracle-required-address", dry_run=True)

    assert result["outcome"] == "manual"
    assert result["submitted"] is False
    assert "Address Line 1 *" in result["unanswered"]
