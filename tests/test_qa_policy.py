import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apply"))
import qa  # noqa: E402


class QaManualPolicyTest(unittest.TestCase):
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
