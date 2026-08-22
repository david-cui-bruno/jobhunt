import sys
import unittest
from contextlib import contextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apply"))
import ashby  # noqa: E402
from ashby import _fill_basics  # noqa: E402


class _Element:
    def __init__(self, present=True, value=""):
        self.first = self
        self.present = present
        self.value = value

    def count(self):
        return int(self.present)

    def is_visible(self):
        return self.present

    def input_value(self):
        return self.value

    def fill(self, value):
        self.value = value


class _SplitNamePage:
    def __init__(self):
        self.fields = {
            "First Name": _Element(),
            "Last Name": _Element(),
            "Email": _Element(),
            "Phone": _Element(),
            "LinkedIn": _Element(),
            "GitHub": _Element(),
        }
        self.calls = []

    def get_by_label(self, label, exact=False):
        self.calls.append((label, exact))
        return self.fields.get(label, _Element(present=False))


class AshbyBasicFieldTests(unittest.TestCase):
    PROFILE = {
        "name": {"first": "David", "last": "Cui"},
        "email": "david@example.com",
        "phone": "555-0100",
        "links": {
            "linkedin": "https://www.linkedin.com/in/david",
            "github": "https://github.com/david",
        },
    }

    def test_split_name_form_fills_both_names_without_full_name_fallback(self):
        page = _SplitNamePage()

        _fill_basics(page, self.PROFILE)

        self.assertEqual("David", page.fields["First Name"].value)
        self.assertEqual("Cui", page.fields["Last Name"].value)
        self.assertIn(("Name", True), page.calls)

    def test_existing_value_is_not_overwritten(self):
        page = _SplitNamePage()
        page.fields["First Name"].value = "Already Present"

        _fill_basics(page, self.PROFILE)

        self.assertEqual("Already Present", page.fields["First Name"].value)
        self.assertEqual("Cui", page.fields["Last Name"].value)

    def test_combined_name_form_remains_supported(self):
        page = _SplitNamePage()
        page.fields = {"Name": _Element()}

        _fill_basics(page, self.PROFILE)

        self.assertEqual("David Cui", page.fields["Name"].value)

    def test_explicit_full_name_form_is_supported(self):
        page = _SplitNamePage()
        page.fields = {"Full Name": _Element()}

        _fill_basics(page, self.PROFILE)

        self.assertEqual("David Cui", page.fields["Full Name"].value)
        self.assertIn(("Full Name", True), page.calls)


class FakeFileInput:
    def __init__(self, page):
        self.page = page
        self.first = self

    def set_input_files(self, path):
        self.page.events.append(("upload", path))
        if self.page.upload_error:
            raise self.page.upload_error


class FakeSubmitButton:
    def __init__(self, page):
        self.page = page
        self.first = self

    def click(self, timeout=None):
        self.page.events.append(("click", timeout))
        if self.page.click_error:
            raise self.page.click_error


class FakeAshbyPage(_SplitNamePage):
    def __init__(self, *, required_empty=None, body="", page_url="https://jobs.ashbyhq.com/acme/job/application"):
        super().__init__()
        self.required_empty = [] if required_empty is None else required_empty
        self.body = body
        self.url = page_url
        self.upload_error = None
        self.click_error = None
        self.events = []
        self.screenshots = []
        self.evaluate_calls = []
        self.goto_urls = []

    def goto(self, url, wait_until=None, timeout=None):
        self.goto_urls.append(url)

    def wait_for_timeout(self, ms):
        self.events.append(("wait", ms))

    def locator(self, selector):
        if selector == "input[type=file]":
            return FakeFileInput(self)
        if "Submit application" in selector:
            return FakeSubmitButton(self)
        raise AssertionError(f"unexpected selector {selector}")

    def evaluate(self, script):
        self.evaluate_calls.append(script)
        if script == ashby.qa.EXTRACT_JS:
            return [{"id": "work_auth", "name": "", "value": "", "chosen": False}]
        return self.required_empty

    def screenshot(self, path, full_page=True, timeout=None):
        self.screenshots.append((path, full_page, timeout))

    def inner_text(self, selector):
        assert selector == "body"
        return self.body


class FakeCtx:
    def __init__(self, pages=None):
        self.pages = list(pages or [])
        self.created = []

    def new_page(self):
        page = FakeAshbyPage()
        self.created.append(page)
        self.pages.append(page)
        return page


class FakePlaywright:
    pass


def _fake_sync_playwright():
    @contextmanager
    def manager():
        yield FakePlaywright()
    return manager()


def _patch_form_dependencies(monkeypatch, *, attempted=None):
    monkeypatch.setattr(ashby, "PROFILE", AshbyBasicFieldTests.PROFILE)
    monkeypatch.setattr(ashby.qa, "harvest_select_options", lambda page, controls: page.events.append("harvest"))
    monkeypatch.setattr(
        ashby.qa,
        "get_answers",
        lambda controls, context: [{"id_or_name": "work_auth", "value": "Yes", "context": context}],
    )
    monkeypatch.setattr(
        ashby.qa,
        "fill_answers",
        lambda page, controls, todo: ([a["id_or_name"] for a in todo], []),
    )
    if attempted is not None:
        monkeypatch.setattr(ashby, "mark_submit_attempted", lambda: attempted.append("attempted"))


def test_apply_ashby_uses_persistent_context_and_reuses_existing_page(monkeypatch, tmp_path):
    calls = []
    page = FakeAshbyPage()
    ctx = FakeCtx([page])

    @contextmanager
    def fake_persistent(pw):
        calls.append("enter")
        try:
            yield ctx
        finally:
            calls.append("exit")

    monkeypatch.setattr(ashby, "sync_playwright", _fake_sync_playwright)
    monkeypatch.setattr(ashby, "persistent_ashby_context", fake_persistent)
    monkeypatch.setattr(ashby, "configure_page", lambda p: calls.append(("configure", p)) or p)
    monkeypatch.setattr(ashby, "_run_ashby_form", lambda *args: {"ok": True, "submitted": False, "reason": "dry run"})
    assert not hasattr(ashby, "stealth")

    result = ashby.apply_ashby("https://jobs.ashbyhq.com/acme/job", tmp_path / "resume.pdf", "acme", dry_run=True)

    assert calls == ["enter", ("configure", page), "exit"]
    assert ctx.created == []
    assert result["submitted"] is False


def test_apply_ashby_creates_page_when_context_is_empty_and_exits_on_exception(monkeypatch, tmp_path):
    calls = []
    ctx = FakeCtx([])

    @contextmanager
    def fake_persistent(pw):
        calls.append("enter")
        try:
            yield ctx
        finally:
            calls.append("exit")

    monkeypatch.setattr(ashby, "sync_playwright", _fake_sync_playwright)
    monkeypatch.setattr(ashby, "persistent_ashby_context", fake_persistent)
    monkeypatch.setattr(ashby, "configure_page", lambda p: p)
    monkeypatch.setattr(ashby, "_run_ashby_form", lambda *args: (_ for _ in ()).throw(RuntimeError("boom")))

    with pytest.raises(RuntimeError):
        ashby.apply_ashby("https://jobs.ashbyhq.com/acme/job", tmp_path / "resume.pdf", "acme")

    assert len(ctx.created) == 1
    assert calls == ["enter", "exit"]


def test_run_ashby_form_uploads_fills_qa_screenshots_and_dry_run_does_not_click(monkeypatch, tmp_path):
    _patch_form_dependencies(monkeypatch, attempted=[])
    page = FakeAshbyPage()

    result = ashby._run_ashby_form(page, "https://jobs.ashbyhq.com/acme/job?embed=1", tmp_path / "resume.pdf", "slug", True)

    assert page.goto_urls == ["https://jobs.ashbyhq.com/acme/job/application"]
    assert ("upload", str(tmp_path / "resume.pdf")) in page.events
    assert page.fields["First Name"].value == "David"
    assert "harvest" in page.events
    assert result["qa_filled"] == ["work_auth"]
    assert result["unanswered"] == []
    assert result["ok"] is True
    assert result["submitted"] is False
    assert result["reason"] == "dry run — did not submit"
    assert not [event for event in page.events if event[0] == "click"]
    assert page.screenshots and page.screenshots[-1][0].endswith("slug_filled.png")


def test_screenshot_timeout_is_best_effort_for_hidden_browser(monkeypatch, tmp_path):
    class HangingScreenshotPage:
        def __init__(self):
            self.calls = []

        def screenshot(self, **kwargs):
            self.calls.append(kwargs)
            raise ashby.PWTimeout("hidden compositor did not capture")

    page = HangingScreenshotPage()
    monkeypatch.setattr(ashby, "SHOTS", tmp_path)

    captured = ashby._shot(page, "ambrook", "filled")

    assert captured is False
    assert page.calls == [{
        "path": str(tmp_path / "ambrook_filled.png"),
        "full_page": True,
        "timeout": 1000,
    }]


def test_run_ashby_form_upload_failure_returns_without_closing_context(monkeypatch, tmp_path):
    _patch_form_dependencies(monkeypatch)
    page = FakeAshbyPage()
    page.upload_error = RuntimeError("missing file")

    result = ashby._run_ashby_form(page, "https://jobs.ashbyhq.com/acme/job", tmp_path / "resume.pdf", "slug", True)

    assert result["ok"] is False
    assert "resume upload failed" in result["reason"]
    assert page.screenshots[-1][0].endswith("slug_fail_upload.png")


def test_run_ashby_form_needs_answers_never_clicks(monkeypatch, tmp_path):
    attempted = []
    _patch_form_dependencies(monkeypatch, attempted=attempted)
    page = FakeAshbyPage(required_empty=["Sponsorship"])

    result = ashby._run_ashby_form(page, "https://jobs.ashbyhq.com/acme/job", tmp_path / "resume.pdf", "slug", False)

    assert result["ok"] is True
    assert result["unanswered"] == ["Sponsorship"]
    assert result["reason"].startswith("needs answers")
    assert attempted == []
    assert not [event for event in page.events if event[0] == "click"]


def test_run_ashby_form_spam_rejection_is_manual_definitive_after_click(monkeypatch, tmp_path):
    attempted = []
    _patch_form_dependencies(monkeypatch, attempted=attempted)
    page = FakeAshbyPage(body="We couldn't submit your application because it was flagged as possible spam.")

    result = ashby._run_ashby_form(page, "https://jobs.ashbyhq.com/acme/job", tmp_path / "resume.pdf", "slug", False)

    assert attempted == ["attempted"]
    assert result["outcome"] == "manual"
    assert result["retryable"] is False
    assert result["definitive_rejection"] is True
    assert result["submission_uncertain"] is False
    assert "possible spam" in result["reason"]


def test_run_ashby_form_confirmed_submission(monkeypatch, tmp_path):
    attempted = []
    _patch_form_dependencies(monkeypatch, attempted=attempted)
    page = FakeAshbyPage(body="Your application has been submitted successfully.")

    result = ashby._run_ashby_form(page, "https://jobs.ashbyhq.com/acme/job", tmp_path / "resume.pdf", "slug", False)

    assert attempted == ["attempted"]
    assert result["ok"] is True
    assert result["submitted"] is True
    assert result["reason"] == "confirmed"


def test_run_ashby_form_unconfirmed_click_is_uncertain_nonretryable(monkeypatch, tmp_path):
    attempted = []
    _patch_form_dependencies(monkeypatch, attempted=attempted)
    page = FakeAshbyPage(body="Still on the application page")

    result = ashby._run_ashby_form(page, "https://jobs.ashbyhq.com/acme/job", tmp_path / "resume.pdf", "slug", False)

    assert attempted == ["attempted"]
    assert result["outcome"] == "manual"
    assert result["retryable"] is False
    assert result["submission_uncertain"] is True


def test_run_ashby_form_click_timeout_is_uncertain(monkeypatch, tmp_path):
    attempted = []
    _patch_form_dependencies(monkeypatch, attempted=attempted)
    page = FakeAshbyPage()
    page.click_error = ashby.PWTimeout("slow")

    result = ashby._run_ashby_form(page, "https://jobs.ashbyhq.com/acme/job", tmp_path / "resume.pdf", "slug", False)

    assert attempted == ["attempted"]
    assert result["outcome"] == "manual"
    assert result["retryable"] is False
    assert result["submission_uncertain"] is True
    assert "timed out" in result["reason"]


if __name__ == "__main__":
    unittest.main()
