import datetime
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import digest  # noqa: E402
import digest_replies  # noqa: E402


def _collected(action=(), manual_ask=(), verify=(), notes=(), debt=None,
               stats=None):
    return {"action": list(action), "manual_ask": list(manual_ask),
            "verify": list(verify), "notes": list(notes),
            "manual_debt": debt or {},
            "stats": stats or {"submitted_24h": 4, "ready": 2, "queued": 31}}


class ComposeShortTest(unittest.TestCase):
    def test_empty_is_none(self):
        self.assertIsNone(digest.compose_short(_collected()))
        # stats alone aren't worth a text, same as compose()
        self.assertIsNone(digest.compose_short(_collected(debt={"no adapter for": 3})))

    def test_full_content_stats_and_cap(self):
        deadline = (datetime.datetime.now(digest.ET).date()
                    + datetime.timedelta(days=2)).isoformat()
        d = _collected(
            action=[("oa_invite", "Stripe", "SWE Intern", deadline, "http://u", "do the OA"),
                    ("offer", "Ramp", "SWE Intern", "", "http://u", "offer!!")],
            manual_ask=[("Cybernetic Labs", "Full-Stack Intern", "http://u",
                         "needs answers: ['When can you start?']"),
                        ("Medtronic", "SWE Intern", "http://u", "sign-in rejected")],
            verify=[("Datadog", "SWE Intern", "http://u")],
            debt={"no adapter for": 5},
        )
        body = digest.compose_short(d)
        self.assertLessEqual(len(body), digest.SHORT_LIMIT)
        self.assertIn("[OA] Stripe", body)
        self.assertIn("due in 2d", body)
        self.assertIn("[OFFER] Ramp", body)
        self.assertIn("Cybernetic Labs", body)
        # Telegram is the PRIMARY channel (2026-08-19): full content, not a
        # 3-item teaser. Everything makes the message now.
        self.assertIn("Medtronic", body)
        self.assertIn("Datadog", body)
        self.assertIn("4 submitted today", body)
        self.assertIn("5 stuck on my side", body)
        # action items carry their URL (act-from-phone); stuck items don't
        self.assertIn("http://u", body)

    def test_cap_holds_under_pathological_input(self):
        d = _collected(manual_ask=[("C" * 300, "T" * 300, "u", "E" * 400)] * 6)
        self.assertLessEqual(len(digest.compose_short(d)), digest.SHORT_LIMIT)

    def test_digest_includes_compact_submission_health(self):
        d = _collected(
            manual_ask=[("Stripe", "SWE", "u", "needs answers: x")],
            stats={"submitted_24h": 4, "ready": 2, "queued": 31},
        )
        d["attempt_metrics"] = [
            {"ats": "greenhouse", "attempts": 2, "confirmed": 1, "confirmation_rate": 0.5,
             "failed": 0, "manual": 1, "p50_duration_ms": 2000, "p95_duration_ms": 3000},
            {"ats": "workday", "attempts": 3, "confirmed": 0, "confirmation_rate": 0.0,
             "failed": 3, "manual": 0, "p50_duration_ms": 5000, "p95_duration_ms": 9000},
        ]
        d["queue_metrics"] = [
            {"lane": "direct", "depth": 5, "automatic": True},
            {"lane": "workday", "depth": 1, "automatic": True},
            {"lane": "ashby", "depth": 2, "automatic": False},
        ]
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE postings (posting_id TEXT PRIMARY KEY, company TEXT, title TEXT, status TEXT, url TEXT, first_seen INTEGER)")
        conn.execute("INSERT INTO postings VALUES ('a1','Ashby Co','SWE','ready','https://jobs.ashbyhq.com/acme/1',0)")
        from submission.ashby_policy import ensure_lane_state
        ensure_lane_state(conn)
        conn.execute("UPDATE ats_lane_state SET enabled=1,tier=1,consecutive_confirmed=3,next_attempt_at=1800000000 WHERE ats='ashby'")
        d["ashby_breaker"] = digest._ashby_breaker_state(conn, now=1700000000)

        body = digest.compose_short(d)

        self.assertLessEqual(len(body), digest.SHORT_LIMIT)
        self.assertIn("workday 0/3 confirmed", body)
        self.assertIn("greenhouse 1/2 confirmed", body)
        self.assertIn("queues: direct 5, workday 1, ashby 2", body)
        self.assertIn("ashby enabled: tier 1 / 90m", body)
        self.assertIn("ready 1", body)

    def test_phone_copy_fails_soft(self):
        d = _collected(manual_ask=[("Stripe", "SWE", "u", "needs answers: x")])
        with mock.patch("notify.kith_bridge.send_phone", side_effect=RuntimeError("bridge down")):
            digest._send_phone_copy(d)  # must not raise

    def test_phone_copy_sends_short_body(self):
        d = _collected(manual_ask=[("Stripe", "SWE", "u", "needs answers: x")])
        with mock.patch("notify.kith_bridge.send_phone") as send:
            digest._send_phone_copy(d)
        send.assert_called_once()
        self.assertIn("Stripe", send.call_args[0][0])


class IMessageRepliesTest(unittest.TestCase):
    """process_imessage: same parsing rules as email replies, kith mocked."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.db = root / "tracker.db"
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE postings (posting_id TEXT PRIMARY KEY, company TEXT, "
                     "title TEXT, status TEXT, last_error TEXT)")
        conn.executemany("INSERT INTO postings VALUES (?,?,?,?,?)", [
            ("p1", "SwingVision", "iOS Intern", "manual", "needs answers: x"),
            ("p2", "Stellar", "SWE Intern", "manual", "needs answers: start date"),
            ("p3", "Medtronic", "SWE Intern", "manual", "sign-in rejected"),
        ])
        conn.commit()
        conn.close()
        self.patches = [
            mock.patch.object(digest_replies, "DB", self.db),
            mock.patch.object(digest_replies, "STATE", root / "state.json"),
            mock.patch.object(digest_replies, "OVERRIDES", root / "overrides.json"),
            mock.patch.object(digest_replies, "NOTES", root / "notes.log"),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(self.tmp.cleanup)
        for p in self.patches:
            self.addCleanup(p.stop)

    def _run(self, texts, cursor="2026-01-01T00:00:00+00:00"):
        (Path(self.tmp.name) / "state.json").write_text(
            json.dumps({"processed": [], "imsg_cursor": cursor}))
        rows = [{"id": f"m{i}", "created_at": f"2026-01-02T00:00:0{i}+00:00",
                 "content": [{"type": "text",
                              "text": f"<context>stamp · channel: phone</context>\n\n{t}"}]}
                for i, t in enumerate(texts)]
        def rest(env, method, path, body=None):
            return [{"id": "sess1"}] if "chat_session" in path else rows
        with mock.patch("notify.kith_token._kith_env", return_value={}), \
                mock.patch("notify.kith_token._rest", side_effect=rest):
            return digest_replies.process_imessage()

    def _status(self, pid):
        conn = sqlite3.connect(self.db)
        v = conn.execute("SELECT status FROM postings WHERE posting_id=?", (pid,)).fetchone()[0]
        conn.close()
        return v

    def test_skip_company(self):
        acted = self._run(["skip swingvision"])
        self.assertEqual(acted["skipped"], ["SwingVision"])
        self.assertEqual(self._status("p1"), "skipped")
        self.assertEqual(self._status("p2"), "manual")

    def test_skip_all(self):
        acted = self._run(["ok jobhunt skip all"])
        self.assertEqual(len(acted["skipped"]), 3)
        self.assertEqual(self._status("p3"), "skipped")

    def test_company_answer_requeues(self):
        acted = self._run(["for stellar: start date is june 15"])
        self.assertEqual(acted["requeued"], ["Stellar"])
        self.assertEqual(self._status("p2"), "ready")
        ov = json.loads((Path(self.tmp.name) / "overrides.json").read_text())
        self.assertIn("start date is june 15", ov["Stellar"][0]["note"])

    def test_freeform_jobhunt_text_noted(self):
        acted = self._run(["jobhunt looks slow today, push harder"])
        self.assertEqual(acted["noted"], 1)
        note = (Path(self.tmp.name) / "notes.log").read_text()
        self.assertIn("texted the digest", note)
        self.assertIn("push harder", note)

    def test_unrelated_kith_chatter_ignored(self):
        acted = self._run(["hey", "remind me to call mom", "[pulse] morning"])
        self.assertEqual(acted, {"skipped": [], "requeued": [], "noted": 0})
        self.assertFalse((Path(self.tmp.name) / "notes.log").exists())

    def test_idempotent_and_cursor_advances(self):
        self._run(["skip swingvision"])
        state = json.loads((Path(self.tmp.name) / "state.json").read_text())
        self.assertEqual(state["imsg_cursor"], "2026-01-02T00:00:00+00:00")
        self.assertIn("m0", state["imsg_processed"])

    def test_first_run_sets_cursor_without_replay(self):
        (Path(self.tmp.name) / "state.json").write_text(json.dumps({"processed": []}))
        with mock.patch("notify.kith_token._kith_env", return_value={}), \
                mock.patch("notify.kith_token._rest") as rest:
            acted = digest_replies.process_imessage()
        self.assertEqual(acted, {"skipped": [], "requeued": [], "noted": 0})
        rest.assert_not_called()
        state = json.loads((Path(self.tmp.name) / "state.json").read_text())
        self.assertTrue(state["imsg_cursor"])

    def test_kith_down_raises_for_wrapper_to_log(self):
        (Path(self.tmp.name) / "state.json").write_text(
            json.dumps({"processed": [], "imsg_cursor": "2026-01-01T00:00:00+00:00"}))
        with mock.patch("notify.kith_token._kith_env",
                        side_effect=OSError("supabase unreachable")):
            with self.assertRaises(OSError):
                digest_replies.process_imessage()


if __name__ == "__main__":
    unittest.main()
