from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "apply"))

import workday


class WorkdayAnswerTests(unittest.TestCase):
    def test_prompt_includes_grounding_without_missing_stories_placeholder(self) -> None:
        response = io.StringIO(json.dumps({"content": [{"type": "text", "text": "[]"}]}))
        fields = [
            {
                "faid": "question-1",
                "value": "",
                "label": "Why are you interested?",
                "kind": "text",
                "options": [],
            }
        ]

        with mock.patch("urllib.request.urlopen", return_value=response):
            self.assertEqual(workday.wd_answers(fields, "Example", "Engineer"), [])

    def test_saved_draft_answers_are_checked_against_approved_facts(self) -> None:
        approved = {
            "education": {
                "expected_graduation_month": "June",
                "expected_graduation_year": "2028",
                "exact_graduation_date": None,
            },
            "company_facts": {
                "LPL Financial": {"referral": False},
            },
        }
        fields = [
            {
                "faid": "referral-right",
                "label": "Were you referred by a current employee?",
                "value": "No",
            },
            {
                "faid": "referral-wrong",
                "label": "Were you referred by a current employee?",
                "value": "Yes",
            },
            {
                "faid": "grad-day",
                "label": "Graduation date",
                "kind": "date",
                "hasDay": True,
                "value": "05/15/2028",
            },
        ]

        self.assertEqual(
            ["Were you referred by a current employee?", "Graduation date"],
            workday.unsafe_prefilled_fields(
                fields,
                "lplfinancial",
                approved_answers=approved,
            ),
        )

    def test_existing_workday_wizard_can_resume_without_initial_upload_zone(self) -> None:
        page = mock.Mock()
        page.locator.return_value.count.return_value = 1
        with mock.patch.object(workday, "current_step", return_value="My Information"):
            self.assertTrue(workday.saved_draft_wizard_is_active(page))
        with mock.patch.object(workday, "current_step", return_value=""):
            self.assertFalse(workday.saved_draft_wizard_is_active(page))


if __name__ == "__main__":
    unittest.main()
