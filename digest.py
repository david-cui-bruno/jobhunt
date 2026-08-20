"""One casual daily digest instead of a pile of [jobhunt] emails.

Collects, since the last digest:
  - postings stuck in 'manual' (grouped by why, with the one-liner ask)
  - possible-but-unverified submissions (quarantined)
  - action-needed inbox events (OA/interview/recruiter/offer), deadline-first
  - pipeline stats (submitted / ready / queued)
plus fleet notes dropped by other agents in out/digest-notes.log.

Tone: short, lowercase-casual, skimmable in 30 seconds. One email a day at
most (evening); nothing to say -> no email. David can just reply to the
email — replies are picked up by revise.py/inbox.py's existing loops and by
the agent reading the thread.

A shortened copy (<=1200 chars, top 3 items + stats) also goes to David's
phone through the kith-bridge outbox (notify/kith_bridge.py). Best-effort:
if kith/the bridge is down the email still goes out and we log one line.
He can text digest commands back — digest_replies.process_imessage picks
them up on the same inbox timer.

Run: python3 digest.py            (respects the once-a-day guard)
     python3 digest.py --force    (send now regardless)
     python3 digest.py --dry-run  (print, never send)
"""
from __future__ import annotations

import datetime
import json
import sqlite3
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from notify import mailer

ROOT = Path(__file__).resolve().parent
DB = ROOT / "out" / "tracker.db"
NOTES = ROOT / "out" / "digest-notes.log"
ET = ZoneInfo("America/New_York")
SEND_HOUR = 18  # 6pm ET
SHORT_LIMIT = 3800  # phone copy hard cap (Telegram chunks at 3900; was 1200 in the iMessage era)

# manual reasons that are pure engineering debt — the agent handles these; not
# worth David's attention in the digest beyond a count.
AGENT_DEBT_PREFIXES = (
    "resume quality gate",
    "no adapter for",
    "legacy terminal status",
    "JD page removed",
)


def _state(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS scan_state (k TEXT PRIMARY KEY, v TEXT)")


def _last_digest_day(conn) -> str:
    row = conn.execute("SELECT v FROM scan_state WHERE k='last_daily_digest'").fetchone()
    return row[0] if row else ""


def _since_ts(conn) -> int:
    row = conn.execute("SELECT v FROM scan_state WHERE k='last_daily_digest_ts'").fetchone()
    return int(row[0]) if row else int(time.time()) - 86400


def collect(conn, since: int) -> dict:
    manual_ask = []   # needs David's actual input
    manual_debt = {}  # agent-fixable, count by reason prefix
    for company, title, url, err in conn.execute(
        "SELECT company, title, url, COALESCE(last_error,'') FROM postings "
        "WHERE status='manual' AND COALESCE(last_attempt_at, first_seen) > ? "
        "ORDER BY COALESCE(last_attempt_at, first_seen) DESC", (since,)):
        if any(err.startswith(p) for p in AGENT_DEBT_PREFIXES):
            key = next(p for p in AGENT_DEBT_PREFIXES if err.startswith(p))
            manual_debt[key] = manual_debt.get(key, 0) + 1
        else:
            manual_ask.append((company, title, url, err))

    verify = conn.execute(
        "SELECT company, title, url FROM postings "
        "WHERE outcome='submitted' AND status='manual' AND COALESCE(last_attempt_at, first_seen) > ?",
        (since,)).fetchall()

    action = conn.execute(
        "SELECT category, company, role, deadline, action_url, summary FROM inbox_events "
        "WHERE category IN ('oa_invite','interview_invite','recruiter_reply','offer') AND ts > ? "
        "ORDER BY CASE WHEN deadline='' THEN 1 ELSE 0 END, deadline", (since,)).fetchall()

    day_ago = int(time.time()) - 86400
    stats = {
        "submitted_24h": conn.execute(
            "SELECT COUNT(*) FROM postings WHERE status='submitted' AND last_attempt_at > ?",
            (day_ago,)).fetchone()[0],
        "ready": conn.execute("SELECT COUNT(*) FROM postings WHERE status='ready'").fetchone()[0],
        "queued": conn.execute("SELECT COUNT(*) FROM postings WHERE status='queued'").fetchone()[0],
    }

    notes = []
    if NOTES.exists():
        cutoff = since
        for line in NOTES.read_text().splitlines():
            try:
                obj = json.loads(line)
                if int(obj.get("ts", 0)) > cutoff:
                    notes.append(obj.get("note", ""))
            except (json.JSONDecodeError, ValueError):
                continue
    return {"manual_ask": manual_ask, "manual_debt": manual_debt, "verify": verify,
            "action": action, "stats": stats, "notes": [n for n in notes if n]}


def compose(d: dict) -> str | None:
    """Casual, concise body. None -> nothing worth sending."""
    sections = []

    if d["action"]:
        lines = []
        today = datetime.datetime.now(ET).date()
        for cat, company, role, deadline, url, summary in d["action"][:8]:
            tag = {"oa_invite": "OA", "interview_invite": "interview",
                   "recruiter_reply": "recruiter", "offer": "OFFER"}.get(cat, cat)
            flag = ""
            if deadline:
                try:
                    days = (datetime.date.fromisoformat(deadline) - today).days
                    flag = f" — due in {days}d" if days <= 5 else f" — due {deadline}"
                except ValueError:
                    flag = f" — due {deadline}"
            lines.append(f"• [{tag}] {company} ({role}){flag}\n  {summary[:110]}\n  {url}")
        sections.append("needs you:\n" + "\n".join(lines))

    if d["manual_ask"]:
        lines = []
        for company, title, url, err in d["manual_ask"][:6]:
            ask = err
            if err.startswith("needs answers:"):
                ask = "couldn't answer: " + err[len("needs answers:"):].strip()[:140]
            lines.append(f"• {company} — {title[:50]}\n  {ask[:150]}\n  {url}")
        sections.append(
            "stuck applications (reply with answers and i'll retry, or say skip):\n" + "\n".join(lines))

    if d["verify"]:
        lines = [f"• {c} — {t[:55]}\n  {u}" for c, t, u in d["verify"][:5]]
        sections.append("might have submitted, couldn't confirm (check if you care):\n" + "\n".join(lines))

    if d["notes"]:
        sections.append("fleet notes:\n" + "\n".join(f"• {n[:160]}" for n in d["notes"][:5]))

    s = d["stats"]
    debt = sum(d["manual_debt"].values())
    tail = f"pipeline: {s['submitted_24h']} submitted today · {s['ready']} ready · {s['queued']} queued"
    if debt:
        tail += f" · {debt} stuck on my side (on it, not yours)"

    if not sections:
        return None  # stats alone aren't worth an email
    body = "hey — quick jobhunt rundown:\n\n" + "\n\n".join(sections) + f"\n\n{tail}\n\n— jobhunt bot (just reply to this email, i read it)"
    return body


def compose_short(d: dict) -> str | None:
    """Phone digest: the real content, sized for one Telegram message.

    Telegram became the PRIMARY channel 2026-08-19 (David: "I'd rather have
    jobhunt sent to my telegram via kith more than anything else"); email is
    the archive copy. So this is no longer a 3-item teaser: action items,
    stuck applications with the exact asks, unconfirmed submissions, and the
    stats line, with URLs only where he might act from his phone.
    None -> nothing worth texting (same nothing-to-say rule as compose()).
    """
    sections = []
    today = datetime.datetime.now(ET).date()

    if d["action"]:
        lines = []
        for cat, company, role, deadline, url, _summary in d["action"][:8]:
            tag = {"oa_invite": "OA", "interview_invite": "interview",
                   "recruiter_reply": "recruiter", "offer": "OFFER"}.get(cat, cat)
            flag = ""
            if deadline:
                try:
                    days = (datetime.date.fromisoformat(deadline) - today).days
                    flag = f" — due in {days}d" if days <= 5 else f" — due {deadline}"
                except ValueError:
                    flag = f" — due {deadline}"
            lines.append(f"• [{tag}] {company} ({role[:45]}){flag}\n  {url}")
        sections.append("needs you:\n" + "\n".join(lines))

    if d["manual_ask"]:
        lines = []
        for company, title, _url, err in d["manual_ask"][:6]:
            ask = err
            if err.startswith("needs answers:"):
                ask = err[len("needs answers:"):].strip()
            lines.append(f"• {company} — {ask[:110]}")
        sections.append("stuck (reply \"<company>: <answer>\" or \"skip <company>\"):\n"
                        + "\n".join(lines))

    if d["verify"]:
        lines = [f"• {c} — {t[:45]}" for c, t, _u in d["verify"][:5]]
        sections.append("maybe submitted, unconfirmed:\n" + "\n".join(lines))

    if d["notes"]:
        sections.append("fleet notes:\n" + "\n".join(f"• {n[:140]}" for n in d["notes"][:3]))

    if not sections:
        return None

    s = d["stats"]
    debt = sum(d["manual_debt"].values())
    tail = f"pipeline: {s['submitted_24h']} submitted today · {s['ready']} ready · {s['queued']} queued"
    if debt:
        tail += f" · {debt} stuck on my side"
    body = ("hey — jobhunt daily:\n\n" + "\n\n".join(sections) + f"\n\n{tail}\n"
            "(full links in the email · ask me \"jobhunt status\" anytime)")
    return body[:SHORT_LIMIT]


def _send_phone_copy(d: dict) -> None:
    """Best-effort iMessage copy via the kith-bridge outbox. Never raises —
    the email is the source of truth; a down bridge costs one log line."""
    short = compose_short(d)
    if not short:
        return
    try:
        from notify import kith_bridge
        kith_bridge.send_phone(short)
    except Exception as e:
        print(f"digest: phone copy failed (email still sent): {e}")


def run(force: bool = False, dry: bool = False) -> bool:
    now = datetime.datetime.now(ET)
    conn = sqlite3.connect(DB)
    _state(conn)
    today = now.strftime("%Y-%m-%d")
    if not force and not dry:
        if now.hour < SEND_HOUR or _last_digest_day(conn) == today:
            conn.close()
            return False
    d = collect(conn, _since_ts(conn))
    body = compose(d)
    if dry:
        print(body or "(nothing to send)")
        conn.close()
        return bool(body)
    if body:
        import os
        os.environ["JOBHUNT_EMAIL_NOTICES"] = "1"  # digest is the ONE allowed email
        mailer.send(f"jobhunt daily — {now.strftime('%b %-d')}", body)
        _send_phone_copy(d)
    conn.execute("INSERT OR REPLACE INTO scan_state VALUES ('last_daily_digest', ?)", (today,))
    conn.execute("INSERT OR REPLACE INTO scan_state VALUES ('last_daily_digest_ts', ?)", (str(int(time.time())),))
    conn.commit()
    conn.close()
    return bool(body)


if __name__ == "__main__":
    sent = run(force="--force" in sys.argv, dry="--dry-run" in sys.argv)
    print("digest sent" if sent and "--dry-run" not in sys.argv else "done")
