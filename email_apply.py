"""Email-application composer for postings without an ATS (HN Who's Hiring, etc.).

Flow: posting marked 'ready' whose url is an HN item / mailto -> extract the
application email + instructions from the posting text -> Claude drafts a short
intro email -> approval request sent to you with the full draft.

FULL AUTO (David ratified 2026-08-08): reply 'send' to send immediately,
'skip' to drop, or edits in plain english. No reply within the veto window
(3h, business hours only) -> the draft is auto-sent as-is.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT / "notify")]
import mailer  # noqa: E402

DB = ROOT / "out" / "tracker.db"
MODEL = "claude-sonnet-5"
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

DRAFT_PROMPT = """Draft a short application email for this posting. Candidate: David Cui,
Brown CS+Econ '27 (GPA 4.0), ex-YC founding CTO (Framewise Health), SWE intern at Freya (YC S25,
real-time LLM voice agents) and Sotatek (fraud-detection ML). USACO/AIME background.

POSTING (from HN Who's Hiring):
{text}

Rules:
- <=140 words, plain text. No flattery, no "I hope this finds you well".
- Lead with the 1-2 most relevant facts for THIS posting.
- Mention the resume is attached.
- THE ONLY ATTACHMENT IS THE RESUME PDF. NEVER claim to include anything else
  (video, portfolio, code sample, cover letter). If the posting REQUIRES extra
  material we cannot attach, do not pretend: return {{"needs_manual": true}}
  instead of a draft so a human can prepare it.
- If the posting names a person, address them; else "Hi <Company> team".
- Sign off: David Cui, davidcui824@gmail.com, github.com/david-cui-bruno

Return ONLY JSON: {{"to": "email found in posting or ''", "subject": str, "body": str}}
or {{"needs_manual": true, "reason": str}} when required materials exceed a resume."""


def _claude(prompt: str) -> dict | None:
    body = json.dumps({"model": MODEL, "max_tokens": 800,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body,
        headers={"x-api-key": API_KEY, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as r:
        resp = json.load(r)
    text = "".join(b.get("text", "") for b in resp["content"] if b.get("type") == "text")
    m = re.search(r"\{.*\}", text, re.S)
    return json.loads(m.group(0)) if m else None


def fetch_hn_text(url: str) -> str:
    m = re.search(r"id=(\d+)", url)
    if not m:
        return ""
    req = urllib.request.Request(
        f"https://hn.algolia.com/api/v1/items/{m.group(1)}",
        headers={"User-Agent": "jobhunt"})
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.load(r)
    text = re.sub(r"<[^>]+>", " ", d.get("text") or "")
    return re.sub(r"\s+", " ", text).strip()


def compose_ready_email_postings(limit: int = 3) -> list[str]:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS email_apps (
        posting_id TEXT PRIMARY KEY, draft_id TEXT, to_addr TEXT,
        approval_thread TEXT, created_at INTEGER, status TEXT DEFAULT 'awaiting_approval')""")
    rows = conn.execute(
        "SELECT p.*, e.resume_pdf FROM postings p JOIN emails e USING(posting_id) "
        "WHERE p.status='ready' AND p.url LIKE '%news.ycombinator.com%' LIMIT ?",
        (limit,)).fetchall()
    done = []
    for r in rows:
        if conn.execute("SELECT 1 FROM email_apps WHERE posting_id=?", (r["posting_id"],)).fetchone():
            continue
        text = fetch_hn_text(r["url"])
        if not text:
            continue
        d = _claude(DRAFT_PROMPT.format(text=text[:3000]))
        if d and d.get("needs_manual"):
            # posting requires materials beyond a resume (video demo etc.):
            # never fake it; park for David with the reason (Tasklet lesson 2026-08-09)
            conn.execute("UPDATE postings SET status='manual', outcome='manual', last_error=? "
                         "WHERE posting_id=?",
                         (f"email app needs extra materials: {str(d.get('reason'))[:200]}",
                          r["posting_id"]))
            conn.commit()
            continue
        if not d or not d.get("body"):
            continue
        to_addr = (d.get("to") or "").strip()
        # find email in posting if Claude didn't
        if not to_addr:
            m = re.search(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-z]{2,}", text)
            to_addr = m.group(0) if m else ""
        if not to_addr:
            conn.execute("UPDATE postings SET status='manual' WHERE posting_id=?", (r["posting_id"],))
            conn.commit()
            continue
        # approval request to David (draft is NOT sent to company)
        pdf = Path(r["resume_pdf"])
        if not pdf.is_absolute():
            pdf = ROOT / pdf
        body = (f"EMAIL APPLICATION DRAFT — {r['company']}\n"
                f"Posting: {r['url']}\n\nTo: {to_addr}\nSubject: {d['subject']}\n\n"
                f"{d['body']}\n\n---\nReply 'send' to send now, 'skip' to drop, "
                "or edits in plain english. No reply in 3h -> auto-sent as-is.")
        resp = mailer.send(f"[jobhunt] email app: {r['company']}", body,
                           [pdf] if pdf.exists() else [])
        conn.execute("INSERT OR IGNORE INTO sent_messages VALUES (?)", (resp.get("id"),))
        conn.execute("INSERT INTO email_apps VALUES (?,?,?,?,?,'awaiting_approval')",
                     (r["posting_id"], "", to_addr, resp.get("threadId"), int(time.time())))
        conn.execute("UPDATE postings SET status='email_drafted' WHERE posting_id=?",
                     (r["posting_id"],))
        conn.commit()
        done.append(r["company"])
    conn.close()
    return done


def _send_draft(conn, r, thread) -> bool:
    """Send the drafted application email (reconstructed from the approval
    thread's first message) to the company. Returns True on success."""
    first = thread["messages"][0]
    body_text = mailer.extract_plain(first)
    m_sub = re.search(r"Subject: (.+)", body_text)
    m_body = re.search(r"Subject: .+?\n\n(.*?)\n\n---\nReply", body_text, re.S)
    if not (m_sub and m_body):
        return False
    pdf = Path(r["resume_pdf"])
    if not pdf.is_absolute():
        pdf = ROOT / pdf
    import base64
    from email.message import EmailMessage
    msg = EmailMessage()
    msg["To"] = r["to_addr"]
    msg["From"] = mailer.ME
    msg["Subject"] = m_sub.group(1).strip()
    msg.set_content(m_body.group(1).strip())
    if pdf.exists():
        msg.add_attachment(pdf.read_bytes(), maintype="application",
                           subtype="pdf", filename="David_Cui_Resume.pdf")
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    mailer._call("/messages/send", {"raw": raw})
    conn.execute("UPDATE email_apps SET status='sent' WHERE posting_id=?", (r["posting_id"],))
    conn.execute("UPDATE postings SET status='submitted' WHERE posting_id=?", (r["posting_id"],))
    conn.execute(
        "INSERT OR REPLACE INTO applications VALUES (?,?,?,?,?,?)",
        (r["posting_id"], str(pdf), "email", int(time.time()), f"emailed {r['to_addr']}", ""))
    return True


AUTO_SEND_VETO_SECONDS = 3 * 3600


def poll_approvals() -> list[str]:
    """Check approval threads. 'skip' drops, 'send' sends now; with no reply,
    the draft auto-sends after the veto window (business hours only)."""
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT ea.*, p.company, p.title, e.resume_pdf FROM email_apps ea "
            "JOIN postings p USING(posting_id) JOIN emails e USING(posting_id) "
            "WHERE ea.status='awaiting_approval'").fetchall()
    except sqlite3.OperationalError:
        return []
    sent_ids = {x[0] for x in conn.execute("SELECT message_id FROM sent_messages")}
    acted = []
    for r in rows:
        thread = mailer.get_thread(r["approval_thread"])
        replies = []
        for m in thread.get("messages", []):
            if m["id"] in sent_ids:
                continue
            if int(m.get("internalDate", 0)) // 1000 <= r["created_at"]:
                continue
            t = mailer.extract_plain(m)
            if t:
                replies.append((int(m["internalDate"]), t))
        if not replies:
            # FULL AUTO: no veto within window -> send as-is (business hours only,
            # so an overnight window still gives you the morning to object).
            import datetime
            from zoneinfo import ZoneInfo
            now = datetime.datetime.now(ZoneInfo("America/New_York"))
            if (time.time() - r["created_at"] >= AUTO_SEND_VETO_SECONDS
                    and 9 <= now.hour < 21 and _send_draft(conn, r, thread)):
                acted.append(f"AUTO-SENT {r['company']} -> {r['to_addr']}")
                conn.commit()
            continue
        replies.sort()
        text = replies[-1][1].strip()
        low = text.lower()
        if low.startswith("skip") or low.startswith("no"):
            conn.execute("UPDATE email_apps SET status='skipped' WHERE posting_id=?", (r["posting_id"],))
            conn.execute("UPDATE postings SET status='skipped' WHERE posting_id=?", (r["posting_id"],))
            acted.append(f"skipped {r['company']}")
        elif low.startswith("send") or low.startswith("yes") or low.startswith("approve"):
            if _send_draft(conn, r, thread):
                acted.append(f"SENT {r['company']} -> {r['to_addr']}")
        conn.commit()
    conn.close()
    return acted


if __name__ == "__main__":
    print("composed:", compose_ready_email_postings())
    print("approvals:", poll_approvals())
