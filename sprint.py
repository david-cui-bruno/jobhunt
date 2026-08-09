"""Sprint lane: near-instant apply for NEWLY-appearing postings.

Polls the three GitHub listing sources every few minutes (launchd). Any posting
that appears AND passes the filter is immediately tailored and submitted in the
same run - no veto window, no business-hours gate, no drip queue. Speed is the
point: early applicants get seen; postings close after a few hundred apps.

Safety properties (FULL AUTO, David ratified 2026-08-09):
  - only fires for postings first seen by THIS run (the backlog stays on drip)
  - same truthful-tailoring pipeline + validate() guards as drip
  - same adapter machinery as submit.py (isolated worker, timeouts)
  - FYI email with the submitted PDF after each sprint submission (audit trail;
    also counts into drip's daily-cap accounting via the emails table)
  - lockfile prevents overlapping runs; per-run cap prevents batch-update storms
  - unknown ATS / adapter failure -> posting falls back into the normal queue
    ('tailored', so drip's auto-approve picks it up within the hour)
"""
from __future__ import annotations

import fcntl
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT), str(ROOT / "apply"), str(ROOT / "tailor"), str(ROOT / "notify")]

DB = ROOT / "out" / "tracker.db"
LOCK = ROOT / "out" / "sprint.lock"
PER_RUN_CAP = 5          # batch repo updates: don't churn for an hour
SPRINT_DAILY_CAP = 15    # sprint submissions per day (drip backlog has its own 20)


def _sprint_submitted_today(conn) -> int:
    import datetime
    from zoneinfo import ZoneInfo
    midnight = datetime.datetime.now(ZoneInfo("America/New_York")).replace(
        hour=0, minute=0, second=0).timestamp()
    return conn.execute(
        "SELECT COUNT(1) FROM applications WHERE submitted_at > ? AND notes='sprint'",
        (midnight,)).fetchone()[0]


def run() -> list[dict]:
    from watcher import watch, filter as filt
    from jd import fetch_jd, detect_ats
    from tailor import tailor
    import mailer

    results = []
    summary = watch.run()                     # fetch + upsert new postings
    new_ids = [p["posting_id"] for p in summary.get("new", [])]
    if not new_ids:
        return results
    filt.run()                                # filter the fresh batch

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    done = 0
    for pid in new_ids:
        if done >= PER_RUN_CAP or _sprint_submitted_today(conn) >= SPRINT_DAILY_CAP:
            break
        r = conn.execute("SELECT * FROM postings WHERE posting_id=? AND status='queued'",
                         (pid,)).fetchone()
        if not r:
            continue  # filtered out, closed, or deduped
        print(f"[sprint] NEW: {r['company']} — {r['title']}", flush=True)
        try:
            jd_text = fetch_jd(r["url"]) or ""
            pdf = tailor(r["posting_id"], r["company"], r["title"], jd_text)
        except Exception as e:
            print(f"[sprint] tailor failed for {r['company']}: {e}", flush=True)
            continue
        if not pdf:
            print(f"[sprint] tailor returned None for {r['company']}", flush=True)
            continue

        # record the resume paths (submit machinery + revise thread need them)
        conn.execute("INSERT OR REPLACE INTO emails VALUES (?,?,?,?,?,?,0)",
                     (r["posting_id"], None, None, str(pdf),
                      str(pdf.with_suffix(".tex")), int(time.time())))
        conn.execute("UPDATE postings SET status='tailored' WHERE posting_id=?",
                     (r["posting_id"],))
        conn.commit()

        # submit RIGHT NOW via the isolated adapter (same as submit.py)
        import submit as submit_mod
        slug = f"{r['company'].replace(' ', '_')[:40]}_{int(time.time())}"
        if submit_mod._posting_dead(r["url"]):
            conn.execute("UPDATE postings SET status='filtered_out' WHERE posting_id=?",
                         (r["posting_id"],))
            conn.commit()
            continue
        res = submit_mod._isolated_adapter({
            "url": r["url"], "resume_pdf": str(pdf), "slug": slug, "dry_run": False,
        })
        outcome = submit_mod._outcome(res)
        reason = str(res.get("reason", ""))
        if outcome == "submitted":
            conn.execute("UPDATE postings SET status='submitted', outcome='submitted' "
                         "WHERE posting_id=?", (r["posting_id"],))
            conn.execute("INSERT OR REPLACE INTO applications VALUES (?,?,?,?,?,?)",
                         (r["posting_id"], str(pdf), str(res.get("detected_ats", "unknown")),
                          int(time.time()), reason, "sprint"))
            conn.commit()
            done += 1
            print(f"[sprint] SUBMITTED: {r['company']} ({reason})", flush=True)
            # FYI audit email (attachment = what was sent)
            try:
                resp = mailer.send(
                    f"[jobhunt] sprint-submitted: {r['company']} — {r['title']}",
                    f"{r['company']} — {r['title']}\n{r['url']}\n\n"
                    f"Auto-submitted {time.strftime('%H:%M')} within minutes of the "
                    f"posting appearing. Outcome: {reason}.\nResume attached.",
                    [pdf])
                conn.execute("INSERT OR IGNORE INTO sent_messages VALUES (?)",
                             (resp.get("id"),))
                conn.execute("UPDATE emails SET thread_id=?, message_id=? WHERE posting_id=?",
                             (resp.get("threadId"), resp.get("id"), r["posting_id"]))
                conn.commit()
            except Exception as e:
                print(f"[sprint] FYI email failed: {e}", flush=True)
        else:
            # leave in 'tailored': drip auto-approves within the hour and the
            # normal submit lane (with retries) takes over.
            print(f"[sprint] not submitted ({outcome}: {reason[:60]}); "
                  f"left for normal lane", flush=True)
        results.append({"company": r["company"], "outcome": outcome, "reason": reason})
    conn.close()
    return results


if __name__ == "__main__":
    LOCK.parent.mkdir(exist_ok=True)
    lf = open(LOCK, "w")
    try:
        fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(0)  # previous sprint still running
    try:
        for r in run():
            print(r)
    except Exception as e:
        try:
            import mailer
            mailer.send("[jobhunt] SPRINT ERROR", f"{type(e).__name__}: {e}")
        except Exception:
            pass
        raise
