import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apply"))

import lever  # noqa: E402


def test_required_location_typeahead_needs_selected_visible_value():
    controls = [
        {
            "id": "location-input",
            "name": "location",
            "label": "Current location ✱",
            "tag": "input",
            "type": "text",
            "cls": "location typeahead",
            "required": True,
            "value": "Providence, RI",
            "chosen": "",
        },
        {
            "id": "work-auth",
            "name": "work_authorization",
            "label": "Work authorization ✱",
            "tag": "input",
            "type": "text",
            "cls": "",
            "required": True,
            "value": "Yes",
            "chosen": "",
        },
    ]

    assert lever._lever_required_empty_from_controls(controls) == ["Current location ✱"]


def test_required_location_typeahead_accepts_selected_visible_value():
    controls = [
        {
            "id": "location-input",
            "name": "location",
            "label": "Current location ✱",
            "tag": "input",
            "type": "text",
            "cls": "location typeahead",
            "required": True,
            "value": "Providence, RI",
            "chosen": "Providence, RI, USA",
        }
    ]

    assert lever._lever_required_empty_from_controls(controls) == []
