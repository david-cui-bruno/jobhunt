from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import drip
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
        self.assertEqual(drip.DAILY_CAP, 50)


if __name__ == "__main__":
    unittest.main()
