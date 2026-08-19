"""Email-application composer for postings without an ATS (HN Who's Hiring, etc.).

Flow: posting marked 'ready' whose url is an HN item / mailto -> extract the
application email + instructions from the posting text -> Claude drafts a short
intro email -> send it directly to the company. Approval requests are disabled.
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
import track as _track  # noqa: E402  (track-based graduation, David 2026-08-19)

DB = ROOT / "out" / "tracker.db"
MODEL = "claude-sonnet-5"
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


def _runtime_path(stored_path: str, root: Path = ROOT) -> Path:
    path = Path(stored_path)
    if not path.is_absolute():
        return root / path
    if path.exists():
        return path
    if "out" in path.parts:
        candidate = root.joinpath(*path.parts[path.parts.index("out"):])
        if candidate.exists():
            return candidate
    return path


def _send_application(to_addr: str, subject: str, body: str, pdf: Path) -> dict:
    import base64
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["To"] = to_addr
    msg["From"] = mailer.ME
    msg["Subject"] = subject.strip()
    msg.set_content(body.strip())
    if pdf.exists():
        msg.add_attachment(pdf.read_bytes(), maintype="application",
                           subtype="pdf", filename="David_Cui_Resume.pdf")
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    return mailer._call("/messages/send", {"raw": raw})


def _record_sent(conn: sqlite3.Connection, posting_id: str, to_addr: str,
                 pdf: Path, message_id: str | None = None) -> None:
    if message_id:
        conn.execute("INSERT OR IGNORE INTO sent_messages VALUES (?)", (message_id,))
    changed = conn.execute(
        "UPDATE postings SET status='submitted' WHERE posting_id=? AND status='submitting'",
        (posting_id,),
    ).rowcount
    if changed != 1:
        raise RuntimeError("email submission claim lost after send; verify manually")
    conn.execute(
        "INSERT INTO applications VALUES (?,?,?,?,?,?)",
        (posting_id, str(pdf), "email", int(time.time()), f"emailed {to_addr}", ""))

DRAFT_PROMPT = """Draft a short application email for this posting. Candidate: David Cui,
Brown CS+Econ, expected {grad_date} (GPA 4.0), ex-YC founding CTO (Framewise Health), SWE intern at Freya (YC S25,
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
    conn.execute("CREATE TABLE IF NOT EXISTS sent_messages (message_id TEXT PRIMARY KEY)")
    rows = conn.execute(
        "SELECT p.*, e.resume_pdf FROM postings p JOIN emails e USING(posting_id) "
        "WHERE p.status='ready' AND p.url LIKE '%news.ycombinator.com%' "
        "AND NOT EXISTS (SELECT 1 FROM applications a WHERE a.posting_id=p.posting_id) "
        "AND NOT EXISTS (SELECT 1 FROM email_apps ea WHERE ea.posting_id=p.posting_id) "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM applications a2 JOIN postings p2 USING(posting_id) "
        "  WHERE lower(trim(p2.company))=lower(trim(p.company))"
        ") LIMIT ?",
        (limit,)).fetchall()
    done = []
    for r in rows:
        if conn.execute("SELECT 1 FROM email_apps WHERE posting_id=?", (r["posting_id"],)).fetchone():
            continue
        pdf = _runtime_path(r["resume_pdf"])
        import submit as submit_mod
        quality_ok, quality_reason = submit_mod._resume_quality_ready(
            pdf, r["posting_id"]
        )
        if not quality_ok:
            conn.execute(
                "UPDATE postings SET status='manual' WHERE posting_id=? AND status='ready'",
                (r["posting_id"],),
            )
            if "last_error" in {
                column[1] for column in conn.execute("PRAGMA table_info(postings)")
            }:
                conn.execute(
                    "UPDATE postings SET last_error=? WHERE posting_id=?",
                    (f"resume quality gate: {quality_reason}", r["posting_id"]),
                )
            conn.commit()
            continue
        text = fetch_hn_text(r["url"])
        if not text:
            continue
        d = _claude(DRAFT_PROMPT.format(
            text=text[:3000],
            grad_date=_track.grad_month_year(_track.infer_track(r["title"])),
        ))
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
        if not d.get("subject"):
            continue
        # Commit a durable point-of-no-return before calling Gmail. If the
        # process dies after Gmail accepts the message but before the ledger
        # commit, this row prevents an automatic duplicate send.
        try:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute(
                "SELECT 1 FROM applications WHERE posting_id=?",
                (r["posting_id"],),
            ).fetchone() or conn.execute(
                "SELECT 1 FROM email_apps WHERE posting_id=?",
                (r["posting_id"],),
            ).fetchone() or conn.execute(
                "SELECT 1 FROM applications a JOIN postings prior USING(posting_id) "
                "WHERE lower(trim(prior.company))=lower(trim(?)) LIMIT 1",
                (r["company"],),
            ).fetchone() or conn.execute(
                "SELECT 1 FROM postings WHERE posting_id<>? "
                "AND lower(trim(company))=lower(trim(?)) "
                "AND status IN ('submitting','sprinting') LIMIT 1",
                (r["posting_id"], r["company"]),
            ).fetchone():
                conn.rollback()
                continue
            changed = conn.execute(
                "UPDATE postings SET status='submitting' "
                "WHERE posting_id=? AND status='ready'",
                (r["posting_id"],),
            ).rowcount
            if changed != 1:
                conn.rollback()
                continue
            conn.execute(
                "INSERT INTO email_apps VALUES (?,?,?,?,?,'sending')",
                (r["posting_id"], None, to_addr, None, int(time.time())),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        try:
            resp = _send_application(to_addr, d["subject"], d["body"], pdf)
            conn.execute("BEGIN IMMEDIATE")
            _record_sent(conn, r["posting_id"], to_addr, pdf, resp.get("id"))
            conn.execute(
                "UPDATE email_apps SET draft_id=?, status='sent' WHERE posting_id=?",
                (resp.get("id", ""), r["posting_id"]),
            )
            conn.commit()
        except Exception as exc:
            conn.rollback()
            # Gmail/network failures after the request begins have an unknown
            # external outcome. Quarantine instead of risking a second email.
            conn.execute(
                "UPDATE email_apps SET status='uncertain' WHERE posting_id=?",
                (r["posting_id"],),
            )
            conn.execute(
                "UPDATE postings SET status='manual' WHERE posting_id=? AND status='submitting'",
                (r["posting_id"],),
            )
            if "last_error" in {
                row[1] for row in conn.execute("PRAGMA table_info(postings)")
            }:
                conn.execute(
                    "UPDATE postings SET last_error=? WHERE posting_id=?",
                    (f"email send uncertain: {type(exc).__name__}: {exc}"[:1000],
                     r["posting_id"]),
                )
            conn.commit()
            continue
        done.append(r["company"])
    conn.close()
    return done


def _send_draft(conn, r, thread) -> bool:
    """Drain a draft created by an older approval-based release."""
    first = thread["messages"][0]
    body_text = mailer.extract_plain(first)
    m_sub = re.search(r"Subject: (.+)", body_text)
    m_body = re.search(r"Subject: .+?\n\n(.*?)\n\n---\nReply", body_text, re.S)
    if not (m_sub and m_body):
        return False
    pdf = _runtime_path(r["resume_pdf"])
    try:
        conn.execute("BEGIN IMMEDIATE")
        posting_claimed = conn.execute(
            "UPDATE postings SET status='submitting' "
            "WHERE posting_id=? AND status='ready'",
            (r["posting_id"],),
        ).rowcount
        email_claimed = conn.execute(
            "UPDATE email_apps SET status='sending' "
            "WHERE posting_id=? AND status='awaiting_approval'",
            (r["posting_id"],),
        ).rowcount
        if posting_claimed != 1 or email_claimed != 1:
            conn.rollback()
            return False
        conn.commit()

        resp = _send_application(r["to_addr"], m_sub.group(1), m_body.group(1), pdf)
        conn.execute("BEGIN IMMEDIATE")
        _record_sent(conn, r["posting_id"], r["to_addr"], pdf, resp.get("id"))
        conn.execute(
            "UPDATE email_apps SET draft_id=?, status='sent' WHERE posting_id=?",
            (resp.get("id", ""), r["posting_id"]),
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        conn.execute(
            "UPDATE email_apps SET status='uncertain' WHERE posting_id=?",
            (r["posting_id"],),
        )
        conn.execute(
            "UPDATE postings SET status='manual' "
            "WHERE posting_id=? AND status='submitting'",
            (r["posting_id"],),
        )
        conn.commit()
        return False


def poll_approvals() -> list[str]:
    """Drain approval threads created before approval requests were disabled."""
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
            if _send_draft(conn, r, thread):
                acted.append(f"LEGACY-SENT {r['company']} -> {r['to_addr']}")
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
