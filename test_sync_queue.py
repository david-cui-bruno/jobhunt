import sqlite3
import tempfile
import unittest
from pathlib import Path

from sync_queue import read_active_postings


class SyncQueueTests(unittest.TestCase):
    def test_reads_only_active_rows_from_read_only_tracker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tracker.db"
            with sqlite3.connect(path) as conn:
                conn.execute(
                    "CREATE TABLE postings (posting_id TEXT, source TEXT, company TEXT, title TEXT, locations TEXT, url TEXT, status TEXT, first_seen INTEGER)"
                )
                conn.executemany(
                    "INSERT INTO postings VALUES (?,?,?,?,?,?,?,?)",
                    [
                        ("ready", "source", "Acme", "Role", "Remote", "https://acme.test/role", "ready", 2),
                        ("tailoring", "source", "Tailor", "Role", "Remote", "https://tailor.test/role", "tailoring", 4),
                        ("sprinting", "source", "Sprint", "Role", "Remote", "https://sprint.test/role", "sprinting", 5),
                        ("submitting", "source", "Submit", "Role", "Remote", "https://submit.test/role", "submitting", 6),
                        ("filtered", "source", "Old", "Role", "Remote", "https://old.test/role", "filtered_out", 3),
                    ],
                )
            rows = read_active_postings(path)
            self.assertEqual(
                {row["posting_id"]: row["status"] for row in rows},
                {
                    "ready": "ready",
                    "tailoring": "queued",
                    "sprinting": "queued",
                    "submitting": "ready",
                },
            )


if __name__ == "__main__":
    unittest.main()
