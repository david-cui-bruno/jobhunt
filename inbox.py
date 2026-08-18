"""Recruiter-reply and OA-deadline handling.

Every run (launchd, 30 min):
  1. Scan inbound mail since last run (excluding self-sent, promos).
  2. Claude classifies each application-related email:
       confirmation | oa_invite | interview_invite | recruiter_reply |
       rejection | offer | other
     and extracts company, role, deadline, action link.
  3. Apply Gmail labels (jobhunt/confirmations, jobhunt/action-needed,
     jobhunt/rejections); archive confirmations to keep inbox clean.
  4. Record in tracker (events table). OA/interview deadlines go into the
     deadlines table.
  5. Daily 6pm digest email: pending actions sorted by deadline, expiring soon
     flagged. (Per David: label quietly, no per-email forwards.)
"""
from __future__ import annotations

import datetime
import json
import os
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "notify"))
import mailer  # noqa: E402

DB = ROOT / "out" / "tracker.db"
ET = ZoneInfo("America/New_York")
MODEL = "claude-sonnet-5"
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

SKIP_FROM = re.compile(
    r"linkedin|no-?reply@(google|apple)|amtrak|frontier|expedia|chase|ally|taco ?bell|"
    r"soundcloud|headspace|hilton|spotify|doordash|uber|notifications@link\.com", re.I)

CLASSIFY_PROMPT = """Classify this email for a job-application tracker. The candidate applies to many SWE internships.

From: {sender}
Subject: {subject}
Body (truncated): {body}

Return ONLY JSON:
{{"category": "confirmation|oa_invite|interview_invite|recruiter_reply|rejection|offer|verification_code|not_job_related",
  "company": str, "role": str or "",
  "deadline": "YYYY-MM-DD" or "" (any stated deadline/expiry for OA/interview/scheduling),
  "action_url": str or "", "summary": one sentence}}"""


def _claude(prompt: str) -> dict | None:
    body = json.dumps({"model": MODEL, "max_tokens": 500,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body,
        headers={"x-api-key": API_KEY, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            resp = json.load(r)
        text = "".join(b.get("text", "") for b in resp["content"] if b.get("type") == "text")
        m = re.search(r"\{.*\}", text, re.S)
        return json.loads(m.group(0)) if m else None
    except Exception:
        return None


def ensure_tables(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS inbox_events (
        message_id TEXT PRIMARY KEY, ts INTEGER, category TEXT, company TEXT,
        role TEXT, deadline TEXT, action_url TEXT, summary TEXT);
    CREATE TABLE IF NOT EXISTS scan_state (k TEXT PRIMARY KEY, v TEXT);
    """)
    conn.commit()


def get_or_create_label(name: str) -> str:
    labels = mailer._call("/labels").get("labels", [])
    for l in labels:
        if l["name"] == name:
            return l["id"]
    created = mailer._call("/labels", {"name": name, "labelListVisibility": "labelShow",
                                       "messageListVisibility": "show"})
    return created["id"]


LABELS = {}


def label_msg(msg_id: str, label_name: str, archive: bool = False):
    if label_name not in LABELS:
        LABELS[label_name] = get_or_create_label(label_name)
    body = {"addLabelIds": [LABELS[label_name]]}
    if archive:
        body["removeLabelIds"] = ["INBOX"]
    mailer._call(f"/messages/{msg_id}/modify", body)


CATEGORY_LABEL = {
    "confirmation": ("jobhunt/confirmations", True),   # archive: keep inbox clean
    "oa_invite": ("jobhunt/action-needed", False),
    "interview_invite": ("jobhunt/action-needed", False),
    "recruiter_reply": ("jobhunt/action-needed", False),
    "rejection": ("jobhunt/rejections", True),
    "offer": ("jobhunt/action-needed", False),
}


def scan(verbose: bool = True) -> dict:
    conn = sqlite3.connect(DB)
    ensure_tables(conn)
    row = conn.execute("SELECT v FROM scan_state WHERE k='last_scan'").fetchone()
    last = int(row[0]) if row else int(time.time()) - 3 * 86400
    q = f"after:{last} -from:davidcui824@gmail.com in:inbox"
    data = mailer._call("/messages?q=" + urllib.parse.quote(q) + "&maxResults=50")
    stats = {"scanned": 0, "classified": {}, "deadlines": 0}
    for m in data.get("messages", []) or []:
        if conn.execute("SELECT 1 FROM inbox_events WHERE message_id=?", (m["id"],)).fetchone():
            continue
        full = mailer._call(f"/messages/{m['id']}?format=full")
        hdrs = mailer.headers_of(full)
        sender = hdrs.get("from", "")
        subject = hdrs.get("subject", "")
        if SKIP_FROM.search(sender):
            continue
        body_text = mailer.extract_plain(full) or full.get("snippet", "")
        stats["scanned"] += 1
        c = _claude(CLASSIFY_PROMPT.format(sender=sender[:100], subject=subject[:150],
                                           body=body_text[:2500]))
        if not c or c.get("category") in (None, "not_job_related", "verification_code"):
            cat = (c or {}).get("category", "skip")
            conn.execute("INSERT OR IGNORE INTO inbox_events VALUES (?,?,?,?,?,?,?,?)",
                         (m["id"], int(time.time()), cat, "", "", "", "", ""))
            conn.commit()
            continue
        cat = c["category"]
        stats["classified"][cat] = stats["classified"].get(cat, 0) + 1
        conn.execute("INSERT OR IGNORE INTO inbox_events VALUES (?,?,?,?,?,?,?,?)",
                     (m["id"], int(full.get("internalDate", 0)) // 1000, cat,
                      c.get("company", ""), c.get("role", ""), c.get("deadline", ""),
                      c.get("action_url", ""), c.get("summary", "")))
        conn.commit()
        if c.get("deadline"):
            stats["deadlines"] += 1
        if cat in CATEGORY_LABEL:
            name, archive = CATEGORY_LABEL[cat]
            try:
                label_msg(m["id"], name, archive)
            except Exception as e:
                print(f"[inbox] label failed: {e}")
        if verbose:
            print(f"[inbox] {cat}: {c.get('company')} — {c.get('summary', '')[:80]}")
    conn.execute("INSERT OR REPLACE INTO scan_state VALUES ('last_scan', ?)", (str(int(time.time())),))
    conn.commit()
    conn.close()
    return stats


def digest(force: bool = False) -> bool:
    """6pm ET daily digest of action-needed items, deadline-sorted."""
    now = datetime.datetime.now(ET)
    conn = sqlite3.connect(DB)
    ensure_tables(conn)
    if not force:
        if now.hour != 18:
            return False
        row = conn.execute("SELECT v FROM scan_state WHERE k='last_digest'").fetchone()
        if row and row[0] == now.strftime("%Y-%m-%d"):
            return False
    rows = conn.execute(
        "SELECT category, company, role, deadline, action_url, summary, ts FROM inbox_events "
        "WHERE category IN ('oa_invite','interview_invite','recruiter_reply','offer') "
        "AND ts > ? ORDER BY CASE WHEN deadline='' THEN 1 ELSE 0 END, deadline",
        (int(time.time()) - 14 * 86400,)).fetchall()
    if rows:
        today = now.date()
        lines = []
        for cat, company, role, deadline, url, summary, ts in rows:
            flag = ""
            if deadline:
                try:
                    dl = datetime.date.fromisoformat(deadline)
                    days = (dl - today).days
                    flag = f" ⚠️ DUE IN {days}d" if days <= 3 else f" (due {deadline})"
                except ValueError:
                    flag = f" (due {deadline})"
            lines.append(f"[{cat}] {company} — {role}{flag}\n  {summary}\n  {url}")
        mailer.send("[jobhunt] ACTION NEEDED digest",
                    f"{len(rows)} items needing your attention:\n\n" + "\n\n".join(lines))
    conn.execute("INSERT OR REPLACE INTO scan_state VALUES ('last_digest', ?)",
                 (now.strftime("%Y-%m-%d"),))
    conn.commit()
    conn.close()
    return bool(rows)


if __name__ == "__main__":
    force = "--digest" in sys.argv
    print(scan())
    # The unified daily digest (digest.py) replaced this module's own 6pm
    # email (2026-08-17): one casual message covering action-needed inbox
    # events AND stuck/unverified applications, instead of parallel digests.
    import digest as daily_digest
    sent = daily_digest.run(force=force)
    print("digest sent" if sent else "no digest (empty or not time)")
    # act on any replies David sent to earlier digests (skip X / for Y: ...)
    try:
        import digest_replies
        acted = digest_replies.process()
        if any(acted.values()):
            print(f"digest replies: {acted}")
    except Exception as e:
        print(f"digest reply processing failed: {e}")
