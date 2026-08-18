import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT)]
from watcher import bigco  # noqa: E402


class BigcoResolutionTests(unittest.TestCase):
    def test_ats_url_shapes_are_stable_public_json(self):
        self.assertEqual(
            bigco.feed_url(bigco.CompanyFeed("Ramp", "greenhouse", "ramp")),
            "https://boards-api.greenhouse.io/v1/boards/ramp/jobs",
        )
        self.assertEqual(
            bigco.feed_url(bigco.CompanyFeed("Netflix", "lever", "netflix")),
            "https://api.lever.co/v0/postings/netflix?mode=json",
        )
        self.assertEqual(
            bigco.feed_url(bigco.CompanyFeed("Anthropic", "ashby", "anthropic")),
            "https://api.ashbyhq.com/posting-api/job-board/anthropic",
        )

    def test_bigco_posting_filter_accepts_intern_newgrad_swe_ml_only(self):
        self.assertTrue(bigco.title_ok("Software Engineer Intern, Summer 2027"))
        self.assertTrue(bigco.title_ok("Machine Learning Engineer, University Grad 2027"))
        self.assertFalse(bigco.title_ok("Senior Software Engineer"))
        self.assertFalse(bigco.title_ok("Product Manager Intern"))

    def test_greenhouse_jobs_become_bigco_postings(self):
        jobs = [{"id": 42, "title": "Software Engineer Intern, Summer 2027", "location": {"name": "New York, NY"}, "absolute_url": "https://example/jobs/42"}]
        postings = bigco.postings_from_payload(bigco.CompanyFeed("Stripe", "greenhouse", "stripe"), json.dumps({"jobs": jobs}))
        self.assertEqual(len(postings), 1)
        self.assertEqual(postings[0].source, "bigco")
        self.assertEqual(postings[0].posting_id, "bigco:greenhouse:stripe:42")

    def test_google_payload_parser_handles_public_search_results(self):
        payload = json.dumps({"jobs": [{"id": "g1", "title": "Software Engineering Intern, Summer 2027", "locations": [{"display": "Mountain View, CA"}], "apply_url": "https://careers.google.com/jobs/g1"}]})
        postings = bigco.postings_from_payload(bigco.CompanyFeed("Google", "google", ""), payload)
        self.assertEqual([p.posting_id for p in postings], ["bigco:google:google:g1"])


if __name__ == "__main__":
    unittest.main()
