from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "apply"))

import jd
import workable


class DetectTests(unittest.TestCase):
    def test_workable_host_detected(self):
        self.assertEqual(
            "workable",
            jd.detect_ats("https://apply.workable.com/luminance-1/j/E045EF5A7A/"),
        )

    def test_worker_maps_workable(self):
        sys.path.insert(0, str(ROOT))
        import submit_worker

        fn, waas, detected, target = submit_worker._adapter(
            "", "https://apply.workable.com/luminance-1/j/E045EF5A7A/apply"
        )
        self.assertEqual("workable", detected)
        self.assertIsNotNone(fn)


class UrlTests(unittest.TestCase):
    def test_apply_url_from_posting_url(self):
        self.assertEqual(
            "https://apply.workable.com/acme/j/ABC123/apply/",
            workable._apply_url("https://apply.workable.com/acme/j/ABC123/"),
        )

    def test_apply_url_idempotent(self):
        self.assertEqual(
            "https://apply.workable.com/acme/j/ABC123/apply/",
            workable._apply_url("https://apply.workable.com/acme/j/ABC123/apply"),
        )

    def test_non_workable_url_passthrough(self):
        url = "https://example.com/jobs/1"
        self.assertEqual(url, workable._apply_url(url))


class StateTests(unittest.TestCase):
    def test_removed_short_circuits_before_browser(self):
        with mock.patch.object(workable, "_posting_state", return_value="removed"):
            result = workable.apply_workable(
                "https://apply.workable.com/acme/j/ABC123/", Path("/nonexistent.pdf"),
                "t", dry_run=True,
            )
        self.assertEqual("stale", result["outcome"])
        self.assertFalse(result["submitted"])

    def test_posting_state_unparsable_url(self):
        self.assertIsNone(workable._posting_state("https://example.com/x"))


if __name__ == "__main__":
    unittest.main()
