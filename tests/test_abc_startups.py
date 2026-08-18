import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT)]
from watcher import abc_startups as abc  # noqa: E402
from watcher.filter import title_ok  # noqa: E402


class SlugGuessTests(unittest.TestCase):
    def test_basic_name(self):
        self.assertEqual(abc.slug_guesses("Wispr")[0], "wispr")

    def test_multi_word(self):
        g = abc.slug_guesses("Harvey AI")
        self.assertIn("harveyai", g)
        self.assertIn("harvey-ai", g)
        self.assertIn("harvey", g)  # 'ai' suffix stripped

    def test_punctuation(self):
        g = abc.slug_guesses("Character.AI")
        self.assertEqual(g[0], "characterai")
        self.assertIn("character", g)

    def test_inc_suffix_stripped(self):
        self.assertIn("acme", abc.slug_guesses("Acme Inc"))

    def test_empty_and_junk(self):
        self.assertEqual(abc.slug_guesses(""), [])
        self.assertEqual(abc.slug_guesses("!!"), [])

    def test_bounded(self):
        self.assertLessEqual(len(abc.slug_guesses("Very Long Company Name Labs Inc")), 5)


class RoundFilterTests(unittest.TestCase):
    def test_abc_accepted(self):
        for r in ("A", "b", "Series C", "series a"):
            self.assertIsNotNone(abc._normalize_round(r), r)

    def test_seed_and_late_rejected(self):
        for r in ("Seed", "Pre-Seed", "D", "Series D", "Series E", "IPO",
                  "growth", "", None, "Series AA"):
            self.assertIsNone(abc._normalize_round(r), r)

    def test_extract_discards_bad_rounds(self):
        items = [{"title": "t", "summary": "s", "date": "2026-08-01", "via": "test"}]
        abc._claude, orig = (lambda *a, **k: (
            '[{"i":0,"company":"GoodCo","round":"B","sector":"ai","hq":"SF"},'
            '{"i":0,"company":"SeedCo","round":"Seed","sector":"ai","hq":"SF"},'
            '{"i":0,"company":"LateCo","round":"D","sector":"ai","hq":"SF"}]')), abc._claude
        try:
            rows = abc.extract_companies(items)
        finally:
            abc._claude = orig
        self.assertEqual([r["company"] for r in rows], ["GoodCo"])
        self.assertEqual(rows[0]["round"], "B")
        self.assertEqual(rows[0]["announced_at"], "2026-08-01")


class PostingShapeTests(unittest.TestCase):
    def test_greenhouse_posting_id(self):
        jobs = [{"id": 12345, "title": "Software Engineer, Backend",
                 "location": {"name": "New York, NY"},
                 "absolute_url": "https://x.example/j/12345"}]
        p = abc._postings_from_jobs("greenhouse", "GoodCo", jobs)
        self.assertEqual(len(p), 1)
        self.assertEqual(p[0].posting_id, "abc:greenhouse:12345")
        self.assertEqual(p[0].source, "abc")
        self.assertEqual(p[0].company, "GoodCo")

    def test_ashby_posting_id_and_remote(self):
        jobs = [{"id": "aaa-bbb", "title": "ML Engineer", "location": "Remote",
                 "isRemote": True, "jobUrl": "https://jobs.ashbyhq.com/x/aaa-bbb"}]
        p = abc._postings_from_jobs("ashby", "GoodCo", jobs)
        self.assertEqual(p[0].posting_id, "abc:ashby:aaa-bbb")

    def test_lever_unlisted_and_nonus_skipped(self):
        jobs = [
            {"id": "1", "text": "Software Engineer",
             "categories": {"location": "London, UK"}, "hostedUrl": "https://x/1"},
            {"id": "2", "text": "Software Engineer Intern",
             "categories": {"location": "San Francisco, CA"}, "hostedUrl": "https://x/2"},
            {"id": "3", "text": "Account Executive",
             "categories": {"location": "San Francisco, CA"}, "hostedUrl": "https://x/3"},
        ]
        p = abc._postings_from_jobs("lever", "GoodCo", jobs)
        self.assertEqual([x.posting_id for x in p], ["abc:lever:2"])

    def test_senior_titles_skipped(self):
        jobs = [{"id": 7, "title": "Senior Staff Software Engineer",
                 "location": {"name": "NYC"}, "absolute_url": "https://x/7"}]
        self.assertEqual(abc._postings_from_jobs("greenhouse", "G", jobs), [])


class LocationGateTests(unittest.TestCase):
    def test_us_cities_ok(self):
        for loc in ("San Francisco, CA", "New York", "Remote (US)", "Boston, MA", ""):
            self.assertTrue(abc._us_or_remote(loc), loc)

    def test_foreign_rejected(self):
        for loc in ("London, UK", "Remote (Canada)", "Bangalore, India", "Berlin"):
            self.assertFalse(abc._us_or_remote(loc), loc)


class FilterIntegrationTests(unittest.TestCase):
    def test_abc_allows_fulltime(self):
        self.assertTrue(title_ok("Software Engineer", source="abc"))
        self.assertTrue(title_ok("Founding Engineer", source="abc"))
        self.assertTrue(title_ok("Software Engineer, New Grad", source="abc"))

    def test_other_sources_still_reject_new_grad(self):
        self.assertFalse(title_ok("Software Engineer, New Grad", source="simplify"))

    def test_abc_still_rejects_hard_excludes(self):
        self.assertFalse(title_ok("Mechanical Engineer", source="abc"))
        self.assertFalse(title_ok("Software Engineer Co-op", source="abc"))


if __name__ == "__main__":
    unittest.main()
