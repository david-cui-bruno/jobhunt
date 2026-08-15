"""Poll legacy review threads without allowing freeform resume rewrites.

Reply handling:
  - 'approve' / 'lgtm' / 'looks good' -> status 'ready'
  - 'skip' / 'pass'                   -> status 'skipped'
  - anything else                     -> status 'manual'; canonical update required
"""
from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "notify"))

import mailer  # noqa: E402

DB = ROOT / "out" / "tracker.db"


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


def classify(reply: str) -> str:
    r = reply.strip().lower()
    if re.fullmatch(r"(approve[d]?|lgtm|looks good\.?|ship it|yes)[\s!.]*", r):
        return "approve"
    if re.fullmatch(r"(skip|pass|no|reject)[\s!.]*", r):
        return "skip"
    return "manual_review"


def poll_once(verbose: bool = True) -> dict:
    """Apply terminal review decisions and fail closed on rewrite requests."""
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT e.*, p.company, p.title, p.status FROM emails e "
        "JOIN postings p USING(posting_id) WHERE p.status IN ('tailored') "
        "AND e.thread_id IS NOT NULL AND e.thread_id != ''"
    ).fetchall()
    sent_ids = {x[0] for x in conn.execute("SELECT message_id FROM sent_messages")}
    actions = {"approved": [], "skipped": [], "manual_review": []}
    for row in rows:
        thread = mailer.get_thread(row["thread_id"])
        replies = []
        for message in thread.get("messages", []):
            if message["id"] in sent_ids:
                continue
            internal_ts = int(message.get("internalDate", 0)) // 1000
            if internal_ts <= row["sent_at"]:
                continue
            body_text = mailer.extract_plain(message)
            if body_text:
                replies.append((internal_ts, body_text, message["id"]))
        if not replies:
            continue
        replies.sort()
        _timestamp, text, _message_id = replies[-1]
        kind = classify(text)
        name = f"{row['company']} — {row['title']}"
        if kind == "approve":
            conn.execute(
                "UPDATE postings SET status='ready' "
                "WHERE posting_id=? AND status='tailored'",
                (row["posting_id"],),
            )
            actions["approved"].append(name)
        elif kind == "skip":
            conn.execute(
                "UPDATE postings SET status='skipped' "
                "WHERE posting_id=? AND status='tailored'",
                (row["posting_id"],),
            )
            actions["skipped"].append(name)
        else:
            if verbose:
                print(
                    f"manual resume source update required for {name}: {text[:100]!r}"
                )
            conn.execute(
                "UPDATE postings SET status='manual', "
                "last_error='resume revision requested; update canonical template manually' "
                "WHERE posting_id=? AND status='tailored'",
                (row["posting_id"],),
            )
            actions["manual_review"].append(name)
        conn.commit()
    conn.close()
    return actions


if __name__ == "__main__":
    print(poll_once())
