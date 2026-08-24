import sys
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apply"))

import lever  # noqa: E402
from submission_state import confirmation_observed  # noqa: E402


class FakeLeverLocationPage:
    def __init__(self, *, has_location=True, selected_location="", generic_required=None):
        self.has_location = has_location
        self.selected_location = selected_location
        self.generic_required = list(generic_required or [])
        self.scripts = []

    def evaluate(self, script):
        self.scripts.append(script)
        if "selected-location" in script and "selectedLocation" in script:
            if not self.has_location:
                return []
            if self.selected_location.strip():
                return []
            return ["Current location ✱"]
        return list(self.generic_required)


def test_required_lever_location_needs_hidden_selected_location_dom_value():
    page = FakeLeverLocationPage(selected_location="")

    assert lever._lever_location_required_empty(page) == ["Current location ✱"]


def test_required_lever_location_accepts_nonempty_hidden_selected_location_dom_value():
    page = FakeLeverLocationPage(selected_location='{"location":"Providence, RI, USA"}')

    assert lever._lever_location_required_empty(page) == []


def test_lever_location_validator_ignores_unrelated_required_controls():
    page = FakeLeverLocationPage(has_location=False, generic_required=["Work authorization ✱", "Consent ✱"])

    assert lever._lever_location_required_empty(page) == []


def test_lever_required_empty_preserves_generic_required_validation_and_adds_location_only():
    page = FakeLeverLocationPage(selected_location="", generic_required=["Work authorization ✱"])

    assert lever._lever_required_empty(page) == ["Work authorization ✱", "Current location ✱"]


def test_location_only_blocker_gets_explicit_hcaptcha_manual_reason():
    assert lever._lever_required_reason(["Current location ✱"]) == (
        "needs manual Lever location selection: location typeahead is hCaptcha-gated"
    )


def test_mixed_blockers_keep_generic_needs_answers_reason():
    assert lever._lever_required_reason(["Current location ✱", "Work authorization ✱"]) == (
        "needs answers: ['Current location ✱', 'Work authorization ✱']"
    )


def test_lever_thanks_path_is_confirmation():
    assert confirmation_observed("", "https://jobs.lever.co/acme/id/thanks")
    assert not confirmation_observed("", "https://example.com/thanksgiving")
    assert not confirmation_observed("", "https://jobs.lever.co/acme/id?next=/thanks")


class FakeLocator:
    def __init__(self, page, selector, count=0, visible=False, visibilities=None):
        self.page = page
        self.selector = selector
        self._count = count
        self._visibilities = list(visibilities) if visibilities is not None else [visible] * count
        self._visible = visible if visibilities is None else any(self._visibilities)
        self.first = self

    def count(self):
        return self._count

    def is_visible(self):
        return self._visible

    def nth(self, index):
        return FakeLocator(
            self.page,
            self.selector,
            count=1,
            visible=self._visibilities[index],
        )

    def set_input_files(self, _path):
        self.page.resume_uploaded = True

    def fill(self, _value):
        pass

    def click(self, timeout=None):
        if "Submit application" in self.selector or "btn-submit" in self.selector:
            self.page.submit_clicks += 1


class FakeLeverCaptchaPage:
    url = "https://jobs.lever.co/acme/id/apply"

    def __init__(self):
        self.submit_clicks = 0
        self.resume_uploaded = False
        self.visible_iframes = []
        self.hidden_iframes = []

    def add_visible_iframe(self, src):
        self.visible_iframes.append(src)

    def add_hidden_iframe(self, src):
        self.hidden_iframes.append(src)

    def goto(self, *_args, **_kwargs):
        pass

    def set_default_timeout(self, _timeout):
        pass

    def set_default_navigation_timeout(self, _timeout):
        pass

    def wait_for_timeout(self, _timeout):
        pass

    def locator(self, selector):
        if selector == "input[type=file]":
            return FakeLocator(self, selector, count=1, visible=True)
        if "hcaptcha.com" in selector:
            visibilities = [False] * len(self.hidden_iframes) + [True] * len(self.visible_iframes)
            return FakeLocator(
                self,
                selector,
                count=len(visibilities),
                visibilities=visibilities,
            )
        if "h-captcha" in selector or "h-captcha-response" in selector:
            return FakeLocator(self, selector, count=0)
        if "btn-submit" in selector or "Submit application" in selector:
            return FakeLocator(self, selector, count=1, visible=True)
        return FakeLocator(self, selector, count=0)

    def evaluate(self, script):
        if "querySelectorAll('.application-question.required" in script:
            return []
        return []

    def screenshot(self, **_kwargs):
        pass

    def inner_text(self, _selector):
        return ""


class FakeContext:
    def __init__(self, page):
        self.page = page

    def new_page(self):
        return self.page


class FakeBrowser:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


@contextmanager
def fake_playwright():
    yield object()


def test_lever_captcha_present_ignores_hidden_passive_hcaptcha_markup():
    fake_page = FakeLeverCaptchaPage()
    fake_page.add_hidden_iframe("https://newassets.hcaptcha.com/captcha/v1/passive")

    assert not lever.lever_captcha_present(fake_page)


def test_lever_captcha_present_detects_visible_hcaptcha_challenge():
    fake_page = FakeLeverCaptchaPage()
    fake_page.add_visible_iframe("https://newassets.hcaptcha.com/captcha/v1/abc")

    assert lever.lever_captcha_present(fake_page)


def test_lever_hcaptcha_stops_before_submit():
    fake_page = FakeLeverCaptchaPage()
    fake_page.add_visible_iframe("https://newassets.hcaptcha.com/captcha/v1/abc")
    fake_browser = FakeBrowser()

    with mock.patch.object(lever, "sync_playwright", fake_playwright), mock.patch.object(
        lever.stealth, "launch_stealth_context", return_value=(fake_browser, FakeContext(fake_page))
    ), mock.patch.object(lever.qa, "EXTRACT_JS", ""), mock.patch.object(
        lever.qa, "harvest_select_options"
    ), mock.patch.object(lever.qa, "get_answers", return_value=[]), mock.patch.object(
        lever.qa, "fill_answers", return_value=([], [])
    ), mock.patch.object(lever, "mark_submit_attempted") as mark_submit_attempted:
        result = lever.apply_lever("https://jobs.lever.co/acme/id", Path("resume.pdf"), "lever-captcha", dry_run=False)

    assert result["outcome"] == "manual"
    assert result["click_attempted"] is False
    assert result["submission_uncertain"] is False
    assert fake_page.submit_clicks == 0
    mark_submit_attempted.assert_not_called()
