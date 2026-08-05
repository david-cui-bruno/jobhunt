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


GAME_APPS = ("league of legends", "leagueclient", "riot client", "valorant", "steam_osx",
             "cs2", "dota 2", "minecraft")


def _user_is_gaming() -> bool:
    """True if a known game is running or the frontmost app is fullscreen."""
    import subprocess
    try:
        ps = subprocess.run(["ps", "-axo", "comm"], capture_output=True, text=True, timeout=5).stdout.lower()
        if any(g in ps for g in GAME_APPS):
            return True
    except Exception:
        pass
    return False


DEAD_MARKERS = ("job not found", "no longer available", "job you requested was not found",
                "position has been filled", "posting is closed", "job posting is no longer")


def _posting_dead(url: str) -> bool:
    """Cheap liveness sniff before spending a browser session."""
    import urllib.request as _ur
    try:
        req = _ur.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with _ur.urlopen(req, timeout=15) as r:
            body = r.read(60000).decode("utf-8", "replace").lower()
        return any(m in body for m in DEAD_MARKERS)
    except Exception:
        return False


def submit_ready(limit: int = HOURLY_CAP, dry_run: bool = False) -> list[dict]:
    from jd import detect_ats
    from greenhouse import apply_greenhouse
    from lever import apply_lever
    from ashby import apply_ashby
    from workday import apply_workday
    from smartrecruiters import apply_smartrecruiters
    from rippling import apply_rippling
    sys.path.insert(0, str(ROOT / "watcher"))
    from waas import apply_waas
    import mailer

    now = datetime.datetime.now(ET)
    if not (9 <= now.hour < 21):
        return []
    if _user_is_gaming():
        print("[submit] deferring: game/fullscreen app active")
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
              "workday": apply_workday, "smartrecruiters": apply_smartrecruiters,
              "rippling": apply_rippling}.get(ats)
        if fn is None and "workatastartup.com" in r["url"]:
            fn = lambda url, pdf, slug, dry_run=False: apply_waas(url, slug, dry_run=dry_run)
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
        if _posting_dead(r["url"]):
            conn.execute("UPDATE postings SET status='filtered_out' WHERE posting_id=?",
                         (r["posting_id"],))
            conn.commit()
            results.append({"company": r["company"], "ats": ats, "status": "dead posting"})
            continue
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
