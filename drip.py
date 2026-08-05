"""Drip scheduler: run by launchd hourly 9am-9pm ET; sends ~1 tailored email per run
(12/day max), prioritized by location > freshness > easy ATS. Also runs the revise poller
and a nightly summary at 21h.
"""
from __future__ import annotations

import datetime
import sqlite3
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT), str(ROOT / "apply"), str(ROOT / "tailor"), str(ROOT / "notify")]

DB = ROOT / "out" / "tracker.db"
ET = ZoneInfo("America/New_York")
DAILY_CAP = 12

LOC_PRIORITY = ["san francisco", "sf", "bay area", "palo alto", "mountain view", "menlo",
                "new york", "nyc", "manhattan", "brooklyn", "remote"]
EASY_ATS = ("greenhouse", "lever", "ashby")


def loc_score(locations: str) -> int:
    l = (locations or "").lower()
    for i, kw in enumerate(LOC_PRIORITY):
        if kw in l:
            return i
    return len(LOC_PRIORITY)


def pick_next(conn: sqlite3.Connection):
    from jd import detect_ats
    rows = conn.execute("SELECT * FROM postings WHERE status='queued'").fetchall()
    if not rows:
        return None
    def key(r):
        ats = detect_ats(r["url"])
        return (loc_score(r["locations"]), 0 if ats in EASY_ATS else 1, -r["first_seen"])
    return sorted(rows, key=key)[0]


def sent_today(conn) -> int:
    midnight = datetime.datetime.now(ET).replace(hour=0, minute=0, second=0).timestamp()
    return conn.execute("SELECT COUNT(*) FROM emails WHERE sent_at > ?", (midnight,)).fetchone()[0]


def run():
    now = datetime.datetime.now(ET)
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    # 1) watcher + filter every run
    from watcher import watch, filter as filt  # noqa
    summary = watch.run()

    # 1b) startup discovery once a day (10:00-11:00 window)
    if now.hour == 10:
        try:
            from watcher import startups
            sres = startups.run()
            print(f"[drip] startups: {sres}")
        except Exception as e:
            print(f"[drip] startup discovery failed: {e}")

    filt_res = filt.run()
    print(f"[drip] watcher: {summary['new_count']} new, filter: {filt_res}")

    # 2) revise poller
    import revise
    actions = revise.poll_once(verbose=False)
    if any(actions.values()):
        print(f"[drip] revise actions: {actions}")

    # 3) drip one tailored email if within window and under cap
    if 9 <= now.hour < 21 and sent_today(conn) < DAILY_CAP:
        row = pick_next(conn)
        if row:
            import batch
            from jd import fetch_jd, detect_ats
            from tailor import tailor
            import mailer
            batch.ensure_email_table(conn)
            jd_text = fetch_jd(row["url"])
            pdf = tailor(row["posting_id"], row["company"], row["title"], jd_text)
            if pdf:
                body = (f"{row['company']} — {row['title']}\n"
                        f"ATS: {detect_ats(row['url'])}\nLocations: {row['locations']}\n"
                        f"Link: {row['url']}\n\nTailored resume attached.\n\n"
                        "Reply: suggestions -> revision | 'approve' -> submit queue | 'skip' -> drop.\n"
                        "No reply in 72h -> auto-approved (easy ATS only).")
                resp = mailer.send(f"[jobhunt] {row['company']} — {row['title']}", body, [pdf])
                conn.execute("INSERT OR IGNORE INTO sent_messages VALUES (?)", (resp.get("id"),))
                conn.execute("INSERT OR REPLACE INTO emails VALUES (?,?,?,?,?,?,0)",
                             (row["posting_id"], resp.get("threadId"), resp.get("id"),
                              str(pdf), str(pdf.with_suffix('.tex')), int(time.time())))
                conn.execute("UPDATE postings SET status='tailored' WHERE posting_id=?",
                             (row["posting_id"],))
                conn.commit()
                print(f"[drip] emailed: {row['company']} — {row['title']}")

    # 4) 72h auto-approve for easy-ATS threads with no reply
    from jd import detect_ats as _ats
    cutoff = time.time() - 72 * 3600
    for r in conn.execute("SELECT e.*, p.url, p.company FROM emails e JOIN postings p USING(posting_id) "
                          "WHERE p.status='tailored' AND e.sent_at < ? AND e.revision=0", (cutoff,)).fetchall():
        if _ats(r["url"]) in EASY_ATS:
            conn.execute("UPDATE postings SET status='ready' WHERE posting_id=?", (r["posting_id"],))
            print(f"[drip] auto-approved after 72h: {r['company']}")
    conn.commit()

    # 5) nightly summary at 21h
    if now.hour == 21:
        import mailer
        stats = dict(conn.execute("SELECT status, COUNT(*) FROM postings GROUP BY status").fetchall())
        mailer.send("[jobhunt] daily summary",
                    "Queue state:\n" + "\n".join(f"  {k}: {v}" for k, v in sorted(stats.items())))
    conn.close()


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        # systemic failure -> alert email (rule 8)
        try:
            sys.path.insert(0, str(ROOT / "notify"))
            import mailer
            mailer.send("[jobhunt] PIPELINE ERROR", f"{type(e).__name__}: {e}")
        except Exception:
            pass
        raise
