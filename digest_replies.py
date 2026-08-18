"""Digest reply handler: David replies to the daily digest email; act on it.

Understands casual replies against the postings mentioned in the last digest:
  "skip swingvision"                          -> mark skipped
  "skip all"                                  -> skip every stuck posting listed
  "for stellar: start date is june, answer X" -> stores a company-scoped
     answer override (out/reply_answers.json) and requeues the posting so the
     next submit pass retries with it.
  anything else                               -> logged to out/digest-notes.log
     for the daily agent review (the agent reads it and acts).

Called from inbox.py's timer (every 30 min). Idempotent via processed-id set.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path

from notify import mailer

ROOT = Path(__file__).resolve().parent
DB = ROOT / "out" / "tracker.db"
STATE = ROOT / "out" / "digest_reply_state.json"
OVERRIDES = ROOT / "out" / "reply_answers.json"
NOTES = ROOT / "out" / "digest-notes.log"


def _state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except json.JSONDecodeError:
            pass
    return {"processed": []}


def _save(s: dict) -> None:
    s["processed"] = s["processed"][-500:]
    STATE.write_text(json.dumps(s))


def _digest_threads() -> list[dict]:
    q = "subject:\"jobhunt daily\" newer_than:7d"
    data = mailer._call("/messages?q=" + q.replace(" ", "+").replace('"', "%22") + "&maxResults=10")
    return data.get("messages", []) or []


def _stuck_postings(conn) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT posting_id, company, title FROM postings WHERE status='manual'").fetchall()


def _match_company(text: str, postings) -> list:
    t = text.lower()
    return [p for p in postings if p["company"].lower().split()[0] in t or p["company"].lower() in t]


def process() -> dict:
    s = _state()
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    acted = {"skipped": [], "requeued": [], "noted": 0}
    postings = _stuck_postings(conn)

    for m in _digest_threads():
        thread = mailer._call(f"/messages/{m['id']}?format=metadata")
        tid = thread.get("threadId")
        if not tid:
            continue
        full = mailer.get_thread(tid)
        for msg in full.get("messages", []):
            if msg["id"] in s["processed"]:
                continue
            hdrs = mailer.headers_of(msg)
            frm = hdrs.get("from", "")
            # only David's own replies; the digest itself is also from him, so
            # skip messages whose subject line starts with our own prefix AND
            # have no quoted-reply marker
            body = (mailer.extract_plain(msg) or "").strip()
            s["processed"].append(msg["id"])
            if "jobhunt bot" in body[:400] and "-- " not in body[:10]:
                # the digest we sent; replies quote it below their text
                first_line = body.split("\n", 1)[0].lower()
                if first_line.startswith("hey — quick jobhunt rundown"):
                    continue
            if "davidcui824@gmail.com" not in frm.lower():
                continue
            # strip quoted text
            reply = re.split(r"\nOn .{10,80} wrote:|\n>", body)[0].strip()
            if not reply:
                continue
            low = reply.lower()
            if re.search(r"\bskip all\b", low):
                for p in postings:
                    conn.execute("UPDATE postings SET status='skipped' WHERE posting_id=? AND status='manual'",
                                 (p["posting_id"],))
                    acted["skipped"].append(p["company"])
                conn.commit()
                continue
            skip_m = re.findall(r"\bskip ([a-z0-9&.\- ]{2,40})", low)
            for target in skip_m:
                for p in _match_company(target, postings):
                    conn.execute("UPDATE postings SET status='skipped' WHERE posting_id=? AND status='manual'",
                                 (p["posting_id"],))
                    acted["skipped"].append(p["company"])
            conn.commit()
            # per-company answers: "for <company>: <text>" or "<company>: <text>"
            for m2 in re.finditer(r"(?:for )?([a-z0-9&.\- ]{2,40})\s*:\s*([^\n]{3,300})", low):
                cands = _match_company(m2.group(1), postings)
                if not cands:
                    continue
                ov = {}
                if OVERRIDES.exists():
                    try:
                        ov = json.loads(OVERRIDES.read_text())
                    except json.JSONDecodeError:
                        ov = {}
                for p in cands:
                    ov.setdefault(p["company"], []).append({"ts": int(time.time()), "note": m2.group(2)})
                    conn.execute(
                        "UPDATE postings SET status='ready', last_error='retry with David reply: ' || ? "
                        "WHERE posting_id=? AND status='manual'", (m2.group(2)[:120], p["posting_id"]))
                    acted["requeued"].append(p["company"])
                OVERRIDES.write_text(json.dumps(ov, indent=1))
                conn.commit()
            if not skip_m and "skip all" not in low and not re.search(r":", reply):
                # freeform note -> agent review picks it up
                with NOTES.open("a") as f:
                    f.write(json.dumps({"ts": int(time.time()), "note": f"David replied to digest: {reply[:200]}"}) + "\n")
                acted["noted"] += 1
    _save(s)
    conn.close()
    return acted


if __name__ == "__main__":
    print(process())
