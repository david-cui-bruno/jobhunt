import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apply"))
import qa  # noqa: E402
import smartrecruiters  # noqa: E402


class QaManualPolicyTest(unittest.TestCase):
    APPROVED = {
        "version": 1,
        "identity": {
            "date_of_birth": "06/01/2006",
            "pronouns": "He/Him",
            "disability": {"current": False, "history": False},
        },
        "preferences": {
            "hybrid": True,
            "compensation_policy": "Use an employer-published range; otherwise open / market rate.",
        },
        "education": {
            "expected_graduation_month": "June",
            "expected_graduation_year": "2028",
            "exact_graduation_date": None,
        },
        "current_offers": [
            {"company": "Soren", "deadline_month": "September 2026", "deadline": None}
        ],
        "company_facts": {
            "Sentry": {"used_product": True},
            "LPL Financial": {
                "prior_employment": False,
                "referral": False,
                "prior_interview_or_application": False,
                "licenses_or_exams": False,
            },
            "Deloitte": {"household_employment": False},
        },
    }

    def test_smartrecruiters_intro_does_not_hardcode_a_grad_year(self):
        message = smartrecruiters._hiring_team_message(qa.PROFILE)
        self.assertNotIn("'27", message)
        self.assertNotIn("May 2027", message)
        self.assertNotIn("May 2028", message)
        self.assertIn("Brown University", message)

    def test_filter_blocks_explicit_user_fact_preferences(self):
        controls = [
            {"id": "dob", "name": "", "label": "Date of birth", "value": ""},
            {"id": "comp", "name": "", "label": "Desired salary", "value": ""},
            {"id": "offer", "name": "", "label": "Do you have an offer deadline?", "value": ""},
            {"id": "prod", "name": "", "label": "Have you used our product before?", "value": ""},
            {"id": "ref", "name": "", "label": "Were you referred by a current employee?", "value": ""},
            {"id": "nc", "name": "", "label": "Are you bound by a non-compete?", "value": ""},
            {"id": "clear", "name": "", "label": "What security clearance do you hold?", "value": ""},
            {"id": "sched", "name": "", "label": "List your exact work schedule and travel percentage", "value": ""},
            {"id": "dis", "name": "", "label": "Disability history", "value": ""},
            {"id": "pronouns", "name": "", "label": "Preferred pronouns", "value": ""},
            {"id": "used", "name": "", "label": "Have you ever used Sentry before?", "value": ""},
            {"id": "hybrid", "name": "", "label": "Are you willing to join us in office 3 days a week?", "value": ""},
            {"id": "grad", "name": "", "label": "Anticipated graduation date", "kind": "date", "hasDay": True, "value": ""},
            {"id": "interview", "name": "", "label": "Have you previously interviewed at LPL?", "value": ""},
            {"id": "finra", "name": "", "label": "Do you hold any FINRA licenses?", "value": ""},
            {"id": "household", "name": "", "label": "Was a member of your household employed by Deloitte?", "value": ""},
            {"id": "media", "name": "", "label": "What are you reading right now?", "value": ""},
            {"id": "allowed", "name": "", "label": "Why are you interested in this internship?", "value": ""},
        ]
        answers = [{"id_or_name": c["id"], "answer": "model answer"} for c in controls]

        allowed, blocked = qa.filter_manual_answers(controls, answers, profile_text="")

        self.assertEqual(["allowed"], [a["id_or_name"] for a in allowed])
        self.assertEqual({c["id"] for c in controls if c["id"] != "allowed"}, {a["id_or_name"] for a in blocked})

    def test_disability_allowed_when_profile_has_explicit_text(self):
        control = {"id": "dis", "label": "Voluntary disability status", "value": ""}
        self.assertTrue(qa.answer_requires_manual(control, "No", profile_text=""))
        self.assertFalse(qa.answer_requires_manual(control, "No", profile_text="disability: no"))

    def test_user_approved_answers_are_allowed_but_missing_deadlines_stay_manual(self):
        controls = [
            {"id": "dob", "label": "Date of birth", "value": ""},
            {"id": "pronouns", "label": "Preferred pronouns", "value": ""},
            {"id": "product", "label": "Have you ever used Sentry before?", "value": ""},
            {"id": "prior", "label": "Have you worked for LPL Financial as an employee?", "value": ""},
            {"id": "hybrid", "label": "Can you work in-office 3 days a week?", "value": ""},
            {"id": "offer", "label": "Do you have an outstanding offer?", "value": ""},
            {"id": "deadline", "label": "What is your offer deadline?", "value": ""},
            {"id": "comp", "label": "Desired compensation", "value": "", "options": []},
        ]
        answers = [
            {"id_or_name": "dob", "answer": "06/01/2006"},
            {"id_or_name": "pronouns", "answer": "He/Him"},
            {"id_or_name": "product", "answer": "Yes"},
            {"id_or_name": "prior", "answer": "No"},
            {"id_or_name": "hybrid", "answer": "Yes"},
            {"id_or_name": "offer", "answer": "Yes, Soren"},
            {"id_or_name": "deadline", "answer": "No deadline"},
            {"id_or_name": "comp", "answer": "$50/hour"},
        ]
        allowed, blocked = qa.filter_manual_answers(
            controls, answers, approved_answers=self.APPROVED
        )
        self.assertEqual(
            {"dob", "pronouns", "product", "prior", "hybrid", "offer"},
            {answer["id_or_name"] for answer in allowed},
        )
        self.assertEqual(
            {"deadline", "comp"},
            {answer["id_or_name"] for answer in blocked},
        )

    def test_month_only_offer_deadline_is_allowed_but_day_is_not_invented(self):
        controls = [
            {"id": "month", "label": "What is your offer deadline?", "value": ""},
            {
                "id": "exact",
                "label": "What is your offer deadline?",
                "kind": "date",
                "hasDay": True,
                "value": "",
            },
        ]
        answers = [
            {"id_or_name": "month", "answer": "September 2026"},
            {"id_or_name": "exact", "answer": "09/15/2026"},
        ]

        allowed, blocked = qa.filter_manual_answers(
            controls, answers, approved_answers=self.APPROVED
        )

        self.assertEqual(["month"], [answer["id_or_name"] for answer in allowed])
        self.assertEqual(["exact"], [answer["id_or_name"] for answer in blocked])

    def test_approved_boolean_must_match_and_company_context_is_supported(self):
        controls = [
            {
                "id": "referral-no",
                "label": "Were you referred by a current employee?",
                "company_context": "lplfinancial",
                "value": "",
            },
            {
                "id": "referral-wrong",
                "label": "Were you referred by a current employee?",
                "company_context": "lplfinancial",
                "value": "",
            },
            {
                "id": "household",
                "label": "Was a member of your household employed by Deloitte?",
                "company_context": "lplfinancial",
                "value": "",
            },
        ]
        answers = [
            {"id_or_name": "referral-no", "answer": "No"},
            {"id_or_name": "referral-wrong", "answer": "Yes"},
            {"id_or_name": "household", "answer": "No"},
        ]

        allowed, blocked = qa.filter_manual_answers(
            controls, answers, approved_answers=self.APPROVED
        )

        self.assertEqual(
            {"referral-no", "household"},
            {answer["id_or_name"] for answer in allowed},
        )
        self.assertEqual(["referral-wrong"], [answer["id_or_name"] for answer in blocked])

    def test_graduation_month_year_must_match_and_exact_day_stays_manual(self):
        controls = [
            {"id": "right", "label": "Graduation date", "value": ""},
            {"id": "wrong", "label": "Graduation date", "value": ""},
            {"id": "day", "label": "Graduation date", "hasDay": True, "value": ""},
        ]
        answers = [
            {"id_or_name": "right", "answer": "June 2028"},
            {"id_or_name": "wrong", "answer": "May 2027"},
            {"id_or_name": "day", "answer": "06/01/2028"},
        ]

        allowed, blocked = qa.filter_manual_answers(
            controls, answers, approved_answers=self.APPROVED
        )

        self.assertEqual(["right"], [answer["id_or_name"] for answer in allowed])
        self.assertEqual(
            {"wrong", "day"},
            {answer["id_or_name"] for answer in blocked},
        )

    def test_explicit_facts_render_without_a_model_and_unknown_days_stay_empty(self):
        controls = [
            {
                "id": "referral",
                "label": "Were you referred by a current employee?",
                "options": ["Yes", "No"],
                "value": "",
            },
            {
                "id": "grad",
                "label": "Graduation date",
                "kind": "date",
                "hasDay": False,
                "value": "",
            },
            {
                "id": "grad-day",
                "label": "Graduation date",
                "kind": "date",
                "hasDay": True,
                "value": "",
            },
            {
                "id": "deadline",
                "label": "Offer deadline",
                "value": "",
            },
        ]

        rendered = qa.explicit_approved_answers(
            controls,
            company_context="lplfinancial",
            approved_answers=self.APPROVED,
        )

        self.assertEqual(
            {
                "referral": "No",
                "grad": "06/2028",
                "deadline": "September 2026",
            },
            {answer["id_or_name"]: answer["answer"] for answer in rendered},
        )

    def test_only_relevant_sensitive_answers_enter_the_model_prompt(self):
        context = qa.relevant_application_answers(
            [{"label": "Preferred pronouns"}], approved=self.APPROVED
        )
        self.assertEqual("He/Him", context["identity"]["pronouns"])
        self.assertNotIn("date_of_birth", context["identity"])
        self.assertNotIn("current_offers", context)

    def test_get_answers_logs_allowed_and_blocked_with_context_without_api(self):
        controls = [
            {"id": "q1", "name": "", "label": "Why us?", "value": ""},
            {"id": "q2", "name": "", "label": "Desired compensation", "value": ""},
        ]
        model_answers = [
            {"id_or_name": "q1", "answer": "Because the mission fits."},
            {"id_or_name": "q2", "answer": "$50/hour"},
        ]

        class FakeResponse:
            def __enter__(self):
                return self
            def __exit__(self, exc_type, exc, tb):
                return False

        def fake_json_load(_resp):
            return {"content": [{"type": "text", "text": json.dumps(model_answers)}]}

        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(qa, "ROOT", Path(tmp)), \
             mock.patch.object(qa.urllib.request, "urlopen", return_value=FakeResponse()), \
             mock.patch.object(qa.json, "load", side_effect=fake_json_load), \
             mock.patch.object(qa, "_grounding", return_value=""):
            (Path(tmp) / "out").mkdir()
            answers = qa.get_answers(controls, context={"slug": "acme", "url": "https://example.test/job"})
            rows = [(json.loads(line)) for line in (Path(tmp) / "out" / "qa_answers.log").read_text().splitlines()]

        self.assertEqual([model_answers[0]], answers)
        self.assertEqual(["allowed", "blocked_manual"], [r["decision"] for r in rows])
        self.assertEqual(["Why us?", "Desired compensation"], [r["question"] for r in rows])
        self.assertEqual(["acme", "acme"], [r["slug"] for r in rows])
        self.assertEqual(["https://example.test/job", "https://example.test/job"], [r["url"] for r in rows])


if __name__ == "__main__":
    unittest.main()
