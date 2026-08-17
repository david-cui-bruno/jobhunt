from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "apply"))

import greenhouse


class EmbedFallbackTests(unittest.TestCase):
    def test_us_board_maps_to_embed(self):
        self.assertEqual(
            "https://job-boards.greenhouse.io/embed/job_app?for=workato&token=8492935002",
            greenhouse._embed_fallback_url(
                "https://job-boards.greenhouse.io/workato/jobs/8492935002"
            ),
        )

    def test_eu_board_keeps_regional_host(self):
        self.assertEqual(
            "https://job-boards.eu.greenhouse.io/embed/job_app?for=veeamsoftware&token=4857828101",
            greenhouse._embed_fallback_url(
                "https://job-boards.eu.greenhouse.io/veeamsoftware/jobs/4857828101"
            ),
        )

    def test_non_job_boards_hosts_are_ignored(self):
        self.assertIsNone(
            greenhouse._embed_fallback_url(
                "https://boards.greenhouse.io/workato/jobs/8492935002"
            )
        )

    def test_embed_urls_are_not_reprocessed(self):
        self.assertIsNone(
            greenhouse._embed_fallback_url(
                "https://job-boards.greenhouse.io/embed/job_app?for=x&token=1"
            )
        )


if __name__ == "__main__":
    unittest.main()
