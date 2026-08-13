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
DAILY_CAP = 20

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
        try:
            from watcher import waas
            print(f"[drip] waas new: {waas.scrape()}")
        except Exception as e:
            print(f"[drip] waas scrape failed: {e}")

    filt_res = filt.run()
    print(f"[drip] watcher: {summary['new_count']} new, filter: {filt_res}")

    # 2) email applications: compose and send ready HN postings autonomously.
    # poll_approvals only drains approval threads created by older releases.
    try:
        import email_apply
        comp = email_apply.compose_ready_email_postings()
        appr = email_apply.poll_approvals()
        if comp or appr:
            print(f"[drip] email apps: composed={comp} approvals={appr}")
    except Exception as e:
        print(f"[drip] email apps failed: {e}")

    # 3) tailor one posting and queue it directly, with no approval email.
    if 9 <= now.hour < 21 and sent_today(conn) < DAILY_CAP:
        row = pick_next(conn)
        if row:
            import batch
            from jd import fetch_jd
            from tailor import tailor
            batch.ensure_email_table(conn)
            jd_text = fetch_jd(row["url"])
            pdf = tailor(row["posting_id"], row["company"], row["title"], jd_text)
            if pdf:
                conn.execute("INSERT OR REPLACE INTO emails VALUES (?,?,?,?,?,?,0)",
                             (row["posting_id"], None, None,
                              str(pdf), str(pdf.with_suffix('.tex')), int(time.time())))
                conn.execute("UPDATE postings SET status='ready' WHERE posting_id=?",
                             (row["posting_id"],))
                conn.commit()
                print(f"[drip] tailored and queued: {row['company']} — {row['title']}")

    # 4) Drain legacy tailored rows immediately. New rows enter ready directly.
    for r in conn.execute("SELECT posting_id, company FROM postings WHERE status='tailored'").fetchall():
        conn.execute("UPDATE postings SET status='ready' WHERE posting_id=?", (r["posting_id"],))
        print(f"[drip] legacy tailored row queued: {r['company']}")
    conn.commit()

    # 4b) weekly funnel stats (Sunday 6pm)
    try:
        from weekly import weekly_stats
        if weekly_stats():
            print("[drip] weekly stats sent")
    except Exception as e:
        print(f"[drip] weekly stats failed: {e}")

    # 5) nightly summary at 21h — FULL-AUTO audit digest: everything that was
    # submitted/sent/answered today, so David reviews after the fact.
    if now.hour == 21:
        import json as _json
        import mailer
        stats = dict(conn.execute("SELECT status, COUNT(*) FROM postings GROUP BY status").fetchall())
        day_start = int(datetime.datetime(now.year, now.month, now.day, tzinfo=ET).timestamp())
        subs = conn.execute(
            "SELECT p.company, p.title, a.ats, a.confirmation FROM applications a "
            "JOIN postings p USING(posting_id) WHERE a.submitted_at >= ? "
            "ORDER BY a.submitted_at", (day_start,)).fetchall()
        lines = ["Queue state:"] + [f"  {k}: {v}" for k, v in sorted(stats.items())]
        lines += ["", f"Applications submitted today ({len(subs)}):"]
        lines += [f"  {r[0]} — {r[1]} [{r[2]}] {r[3] or ''}" for r in subs] or ["  (none)"]
        qa_log = ROOT / "out" / "qa_answers.log"
        if qa_log.exists():
            today_str = now.strftime("%Y-%m-%d")
            answers = [
                _json.loads(l) for l in qa_log.read_text().splitlines()
                if l.strip() and l.startswith('{"ts": "' + today_str)]
            long_ones = [a for a in answers if len(str(a.get("answer", ""))) > 120]
            lines += ["", f"Form answers auto-filled today: {len(answers)} "
                          f"({len(long_ones)} long-form, shown below):"]
            for a in long_ones:
                lines += [f"  Q: {a.get('question')}", f"  A: {a.get('answer')}", ""]
        mailer.send("[jobhunt] daily summary", "\n".join(lines))
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
