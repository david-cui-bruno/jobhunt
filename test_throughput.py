from __future__ import annotations

import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import drip
import submit
from watcher import filter as filt
from watcher import watch


class SourceCoverageTests(unittest.TestCase):
    def test_simplify_keeps_every_approved_recruiting_term(self) -> None:
        rows = []
        for index, term in enumerate(("Summer 2027", "Fall 2026", "Spring 2027", "Winter 2027")):
            rows.append({
                "id": str(index),
                "is_visible": True,
                "active": True,
                "terms": [term],
                "company_name": f"Company {index}",
                "title": "Software Engineer Intern",
                "locations": ["USA"],
                "url": f"https://example.com/{index}",
            })
        with mock.patch.object(watch, "_fetch", return_value=json.dumps(rows)):
            postings = watch.fetch_simplify()

        self.assertEqual(
            {posting.company for posting in postings},
            {"Company 0", "Company 1", "Company 2"},
        )

    def test_watcher_polls_ai_and_offseason_github_lists(self) -> None:
        names = {name for name, _ in watch.WATCH_SOURCES}
        self.assertIn("speedy-ai", names)
        self.assertIn("vansh-offseason", names)

    def test_markdown_source_uses_posting_link_not_company_homepage(self) -> None:
        markdown = "\n".join((
            "| Company | Position | Location | Posting | Age |",
            "|---|---|---|---|---|",
            "| <a href=\"https://acme.example\">Acme</a> | SWE Intern | NYC | "
            "<a href=\"https://jobs.example/acme/123\">Apply</a> | 1d |",
        ))

        postings = watch._parse_md_table(markdown, "speedy")

        self.assertEqual(1, len(postings))
        self.assertEqual("https://jobs.example/acme/123", postings[0].url)

    def test_advanced_degree_only_titles_are_filtered(self) -> None:
        for title in (
            "Machine Learning Engineer Intern - PhD",
            "Doctoral Research Intern",
            "Software Engineer Intern - Master's Required",
            "Research Intern - Master’s Required",
        ):
            with self.subTest(title=title):
                self.assertFalse(filt.title_ok(title))

    def test_manual_company_blocks_a_second_application(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "tracker.db"
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            watch.init_db(conn)
            conn.executemany(
                "INSERT INTO postings "
                "(posting_id, source, company, title, locations, url, sponsorship, "
                "citizenship_required, closed, first_seen, status) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [
                    ("old", "one", "Acme", "Software Engineer Intern", "NYC", "https://old", "", 0, 0, 1, "manual"),
                    ("new", "two", "Acme", "Machine Learning Intern", "SF", "https://new", "", 0, 0, 2, "new"),
                ],
            )
            conn.commit()
            conn.close()
            with mock.patch.object(filt, "DB_PATH", db):
                result = filt.run()
            conn = sqlite3.connect(db)
            status = conn.execute(
                "SELECT status FROM postings WHERE posting_id='new'"
            ).fetchone()[0]
            conn.close()

        self.assertEqual(result, {"queued": 0, "filtered_out": 1})
        self.assertEqual(status, "filtered_out")

    def test_active_tailoring_claim_cannot_be_replaced_by_new_intern(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "tracker.db"
            conn = sqlite3.connect(db)
            watch.init_db(conn)
            conn.executemany(
                "INSERT INTO postings "
                "(posting_id, source, company, title, locations, url, sponsorship, "
                "citizenship_required, closed, first_seen, status) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [
                    ("claimed", "one", "Acme", "Software Engineer", "NYC", "https://old", "", 0, 0, 1, "tailoring"),
                    ("intern", "two", "Acme", "Software Engineer Intern", "SF", "https://new", "", 0, 0, 2, "new"),
                ],
            )
            conn.commit()
            conn.close()
            with mock.patch.object(filt, "DB_PATH", db):
                result = filt.run()
            conn = sqlite3.connect(db)
            statuses = dict(conn.execute("SELECT posting_id,status FROM postings"))
            conn.close()

        self.assertEqual(result, {"queued": 0, "filtered_out": 1})
        self.assertEqual(statuses, {"claimed": "tailoring", "intern": "filtered_out"})


class BacklogPriorityTests(unittest.TestCase):
    def _connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE postings (posting_id TEXT, status TEXT, url TEXT, "
            "locations TEXT, first_seen INTEGER)"
        )
        return conn

    def test_supported_ats_beats_better_location_on_unsupported_form(self) -> None:
        conn = self._connection()
        conn.executemany(
            "INSERT INTO postings VALUES (?,?,?,?,?)",
            [
                ("unsupported", "queued", "https://example.com/careers/1", "San Francisco", 20),
                ("supported", "queued", "https://boards.greenhouse.io/acme/jobs/2", "Ohio", 10),
            ],
        )

        self.assertEqual(drip.pick_next(conn)["posting_id"], "supported")
        conn.close()

    def test_tailoring_batch_is_bounded_but_material(self) -> None:
        self.assertEqual(drip.TAILOR_PER_RUN, 5)
        self.assertEqual(drip.DAILY_CAP, 100)

    def test_live_pipeline_is_not_artificially_capped_at_three_per_hour(self) -> None:
        self.assertEqual(submit.SUBMISSIONS_PER_RUN, 8)
        self.assertEqual((submit.PACING_MIN_SECONDS, submit.PACING_MAX_SECONDS), (15.0, 45.0))

        systemd = Path(__file__).parent / "deploy" / "systemd"
        submit_timer = (systemd / "jobhunt@submit.timer").read_text()
        drip_timer = (systemd / "jobhunt@drip.timer").read_text()
        self.assertIn("OnUnitActiveSec=30min", submit_timer)
        self.assertIn("OnUnitActiveSec=20min", drip_timer)

    def test_only_one_worker_can_claim_a_queued_posting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "tracker.db"
            first = sqlite3.connect(db)
            first.execute(
                "CREATE TABLE postings (posting_id TEXT PRIMARY KEY, status TEXT, "
                "last_attempt_at INTEGER, outcome TEXT, last_error TEXT)"
            )
            first.execute("INSERT INTO postings VALUES ('p1','queued',NULL,NULL,NULL)")
            first.commit()
            second = sqlite3.connect(db)

            self.assertTrue(drip.claim_posting(first, "p1", "sprinting"))
            self.assertFalse(drip.claim_posting(second, "p1", "tailoring"))
            self.assertEqual(
                "sprinting",
                first.execute("SELECT status FROM postings WHERE posting_id='p1'").fetchone()[0],
            )
            first.close()
            second.close()

    def test_stale_claims_recover_but_fresh_claims_remain_reserved(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE postings (posting_id TEXT PRIMARY KEY, status TEXT, "
            "last_attempt_at INTEGER, outcome TEXT, last_error TEXT)"
        )
        conn.executemany(
            "INSERT INTO postings VALUES (?,?,?,?,?)",
            [
                ("stale", "sprinting", 1, None, None),
                ("fresh", "tailoring", int(time.time()), None, None),
            ],
        )

        self.assertEqual(1, drip.recover_stale_claims(conn))
        self.assertEqual(
            [("fresh", "tailoring"), ("stale", "queued")],
            conn.execute("SELECT posting_id,status FROM postings ORDER BY posting_id").fetchall(),
        )
        conn.close()

    def test_interrupted_submission_is_quarantined_for_manual_verification(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE postings (posting_id TEXT PRIMARY KEY, status TEXT, "
            "last_attempt_at INTEGER, outcome TEXT, last_error TEXT)"
        )
        conn.execute("INSERT INTO postings VALUES ('p1','submitting',1,NULL,NULL)")

        self.assertEqual(1, drip.recover_stale_claims(conn))
        self.assertEqual(
            ("manual", "manual", "submission interrupted; verify possible prior submission"),
            conn.execute(
                "SELECT status,outcome,last_error FROM postings WHERE posting_id='p1'"
            ).fetchone(),
        )
        conn.close()

    def test_late_worker_cannot_clobber_a_terminal_status(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE postings (posting_id TEXT PRIMARY KEY, status TEXT, "
            "last_attempt_at INTEGER)"
        )
        conn.execute("INSERT INTO postings VALUES ('p1','queued',NULL)")
        self.assertTrue(drip.claim_posting(conn, "p1", "tailoring"))
        conn.execute("UPDATE postings SET status='submitted' WHERE posting_id='p1'")
        conn.commit()

        self.assertFalse(
            drip.transition_claim(conn, "p1", "tailoring", "ready")
        )
        self.assertEqual(
            "submitted",
            conn.execute("SELECT status FROM postings WHERE posting_id='p1'").fetchone()[0],
        )
        conn.close()

    def test_release_claim_rejects_unapproved_target_status(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE postings (posting_id TEXT PRIMARY KEY, status TEXT, "
            "last_attempt_at INTEGER)"
        )
        conn.execute("INSERT INTO postings VALUES ('p1','tailoring',NULL)")
        with self.assertRaises(ValueError):
            drip.release_claim(conn, "p1", "tailoring", "submitted")
        conn.close()


if __name__ == "__main__":
    unittest.main()
