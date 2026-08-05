"""Weekly funnel stats email (Sundays 6pm, called from drip)."""
from __future__ import annotations

import datetime
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "notify"))

DB = ROOT / "out" / "tracker.db"


def weekly_stats(force: bool = False) -> bool:
    import mailer
    now = datetime.datetime.now()
    conn = sqlite3.connect(DB)
    conn.execute("CREATE TABLE IF NOT EXISTS scan_state (k TEXT PRIMARY KEY, v TEXT)")
    if not force:
        if now.weekday() != 6 or now.hour != 18:
            return False
        row = conn.execute("SELECT v FROM scan_state WHERE k='last_weekly'").fetchone()
        if row and row[0] == now.strftime("%Y-%W"):
            return False
    week_ago = int(time.time()) - 7 * 86400
    by_status = dict(conn.execute("SELECT status, COUNT(*) FROM postings GROUP BY status").fetchall())
    new_week = conn.execute("SELECT COUNT(*) FROM postings WHERE first_seen > ?", (week_ago,)).fetchone()[0]
    subs_week = conn.execute("SELECT COUNT(*) FROM applications WHERE submitted_at > ?", (week_ago,)).fetchone()[0]
    subs_all = conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0]
    by_source = dict(conn.execute(
        "SELECT source, COUNT(*) FROM postings WHERE first_seen > ? GROUP BY source", (week_ago,)).fetchall())
    try:
        events = dict(conn.execute(
            "SELECT category, COUNT(*) FROM inbox_events WHERE ts > ? "
            "AND category NOT IN ('skip','not_job_related') GROUP BY category", (week_ago,)).fetchall())
    except sqlite3.OperationalError:
        events = {}
    body = (
        f"WEEK IN REVIEW\n\n"
        f"New postings discovered: {new_week}\n"
        f"  by source: {by_source}\n"
        f"Applications submitted this week: {subs_week} (total: {subs_all})\n"
        f"Pipeline: {by_status}\n\n"
        f"Inbox events this week: {events or 'none'}\n\n"
        f"Response rate so far: "
        f"{events.get('oa_invite', 0) + events.get('interview_invite', 0) + events.get('recruiter_reply', 0)}"
        f" positive signals / {subs_all} applications\n"
    )
    mailer.send("[jobhunt] weekly stats", body)
    conn.execute("INSERT OR REPLACE INTO scan_state VALUES ('last_weekly', ?)", (now.strftime("%Y-%W"),))
    conn.commit()
    conn.close()
    return True


if __name__ == "__main__":
    print("sent" if weekly_stats(force="--force" in sys.argv) else "not time")
