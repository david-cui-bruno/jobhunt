from __future__ import annotations

import base64
import io
import inspect
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "apply"))

import workday


class WorkdayAnswerTests(unittest.TestCase):
    def test_activation_link_must_match_the_exact_workday_tenant(self) -> None:
        good = "https://nelnet.wd1.myworkdayjobs.com/MyNelnet/activate/secret-token"
        evil = "https://evil.example/activate/stolen"
        html_body = f'<a href="{evil}">bad</a><a href="{good}">verify</a>'
        encoded = base64.urlsafe_b64encode(html_body.encode()).decode().rstrip("=")
        message = {
            "payload": {
                "mimeType": "multipart/alternative",
                "parts": [{"mimeType": "text/html", "body": {"data": encoded}}],
            }
        }

        self.assertEqual(
            good,
            workday.workday_activation_url_from_message(
                message, "nelnet.wd1.myworkdayjobs.com"
            ),
        )
        self.assertIsNone(
            workday.workday_activation_url_from_message(
                message, "psu.wd1.myworkdayjobs.com"
            )
        )

    def test_new_workday_social_auth_chooser_is_supported(self) -> None:
        source = inspect.getsource(workday._open_email_auth)
        self.assertIn("utilityButtonSignIn", source)
        self.assertIn("SignInWithEmailButton", source)
        self.assertIn("createAccountLink", source)

    def test_localized_workday_click_filters_are_supported(self) -> None:
        self.assertIn("CreateAccount", workday.CREATE_ACCOUNT_OVERLAY)
        self.assertIn("SignIn", workday.SIGN_IN_OVERLAY)

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

    def test_saved_resume_refresh_deletes_only_the_exact_named_attachment(self) -> None:
        source = inspect.getsource(workday.refresh_saved_resume)
        self.assertIn('expected_label = f"Delete {resume_pdf.name}"', source)
        self.assertIn("existing.count() != 1", source)
        self.assertNotIn("delete-file'].first", source)

    def test_model_outage_returns_no_guesses(self) -> None:
        fields = [
            {
                "faid": "question-1",
                "value": "",
                "label": "Why are you interested?",
                "kind": "text",
                "options": [],
            }
        ]
        with mock.patch("urllib.request.urlopen", side_effect=RuntimeError("offline")):
            self.assertEqual(workday.wd_answers(fields, "Example", "Engineer"), [])

    def test_approved_workday_fact_does_not_require_the_model(self) -> None:
        fields = [
            {
                "faid": "referral",
                "value": "",
                "label": "Were you referred by a current employee?",
                "kind": "radio",
                "options": ["Yes", "No"],
            }
        ]
        approved = {"company_facts": {"LPL Financial": {"referral": False}}}
        with mock.patch.object(workday.qa, "APPLICATION_ANSWERS", approved), \
             mock.patch("urllib.request.urlopen") as urlopen:
            self.assertEqual(
                workday.wd_answers(fields, "lplfinancial", "Software Engineer Intern"),
                [{"faid": "referral", "answer": "No"}],
            )
        urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
