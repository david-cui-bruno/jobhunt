import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apply"))

import lever  # noqa: E402


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
