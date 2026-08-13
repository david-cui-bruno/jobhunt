"""Revise: poll email threads for replies; apply suggestions, approve, or skip.

Reply handling:
  - 'approve' / 'lgtm' / 'looks good'  -> status 'ready'
  - 'skip' / 'pass'                    -> status 'skipped'
  - anything else -> treated as revision instructions for Claude; new PDF replied on thread
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT / "tailor"), str(ROOT / "notify")]

from tailor import validate, compile_pdf, sanitize, MODEL, API_KEY  # noqa: E402
import mailer  # noqa: E402

DB = ROOT / "out" / "tracker.db"

REVISE_PROMPT = """You are revising a tailored LaTeX resume based on the owner's feedback.

RULES: reword/reorder existing truthful content only; never invent new employers, metrics, or skills unless the feedback explicitly supplies the factual content. Keep structure and one-page length. Work-experience employers stay in this fixed order: Framewise Health, Freya, Sotatek. Arrows must be $\\rightarrow$ (never plain ->) and approximations $\\sim$ (never bare ~). Output must compile.

OWNER FEEDBACK:
{feedback}

CURRENT LATEX:
{tex}

Return ONLY the complete revised LaTeX source, no commentary, no markdown fences."""


def _runtime_path(stored_path: str, root: Path = ROOT) -> Path:
    """Relocate a project file whose DB path came from another machine."""
    path = Path(stored_path)
    if not path.is_absolute():
        return root / path
    if path.exists():
        return path
    for marker in ("out", "resume"):
        if marker in path.parts:
            candidate = root.joinpath(*path.parts[path.parts.index(marker):])
            if candidate.exists():
                return candidate
    return path


def call_claude_revise(tex: str, feedback: str) -> str:
    body = json.dumps({
        "model": MODEL, "max_tokens": 8000,
        "messages": [{"role": "user",
                      "content": REVISE_PROMPT.format(feedback=feedback[:4000], tex=tex)}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body,
        headers={"x-api-key": API_KEY, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        resp = json.load(r)
    text = "".join(b.get("text", "") for b in resp["content"] if b.get("type") == "text")
    return re.sub(r"^```(?:latex|tex)?\s*|\s*```$", "", text.strip())


def classify(reply: str) -> str:
    r = reply.strip().lower()
    if re.fullmatch(r"(approve[d]?|lgtm|looks good\.?|ship it|yes)[\s!.]*", r):
        return "approve"
    if re.fullmatch(r"(skip|pass|no|reject)[\s!.]*", r):
        return "skip"
    return "revise"


def poll_once(verbose: bool = True) -> dict:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT e.*, p.company, p.title, p.status FROM emails e "
        "JOIN postings p USING(posting_id) WHERE p.status IN ('tailored') "
        "AND e.thread_id IS NOT NULL AND e.thread_id != ''"
    ).fetchall()
    sent_ids = {x[0] for x in conn.execute("SELECT message_id FROM sent_messages")}
    actions = {"approved": [], "skipped": [], "revised": []}
    for r in rows:
        thread = mailer.get_thread(r["thread_id"])
        msgs = thread.get("messages", [])
        # user replies = messages we did not send, newer than our last send
        replies = []
        for m in msgs:
            if m["id"] in sent_ids:
                continue
            internal_ts = int(m.get("internalDate", 0)) // 1000
            if internal_ts <= r["sent_at"]:
                continue
            body_text = mailer.extract_plain(m)
            if body_text:
                replies.append((internal_ts, body_text, m["id"]))
        if not replies:
            continue
        replies.sort()
        ts, text, mid = replies[-1]
        kind = classify(text)
        name = f"{r['company']} — {r['title']}"
        if kind == "approve":
            conn.execute("UPDATE postings SET status='ready' WHERE posting_id=?",
                         (r["posting_id"],))
            actions["approved"].append(name)
        elif kind == "skip":
            conn.execute("UPDATE postings SET status='skipped' WHERE posting_id=?",
                         (r["posting_id"],))
            actions["skipped"].append(name)
        else:
            if verbose:
                print(f"revising {name} per: {text[:100]!r}")
            tex_path = _runtime_path(r["resume_tex"])
            tex = tex_path.read_text()
            new_tex = sanitize(call_claude_revise(tex, text))
            pdf_path = _runtime_path(r["resume_pdf"])
            rev = r["revision"] + 1
            validation_errors = []
            if validate(new_tex, validation_errors):
                if compile_pdf(new_tex, pdf_path):
                    tex_path.write_text(new_tex)
                    resp = mailer.send(
                        f"[jobhunt] {r['company']} — {r['title']}",
                        f"Revision {rev} attached, incorporating: {text[:300]}\n\n"
                        "Reply 'approve', 'skip', or more suggestions.",
                        [pdf_path], thread_id=r["thread_id"])
                    conn.execute("INSERT OR IGNORE INTO sent_messages VALUES (?)",
                                 (resp.get("id"),))
                    conn.execute(
                        "UPDATE emails SET revision=?, sent_at=strftime('%s','now') "
                        "WHERE posting_id=?", (rev, r["posting_id"]))
                    actions["revised"].append(name)
                else:
                    resp = mailer.send(
                        f"[jobhunt] {r['company']} — {r['title']}",
                        "That revision failed to compile; kept the previous PDF. "
                        "Try different wording?", [], thread_id=r["thread_id"])
                    conn.execute("INSERT OR IGNORE INTO sent_messages VALUES (?)",
                                 (resp.get("id"),))
            elif verbose:
                print(f"revision rejected for {name}: {', '.join(validation_errors)}")
        conn.commit()
    conn.close()
    return actions


if __name__ == "__main__":
    print(poll_once())
