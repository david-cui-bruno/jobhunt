"""Track-based graduation law (David 2026-08-19) and filter word-boundary tests."""
from __future__ import annotations

import importlib
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT)]

import track  # noqa: E402
from watcher.filter import title_ok  # noqa: E402


class TrackInferenceTests(unittest.TestCase):
    def test_intern_titles(self):
        for t in ("Software Engineer Intern", "Software Engineering Internship",
                  "ML Intern (Summer 2027)", "Software Co-op", "Software Co op",
                  "Product Management Intern"):
            self.assertEqual(track.infer_track(t), "intern", t)

    def test_fulltime_titles(self):
        for t in ("Software Engineer", "Founding Engineer", "Software Engineer, New Grad",
                  "Member of Technical Staff", "Product Manager", "Forward Deployed Engineer"):
            self.assertEqual(track.infer_track(t), "fulltime", t)

    def test_internal_is_not_intern(self):
        # 'internal' must not match the intern regex
        self.assertEqual(track.infer_track("Internal Tools Engineer"), "fulltime")

    def test_grad_dates_by_track(self):
        self.assertEqual(track.grad_month_year("intern"), "May 2028")
        self.assertEqual(track.grad_month_year("fulltime"), "May 2027")
        self.assertEqual(track.grad_exact_date("intern"), "05/15/2028")
        self.assertEqual(track.grad_exact_date("fulltime"), "05/15/2027")

    def test_env_resolution_order(self):
        old_title = os.environ.pop(track.ENV_TITLE, None)
        old_track = os.environ.pop(track.ENV_TRACK, None)
        try:
            self.assertEqual(track.current_track(), "intern")  # safe default
            os.environ[track.ENV_TITLE] = "Backend Engineer"
            self.assertEqual(track.current_track(), "fulltime")
            os.environ[track.ENV_TRACK] = "intern"
            self.assertEqual(track.current_track(), "intern")  # explicit track wins
            self.assertEqual(track.current_track("SWE Intern"), "intern")  # arg wins over all
        finally:
            for k, v in ((track.ENV_TITLE, old_title), (track.ENV_TRACK, old_track)):
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


class QaOverlayTests(unittest.TestCase):
    """qa.py must resolve graduation facts per-track at import time."""

    def _fresh_qa(self, title: str):
        os.environ[track.ENV_TITLE] = title
        os.environ.pop(track.ENV_TRACK, None)
        try:
            sys.path.insert(0, str(ROOT / "apply"))
            import qa  # noqa: F401
            return importlib.reload(qa)
        finally:
            os.environ.pop(track.ENV_TITLE, None)

    def test_fulltime_overlay(self):
        qa = self._fresh_qa("Founding Engineer")
        edu = qa.PROFILE["education"]
        self.assertEqual(qa.APPLICATION_TRACK, "fulltime")
        self.assertEqual((edu["grad_month"], edu["grad_year"]), ("May", "2027"))
        if isinstance(qa.APPLICATION_ANSWERS.get("education"), dict):
            aedu = qa.APPLICATION_ANSWERS["education"]
            self.assertEqual(aedu["expected_graduation_year"], "2027")
            self.assertEqual(aedu["exact_graduation_date"], "05/15/2027")

    def test_intern_overlay(self):
        qa = self._fresh_qa("Software Engineer Intern")
        edu = qa.PROFILE["education"]
        self.assertEqual(qa.APPLICATION_TRACK, "intern")
        self.assertEqual((edu["grad_month"], edu["grad_year"]), ("May", "2028"))

    @classmethod
    def tearDownClass(cls):
        # restore intern-default module state for any later importers
        os.environ.pop(track.ENV_TITLE, None)
        os.environ.pop(track.ENV_TRACK, None)
        sys.path.insert(0, str(ROOT / "apply"))
        import qa
        importlib.reload(qa)


class FilterWordBoundaryTests(unittest.TestCase):
    def test_ai_ml_do_not_match_inside_words(self):
        # the 2026-08-19 bug: 'ai' matched 'maintenance', 'ml' matched 'html'
        for t in ("Maintenance Technician", "Email Marketing Specialist",
                  "HTML Developer Advocate", "Painter", "Trainer"):
            self.assertFalse(title_ok(t), t)

    def test_expanded_roles_match(self):
        for t in ("Member of Technical Staff", "Forward Deployed Engineer",
                  "Solutions Engineer", "Product Manager", "APM Program 2027",
                  "Embedded Software Engineer", "Firmware Engineer",
                  "Site Reliability Engineer", "iOS Engineer",
                  "Data Scientist", "Quant Developer", "DevOps Engineer",
                  "Security Engineer", "Product Management Intern"):
            self.assertTrue(title_ok(t), t)

    def test_hardware_and_junk_still_excluded(self):
        for t in ("Hardware Engineer", "Electrical Engineer Intern",
                  "Mechanical Engineer", "Product Marketing Manager",
                  "PhD Research Intern", "Actuarial Intern"):
            self.assertFalse(title_ok(t), t)

    def test_seasons_still_excluded(self):
        for t in ("Software Engineer Intern (Fall 2027)", "Software Co-op",
                  "SWE Intern - Spring 2027"):
            self.assertFalse(title_ok(t), t)

    def test_core_roles_still_match(self):
        for t in ("Software Engineer Intern", "Machine Learning Engineer",
                  "AI Engineer", "Full-Stack Engineer", "Backend Engineer"):
            self.assertTrue(title_ok(t), t)


if __name__ == "__main__":
    unittest.main()


class MarkdownTitleTests(unittest.TestCase):
    def test_url_text_never_matches(self):
        # dreamwork titles are '[X](url)'; 'ai' lived in 'utm_campaign' etc.
        self.assertFalse(title_ok("[Security Intern](https://x.dev/job/1?utm_source=github&utm_campaign=gh-ai-internships)"))
        self.assertFalse(title_ok("[Bridge Engineering Intern](https://ai.example/a)"))

    def test_markdown_inner_title_still_matches(self):
        self.assertTrue(title_ok("[Software Engineering Intern - Summer 2027](https://x.dev/j/2)"))
        self.assertTrue(title_ok("[AI Engineer Intern](https://x.dev/j/3)"))

    def test_nonsoftware_engineering_intern_variants_excluded(self):
        for t in ("GTM Engineering Intern", "Project Engineering Intern",
                  "Bridge Engineering Intern - Summer 2027",
                  "Process Engineering Intern", "Quality Engineer Intern"):
            self.assertFalse(title_ok(t), t)
