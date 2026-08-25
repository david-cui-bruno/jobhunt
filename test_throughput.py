from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import drip
import submit
from watcher import filter as filt
from watcher import startups, watch


class SourceCoverageTests(unittest.TestCase):
    def test_clean_url_removes_tracking_without_corrupting_query(self) -> None:
        self.assertEqual(
            watch._clean_url(
                "https://www.dreamworkhq.com/job/abc?"
                "utm_source=github&utm_campaign=internships"
            ),
            "https://www.dreamworkhq.com/job/abc",
        )
        self.assertEqual(
            watch._clean_url(
                "https://jobs.example.com/apply?job=123&utm_source=github&lang=en#form"
            ),
            "https://jobs.example.com/apply?job=123&lang=en#form",
        )

    def test_current_dreamwork_url_repairs_malformed_stale_legacy_row(self) -> None:
        clean_url = "https://www.dreamworkhq.com/job/11111111-1111-1111-1111-111111111111"
        malformed_url = clean_url + "&utm_campaign=gh-tech-internships"
        posting = watch.Posting(
            source="dreamwork-2027",
            company="Acme",
            title="Software Engineer Intern",
            locations="NYC",
            url=clean_url,
            posting_id="dreamwork-2027:acme:swe:new",
        )
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "tracker.db"
            conn = sqlite3.connect(db)
            watch.init_db(conn)
            conn.execute(
                "INSERT INTO postings "
                "(posting_id,source,company,title,locations,url,sponsorship,"
                "citizenship_required,closed,first_seen,status,outcome,last_error) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("legacy", "dreamwork-2027", "Acme", posting.title, "NYC", malformed_url,
                 "", 0, 0, 1, "filtered_out", "stale", "liveness check marked posting stale"),
            )
            conn.commit()

            self.assertEqual(watch.upsert(conn, [posting]), [])
            repaired = conn.execute(
                "SELECT url,status,outcome,last_error FROM postings WHERE posting_id='legacy'"
            ).fetchone()
            conn.close()

            with mock.patch.object(filt, "DB_PATH", db):
                result = filt.run(current_posting_ids={"legacy"})

            conn = sqlite3.connect(db)
            status = conn.execute(
                "SELECT status FROM postings WHERE posting_id='legacy'"
            ).fetchone()[0]
            conn.close()

        self.assertEqual(repaired, (clean_url, "filtered_out", None, None))
        self.assertEqual(result, {"queued": 1, "filtered_out": 0})
        self.assertEqual(status, "queued")

    def test_current_dreamwork_row_retires_existing_malformed_alias(self) -> None:
        clean_url = "https://www.dreamworkhq.com/job/22222222-2222-2222-2222-222222222222"
        posting = watch.Posting(
            source="dreamwork-2027",
            company="Acme",
            title="Software Engineer Intern",
            locations="NYC",
            url=clean_url,
            posting_id="current",
        )
        conn = sqlite3.connect(":memory:")
        watch.init_db(conn)
        conn.executemany(
            "INSERT INTO postings "
            "(posting_id,source,company,title,locations,url,sponsorship,"
            "citizenship_required,closed,first_seen,status,outcome,last_error) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                ("legacy", "dreamwork-2027", "Acme", posting.title, "NYC",
                 clean_url + "&utm_campaign=gh-tech-internships", "", 0, 0, 1,
                 "manual", "manual", "no adapter for other"),
                ("current", "dreamwork-2027", "Acme", posting.title, "NYC", clean_url,
                 "", 0, 0, 2, "manual", "manual", "no adapter for other"),
            ],
        )
        conn.commit()

        self.assertEqual(watch.upsert(conn, [posting]), [])
        rows = {
            row[0]: row[1:]
            for row in conn.execute(
                "SELECT posting_id,status,outcome,last_error FROM postings ORDER BY posting_id"
            )
        }
        conn.close()

        self.assertEqual(rows["current"][0], "manual")
        self.assertEqual(rows["legacy"], (
            "filtered_out", "deduplicated", "replaced malformed Dreamwork URL"
        ))

    def test_simplify_keeps_only_summer_and_winter_2027_terms(self) -> None:
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
            {"Company 0", "Company 3"},
        )

    def test_watcher_polls_ai_but_not_offseason_github_lists(self) -> None:
        names = {name for name, _ in watch.WATCH_SOURCES}
        self.assertIn("speedy-ai", names)
        self.assertNotIn("vansh-offseason", names)

    def test_daily_startup_discovery_has_no_offseason_github_sources(self) -> None:
        self.assertEqual(startups.OFFSEASON_SOURCES, ())

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

    def test_markdown_posting_identity_includes_the_application_url(self) -> None:
        markdown = "\n".join((
            "| Company | Position | Location | Posting | Age |",
            "|---|---|---|---|---|",
            "| Acme | Software Engineer Intern | NYC | "
            "<a href=\"https://jobs.example/acme/123\">Apply</a> | 1d |",
            "| Acme | Software Engineer Intern | SF | "
            "<a href=\"https://jobs.example/acme/456\">Apply</a> | 1d |",
        ))

        postings = watch._parse_md_table(markdown, "speedy")

        self.assertEqual(2, len(postings))
        self.assertNotEqual(postings[0].posting_id, postings[1].posting_id)

    def test_watch_run_reports_current_database_ids_for_reconciliation(self) -> None:
        posting = watch.Posting(
            source="vansh",
            company="Acme",
            title="Software Engineer Intern",
            locations="NYC",
            url="https://jobs.example/acme/123",
            posting_id="new-parser-id",
        )
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "tracker.db"
            conn = sqlite3.connect(db)
            watch.init_db(conn)
            conn.execute(
                "INSERT INTO postings "
                "(posting_id, source, company, title, locations, url, sponsorship, "
                "citizenship_required, closed, first_seen, status) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                ("legacy-id", "vansh", "Acme", posting.title, "NYC", posting.url, "", 0, 0, 1, "filtered_out"),
            )
            conn.commit()
            conn.close()

            with mock.patch.object(watch, "DB_PATH", db), mock.patch.object(
                watch, "WATCH_SOURCES", (("vansh", lambda: [posting]),)
            ):
                summary = watch.run()

        self.assertEqual(summary["current_posting_ids"], ["legacy-id"])

    def test_advanced_degree_only_titles_are_filtered(self) -> None:
        for title in (
            "Machine Learning Engineer Intern - PhD",
            "Generative AI Ph.D. Research Intern",
            "Doctoral Research Intern",
            "Software Engineer Intern - Master's Required",
            "Research Intern - Master’s Required",
        ):
            with self.subTest(title=title):
                self.assertFalse(filt.title_ok(title))

    def test_explicit_out_of_scope_season_and_coop_terms_are_filtered(self) -> None:
        for title in (
            "Social Media Engineering Intern (Fall...",
            "Fall Software Development Intern",
            "Site Reliability Internship - Spring ...",
            "Spring Software Engineer Intern",
            "Software Engineer Intern Summer 2026",
            "2026 Software Engineering Intern",
            "Applied Materials 2026 Summer Software Engineer Intern",
            "Mill Summer 2026 Software Engineer Intern",
            "Snap 2026 Software Engineer Intern",
            "Zettabyte Space Software Engineering Intern 2026",
            "Software Engineer Co-op",
            "Software Engineer Co Op",
            "Software Engineer Coop",
        ):
            with self.subTest(title=title):
                self.assertFalse(filt.title_ok(title))

    def test_summer_winter_and_unseasoned_roles_remain_in_scope(self) -> None:
        for title in (
            "Summer 2027 Software Engineer Intern",
            "Winter 2027 Software Engineer Intern",
            "Software Engineer Intern Summer 2027",
            "2027 Software Engineering Intern",
            "Product Manager Intern",
            "Backend Engineer",
        ):
            with self.subTest(title=title):
                self.assertTrue(filt.title_ok(title))

    def test_hardware_only_engineering_titles_are_filtered(self) -> None:
        for title in (
            "FPGA Engineering Intern",
            "Student Engineering Intern - Civil",
            "Transducer Engineering Intern",
            "Propulsion Test Engineering Intern",
            "AI Infrastructure DC Design Intern",
        ):
            with self.subTest(title=title):
                self.assertFalse(filt.title_ok(title))

        for title in (
            "Embedded Software Engineering Intern",
            "Firmware Engineering Intern",
            "Powertrain Controls Software Engineering Intern",
        ):
            with self.subTest(title=title):
                self.assertTrue(filt.title_ok(title))

    def test_manual_company_does_not_block_a_distinct_application(self) -> None:
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

        self.assertEqual(result, {"queued": 1, "filtered_out": 0})
        self.assertEqual(status, "queued")

    def test_active_tailoring_claim_does_not_block_a_distinct_internship(self) -> None:
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

        self.assertEqual(result, {"queued": 1, "filtered_out": 0})
        self.assertEqual(statuses, {"claimed": "tailoring", "intern": "queued"})

    def test_current_eligible_filtered_listing_is_rechecked_without_reviving_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "tracker.db"
            conn = sqlite3.connect(db)
            watch.init_db(conn)
            conn.executemany(
                "INSERT INTO postings "
                "(posting_id, source, company, title, locations, url, sponsorship, "
                "citizenship_required, closed, first_seen, status, outcome) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    ("current", "vansh", "Acme", "Software Engineer Intern", "NYC", "https://new", "", 0, 0, 1, "filtered_out", None),
                    ("stale", "vansh", "Beta", "Software Engineer Intern", "SF", "https://stale", "", 0, 0, 1, "filtered_out", "stale"),
                ],
            )
            conn.commit()
            conn.close()

            with mock.patch.object(filt, "DB_PATH", db):
                result = filt.run(current_posting_ids={"current", "stale"})

            conn = sqlite3.connect(db)
            statuses = dict(conn.execute("SELECT posting_id,status FROM postings"))
            conn.close()

        self.assertEqual(result, {"queued": 1, "filtered_out": 0})
        self.assertEqual(statuses, {"current": "queued", "stale": "filtered_out"})

    def test_current_title_mismatch_is_marked_once_per_filter_revision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "tracker.db"
            conn = sqlite3.connect(db)
            watch.init_db(conn)
            conn.execute(
                "INSERT INTO postings "
                "(posting_id, source, company, title, locations, url, sponsorship, "
                "citizenship_required, closed, first_seen, status) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                ("current", "vansh", "Acme", "Tax Intern", "NYC", "https://new", "", 0, 0, 1, "filtered_out"),
            )
            conn.commit()
            conn.close()

            with mock.patch.object(filt, "DB_PATH", db):
                first = filt.run(current_posting_ids={"current"})
                second = filt.run(current_posting_ids={"current"})

            conn = sqlite3.connect(db)
            row = conn.execute(
                "SELECT status,last_error FROM postings WHERE posting_id='current'"
            ).fetchone()
            conn.close()

        self.assertEqual(first, {"queued": 0, "filtered_out": 1})
        self.assertEqual(second, {"queued": 0, "filtered_out": 0})
        self.assertEqual(row, ("filtered_out", f"{filt.FILTER_REVISION}:title mismatch"))

    def test_same_canonical_posting_from_two_sources_is_queued_once(self) -> None:
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
                    ("one", "vansh", "Acme", "Software Engineer Intern", "NYC", "https://jobs.example/acme/123", "", 0, 0, 1, "new"),
                    ("two", "speedy", "Acme Inc", "SWE Intern", "SF", "https://jobs.example/acme/123", "", 0, 0, 2, "new"),
                ],
            )
            conn.commit()
            conn.close()

            with mock.patch.object(filt, "DB_PATH", db):
                result = filt.run()

            conn = sqlite3.connect(db)
            statuses = dict(conn.execute("SELECT posting_id,status FROM postings"))
            conn.close()

        self.assertEqual(result, {"queued": 1, "filtered_out": 1})
        self.assertEqual(set(statuses.values()), {"queued", "filtered_out"})

    def test_already_applied_canonical_posting_is_not_requeued_from_an_alias(self) -> None:
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
                    ("applied", "simplify", "Acme", "Software Engineer Intern", "NYC", "https://jobs.example/acme/123", "", 0, 0, 1, "submitted"),
                    ("alias", "vansh", "Acme Inc", "SWE Intern", "SF", "https://jobs.example/acme/123", "", 0, 0, 2, "new"),
                ],
            )
            conn.execute(
                "INSERT INTO applications (posting_id,submitted_at) VALUES ('applied',1)"
            )
            conn.commit()
            conn.close()

            with mock.patch.object(filt, "DB_PATH", db):
                result = filt.run()

            conn = sqlite3.connect(db)
            status = conn.execute(
                "SELECT status FROM postings WHERE posting_id='alias'"
            ).fetchone()[0]
            conn.close()

        self.assertEqual(result, {"queued": 0, "filtered_out": 1})
        self.assertEqual(status, "filtered_out")

    def test_stale_canonical_posting_is_not_requeued_from_an_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "tracker.db"
            conn = sqlite3.connect(db)
            watch.init_db(conn)
            conn.executemany(
                "INSERT INTO postings "
                "(posting_id, source, company, title, locations, url, sponsorship, "
                "citizenship_required, closed, first_seen, status, outcome) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    ("dead", "one", "Acme", "Software Engineer Intern", "NYC", "https://jobs.example/acme/123", "", 0, 0, 1, "filtered_out", "stale"),
                    ("alias", "two", "Acme", "Software Engineer Intern", "NYC", "https://jobs.example/acme/123", "", 0, 0, 2, "new", None),
                ],
            )
            conn.commit()
            conn.close()

            with mock.patch.object(filt, "DB_PATH", db):
                result = filt.run()

            conn = sqlite3.connect(db)
            alias = conn.execute(
                "SELECT status,last_error FROM postings WHERE posting_id='alias'"
            ).fetchone()
            conn.close()

        self.assertEqual(result, {"queued": 0, "filtered_out": 1})
        self.assertEqual(alias[0], "filtered_out")
        self.assertIn("canonical posting already tracked", alias[1])


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

    def test_pick_next_skips_unsupported_and_nonautomatic_ashby_lanes(self) -> None:
        conn = self._connection()
        conn.executemany(
            "INSERT INTO postings VALUES (?,?,?,?,?)",
            [
                ("unknown", "queued", "https://example.com/careers/1", "San Francisco", 30),
                ("ashby", "queued", "https://jobs.ashbyhq.com/acme/id", "San Francisco", 20),
                ("workable", "queued", "https://apply.workable.com/acme/j/ABC", "Ohio", 10),
            ],
        )

        self.assertEqual(drip.pick_next(conn)["posting_id"], "workable")
        conn.close()

    def test_pick_next_keeps_smartrecruiters_preparable_for_handoff(self) -> None:
        conn = self._connection()
        conn.execute(
            "INSERT INTO postings VALUES (?,?,?,?,?)",
            ("smart", "queued", "https://jobs.smartrecruiters.com/acme/1", "Remote", 10),
        )

        self.assertEqual(drip.pick_next(conn)["posting_id"], "smart")
        conn.close()

    def test_tailoring_batch_drains_all_currently_claimable_supported_rows(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE postings (posting_id TEXT, status TEXT, url TEXT, "
            "locations TEXT, first_seen INTEGER, last_attempt_at INTEGER)"
        )
        rows = [
            (f"p{i}", "queued", f"https://boards.greenhouse.io/acme/jobs/{i}", "Remote", i, None)
            for i in range(9)
        ]
        conn.executemany("INSERT INTO postings VALUES (?,?,?,?,?,?)", rows)
        processed = []

        def mark_ready(process_conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
            processed.append(row["posting_id"])
            return drip.transition_claim(
                process_conn, row["posting_id"], "tailoring", "ready"
            )

        self.assertEqual(9, drip.drain_tailoring_queue(conn, mark_ready))
        self.assertEqual([f"p{i}" for i in reversed(range(9))], processed)
        self.assertEqual(
            9,
            conn.execute("SELECT COUNT(*) FROM postings WHERE status='ready'").fetchone()[0],
        )
        conn.close()

    def test_tailoring_batch_drains_mixed_ready_and_manual_lanes_once(self) -> None:
        from submission.lanes import preparation_destination

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE postings (posting_id TEXT, status TEXT, url TEXT, "
            "locations TEXT, first_seen INTEGER, last_attempt_at INTEGER, "
            "outcome TEXT, last_error TEXT)"
        )
        rows = [
            (
                f"gh{i}", "queued", f"https://boards.greenhouse.io/acme/jobs/{i}",
                "Remote", i, None, None, None,
            )
            for i in range(5)
        ] + [
            (
                f"sr{i}", "queued", f"https://jobs.smartrecruiters.com/acme/{i}",
                "Remote", i + 5, None, None, None,
            )
            for i in range(4)
        ]
        conn.executemany("INSERT INTO postings VALUES (?,?,?,?,?,?,?,?)", rows)
        processed = []

        def mark_destination(process_conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
            processed.append(row["posting_id"])
            destination, reason = preparation_destination(process_conn, row["url"])
            if not drip.transition_claim(
                process_conn, row["posting_id"], "tailoring", destination, commit=False
            ):
                process_conn.rollback()
                return False
            if destination == "manual":
                process_conn.execute(
                    "UPDATE postings SET outcome='manual', last_error=? WHERE posting_id=?",
                    (reason, row["posting_id"]),
                )
            process_conn.commit()
            return True

        self.assertEqual(9, drip.drain_tailoring_queue(conn, mark_destination))
        self.assertEqual(9, len(processed))
        self.assertEqual(9, len(set(processed)))
        self.assertEqual(
            5,
            conn.execute(
                "SELECT COUNT(*) FROM postings WHERE posting_id LIKE 'gh%' AND status='ready'"
            ).fetchone()[0],
        )
        self.assertEqual(
            4,
            conn.execute(
                "SELECT COUNT(*) FROM postings WHERE posting_id LIKE 'sr%' "
                "AND status='manual' AND outcome='manual' "
                "AND last_error='prepared for manual completion: smartrecruiters'"
            ).fetchone()[0],
        )
        conn.close()

    def test_tailoring_batch_has_no_artificial_daily_or_per_run_cap(self) -> None:
        self.assertFalse(hasattr(drip, "DAILY_CAP"))
        self.assertFalse(hasattr(drip, "TAILOR_PER_RUN"))

    def test_tailoring_drain_excludes_released_none_results_during_same_run(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE postings (posting_id TEXT, status TEXT, url TEXT, "
            "locations TEXT, first_seen INTEGER, last_attempt_at INTEGER)"
        )
        conn.executemany(
            "INSERT INTO postings VALUES (?,?,?,?,?,?)",
            [
                ("first", "queued", "https://boards.greenhouse.io/acme/jobs/1", "Remote", 10, None),
                ("second", "queued", "https://boards.greenhouse.io/acme/jobs/2", "Remote", 9, None),
            ],
        )
        processed = []

        def maybe_none(process_conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
            processed.append(row["posting_id"])
            if row["posting_id"] == "first":
                return False
            return drip.transition_claim(
                process_conn, row["posting_id"], "tailoring", "ready"
            )

        self.assertEqual(1, drip.drain_tailoring_queue(conn, maybe_none))
        self.assertEqual(["first", "second"], processed)
        self.assertEqual(
            [("first", "queued"), ("second", "ready")],
            [tuple(row) for row in conn.execute(
                "SELECT posting_id,status FROM postings ORDER BY posting_id"
            ).fetchall()],
        )
        conn.close()

    def test_live_pipeline_is_not_artificially_capped_at_three_per_hour(self) -> None:
        self.assertEqual(submit.SUBMISSIONS_PER_RUN, 8)
        self.assertEqual((submit.PACING_MIN_SECONDS, submit.PACING_MAX_SECONDS), (15.0, 45.0))

        from submission.dispatcher import run_forever
        import inspect

        systemd = Path(__file__).parent / "deploy" / "systemd"
        launchd = Path(__file__).parent / "launchd" / "com.jobhunt.submit.plist"
        submit_service = (systemd / "jobhunt-submit.service").read_text()
        drip_timer = (systemd / "jobhunt@drip.timer").read_text()
        submit_plist = launchd.read_text()
        self.assertIn("ExecStart=/opt/jobhunt/.venv/bin/python /opt/jobhunt/submit_daemon.py", submit_service)
        self.assertIn("Restart=always", submit_service)
        self.assertIn("OnUnitActiveSec=20min", drip_timer)
        self.assertIn("<key>KeepAlive</key>", submit_plist)
        self.assertIn("submit_daemon.py", submit_plist)
        self.assertNotIn("StartInterval", submit_plist)
        self.assertEqual(inspect.signature(run_forever).parameters["poll_seconds"].default, 30.0)

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

class CompensationDripTests(unittest.TestCase):
    def test_drip_compensation_research_disabled_without_tavily_key(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE postings(status TEXT)")
        conn.execute("CREATE TABLE emails(sent_at INTEGER, posting_id TEXT, ats TEXT, confirmation TEXT)")
        with mock.patch.dict(os.environ, {"TAVILY_API_KEY": ""}, clear=False), \
             mock.patch.object(drip, "connect_tracker", return_value=conn), \
             mock.patch.object(drip, "recover_stale_claims", return_value=0), \
             mock.patch("watcher.watch.run", return_value={"new_count": 0, "current_posting_ids": []}), \
             mock.patch("watcher.filter.run", return_value={}), \
             mock.patch("watcher.abc_startups.run", return_value={}), \
             mock.patch("watcher.bigco.run", return_value={}), \
             mock.patch("email_apply.compose_ready_email_postings", return_value=0), \
             mock.patch("email_apply.poll_approvals", return_value=0), \
             mock.patch.object(drip, "quarantine_unsupported", return_value=0), \
             mock.patch.object(drip, "reconcile_nonautomatic_ready", return_value=0), \
             mock.patch.object(drip, "drain_tailoring_queue", return_value=0), \
             mock.patch.object(drip, "promote_legacy_tailored", return_value=(0, 0)), \
             mock.patch("compensation.research.TavilySearchProvider", side_effect=AssertionError("provider created without key")), \
             mock.patch("builtins.print") as printed:
            drip.run()
        messages = [str(call.args[0]) for call in printed.call_args_list if call.args]
        self.assertIn("[drip] compensation research: {'status': 'disabled_missing_key', 'limit': 5}", messages)

    def test_drip_compensation_research_uses_fixed_limit_when_key_exists(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE postings(status TEXT)")
        conn.execute("CREATE TABLE emails(sent_at INTEGER, posting_id TEXT, ats TEXT, confirmation TEXT)")
        provider = object()
        calls = []
        with mock.patch.dict(os.environ, {"TAVILY_API_KEY": "key"}, clear=False), \
             mock.patch.object(drip, "connect_tracker", return_value=conn), \
             mock.patch.object(drip, "recover_stale_claims", return_value=0), \
             mock.patch("watcher.watch.run", return_value={"new_count": 0, "current_posting_ids": []}), \
             mock.patch("watcher.filter.run", return_value={}), \
             mock.patch("watcher.abc_startups.run", return_value={}), \
             mock.patch("watcher.bigco.run", return_value={}), \
             mock.patch("email_apply.compose_ready_email_postings", return_value=0), \
             mock.patch("email_apply.poll_approvals", return_value=0), \
             mock.patch.object(drip, "quarantine_unsupported", return_value=0), \
             mock.patch.object(drip, "reconcile_nonautomatic_ready", return_value=0), \
             mock.patch.object(drip, "drain_tailoring_queue", return_value=0), \
             mock.patch.object(drip, "promote_legacy_tailored", return_value=(0, 0)), \
             mock.patch("compensation.research.TavilySearchProvider", return_value=provider), \
             mock.patch("compensation.research.prepare_pending_compensation", side_effect=lambda c, p, limit: calls.append((c, p, limit)) or {"examined": 6, "stored": 1, "manual": 2, "errors": 3, "limit": limit}), \
             mock.patch("builtins.print"):
            drip.run()
        self.assertEqual(calls, [(conn, provider, 5)])
