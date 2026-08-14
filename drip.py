"""Drip scheduler: discover postings and prepare a small resume batch each hour.

Tailoring is not user-facing and runs around the clock.  It prioritizes ATSs the
system can actually submit before spending model calls on unsupported forms.
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
DAILY_CAP = 50
TAILOR_PER_RUN = 5

LOC_PRIORITY = ["san francisco", "sf", "bay area", "palo alto", "mountain view", "menlo",
                "new york", "nyc", "manhattan", "brooklyn", "remote"]
SUPPORTED_ATS = ("greenhouse", "lever", "ashby", "workday", "smartrecruiters", "rippling")
CLAIM_TIMEOUT_SECONDS = 2 * 60 * 60
CLAIM_TRANSITIONS = {
    ("queued", "tailoring"),
    ("queued", "sprinting"),
    ("ready", "submitting"),
    ("sprinting", "submitting"),
    ("tailoring", "queued"),
    ("tailoring", "ready"),
    ("sprinting", "queued"),
    ("sprinting", "filtered_out"),
    ("submitting", "ready"),
    ("submitting", "manual"),
    ("submitting", "failed"),
    ("submitting", "filtered_out"),
    ("submitting", "submitted"),
}


def loc_score(locations: str) -> int:
    l = (locations or "").lower()
    for i, kw in enumerate(LOC_PRIORITY):
        if kw in l:
            return i
    return len(LOC_PRIORITY)


def pick_next(conn: sqlite3.Connection, excluded: set[str] | None = None):
    from jd import detect_ats
    rows = conn.execute("SELECT * FROM postings WHERE status='queued'").fetchall()
    excluded = excluded or set()
    rows = [r for r in rows if r["posting_id"] not in excluded]
    if not rows:
        return None
    def key(r):
        ats = detect_ats(r["url"])
        return (0 if ats in SUPPORTED_ATS else 1, loc_score(r["locations"]), -r["first_seen"])
    return sorted(rows, key=key)[0]


def sent_today(conn) -> int:
    midnight = datetime.datetime.now(ET).replace(hour=0, minute=0, second=0).timestamp()
    return conn.execute("SELECT COUNT(*) FROM emails WHERE sent_at > ?", (midnight,)).fetchone()[0]


def transition_claim(conn: sqlite3.Connection, posting_id: str, expected_status: str,
                     status: str, *, commit: bool = True) -> bool:
    """Compare-and-set one workflow state without clobbering another worker."""
    if (expected_status, status) not in CLAIM_TRANSITIONS:
        raise ValueError(f"invalid claim transition: {expected_status} -> {status}")
    changed = conn.execute(
        "UPDATE postings SET status=?, last_attempt_at=? "
        "WHERE posting_id=? AND status=?",
        (status, int(time.time()), posting_id, expected_status),
    ).rowcount
    if commit:
        conn.commit()
    return changed == 1


def claim_posting(conn: sqlite3.Connection, posting_id: str, status: str,
                  from_status: str = "queued") -> bool:
    """Atomically reserve a posting for one worker."""
    if status not in {"tailoring", "sprinting", "submitting"}:
        raise ValueError(f"invalid claim status: {status}")
    return transition_claim(conn, posting_id, from_status, status)


def release_claim(conn: sqlite3.Connection, posting_id: str, claim_status: str,
                  status: str = "queued") -> bool:
    """Release only the claim this worker still owns."""
    if status not in {"queued", "ready", "manual"}:
        raise ValueError(f"invalid release status: {status}")
    return transition_claim(conn, posting_id, claim_status, status)


def recover_stale_claims(conn: sqlite3.Connection) -> int:
    """Recover safe work; quarantine an interrupted browser submission."""
    cutoff = int(time.time()) - CLAIM_TIMEOUT_SECONDS
    safe = conn.execute(
        "UPDATE postings SET status='queued' "
        "WHERE status IN ('tailoring','sprinting') "
        "AND COALESCE(last_attempt_at, 0) < ?",
        (cutoff,),
    ).rowcount
    uncertain = conn.execute(
        "UPDATE postings SET status='manual', outcome='manual', "
        "last_error='submission interrupted; verify possible prior submission' "
        "WHERE status='submitting' AND COALESCE(last_attempt_at, 0) < ?",
        (cutoff,),
    ).rowcount
    conn.commit()
    return safe + uncertain


def run():
    now = datetime.datetime.now(ET)
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    recovered = recover_stale_claims(conn)
    if recovered:
        print(f"[drip] recovered {recovered} stale tailoring claims")

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

    # 3) Tailor a bounded batch and queue it directly, with no approval email.
    # This used to prepare one resume per hour only between 9am and 9pm, leaving
    # a nine-day backlog despite ample submission capacity.
    attempts = min(TAILOR_PER_RUN, max(0, DAILY_CAP - sent_today(conn)))
    excluded: set[str] = set()
    for _ in range(attempts):
        row = pick_next(conn, excluded)
        if row:
            excluded.add(row["posting_id"])
            if not claim_posting(conn, row["posting_id"], "tailoring"):
                continue
            try:
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
                    if transition_claim(conn, row["posting_id"], "tailoring", "ready",
                                        commit=False):
                        conn.commit()
                        print(f"[drip] tailored and queued: {row['company']} — {row['title']}")
                    else:
                        conn.rollback()
                        print(f"[drip] claim lost; discarded late result: {row['company']}")
                else:
                    release_claim(conn, row["posting_id"], "tailoring")
            except Exception as e:
                # One bad JD or model call must not block the other four slots.
                release_claim(conn, row["posting_id"], "tailoring")
                print(f"[drip] tailoring failed for {row['company']}: {type(e).__name__}: {e}")

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
