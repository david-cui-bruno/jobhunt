"""Submitter: takes 'ready' postings and submits via the matching ATS adapter.

Pacing: max 3 submissions/hour, business hours ET, human-ish jitter between.
Unknown ATS or adapter failure -> posting marked 'manual', summarized in nightly email.
"""
from __future__ import annotations

import datetime
import random
import sqlite3
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT / "apply"), str(ROOT / "notify")]

DB = ROOT / "out" / "tracker.db"
ET = ZoneInfo("America/New_York")
HOURLY_CAP = 3


def submit_ready(limit: int = HOURLY_CAP, dry_run: bool = False) -> list[dict]:
    from jd import detect_ats
    from greenhouse import apply_greenhouse
    from lever import apply_lever
    from ashby import apply_ashby
    from workday import apply_workday
    import mailer

    now = datetime.datetime.now(ET)
    if not (9 <= now.hour < 21):
        return []

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT p.*, e.resume_pdf FROM postings p JOIN emails e USING(posting_id) "
        "WHERE p.status='ready'").fetchall()
    results = []
    done = 0
    for r in rows:
        if done >= limit:
            break
        ats = detect_ats(r["url"])
        fn = {"greenhouse": apply_greenhouse, "lever": apply_lever, "ashby": apply_ashby,
              "workday": apply_workday}.get(ats)
        slug = f"{r['company'].replace(' ', '_')[:40]}_{int(time.time())}"
        if fn is None:
            conn.execute("UPDATE postings SET status='manual' WHERE posting_id=?",
                         (r["posting_id"],))
            conn.commit()
            results.append({"company": r["company"], "ats": ats, "status": "manual (no adapter)"})
            continue
        pdf = Path(r["resume_pdf"])
        if not pdf.is_absolute():
            pdf = ROOT / pdf
        try:
            res = fn(r["url"], pdf, slug, dry_run=dry_run)
        except Exception as e:
            res = {"ok": False, "submitted": False, "reason": f"adapter crash: {e}"}
        if res.get("submitted"):
            conn.execute("UPDATE postings SET status='submitted' WHERE posting_id=?",
                         (r["posting_id"],))
            conn.execute(
                "INSERT OR REPLACE INTO applications VALUES (?,?,?,?,?,?)",
                (r["posting_id"], str(pdf), ats, int(time.time()), res.get("reason", ""), ""))
            done += 1
        elif res.get("ok") and res.get("unanswered"):
            conn.execute("UPDATE postings SET status='manual' WHERE posting_id=?",
                         (r["posting_id"],))
            mailer.send(f"[jobhunt] manual input needed: {r['company']}",
                        f"{r['company']} — {r['title']}\n{r['url']}\n\n"
                        f"Auto-fill couldn't answer: {res['unanswered']}\n"
                        "Reply with answers and I'll retry, or apply manually.")
        else:
            conn.execute("UPDATE postings SET status='failed' WHERE posting_id=?",
                         (r["posting_id"],))
        conn.commit()
        results.append({"company": r["company"], "ats": ats,
                        "status": "submitted" if res.get("submitted") else res.get("reason", "?")})
        time.sleep(random.uniform(60, 240))  # human-ish gap
    conn.close()
    return results


if __name__ == "__main__":
    dry = "--dry" in sys.argv
    for r in submit_ready(dry_run=dry):
        print(r)
