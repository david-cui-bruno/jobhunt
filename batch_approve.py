"""Batch approval: one daily email listing ALL pending items with numbered
actions; a single reply drives everything.

Reply grammar (case-insensitive, one command per line or comma-separated):
  approve 1,3,5-9        approve those items
  skip 2,4               drop those items
  approve all            approve everything listed
  skip all               drop everything listed
  4: make it more ML focused   -> revision instruction for item 4

Items covered: tailored resumes awaiting approval, email-app drafts, essay
drafts (flagged), WaaS notes. The reply is parsed by poll_batch_replies().
"""
from __future__ import annotations

import re
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT / "notify")]
import mailer  # noqa: E402

DB = ROOT / "out" / "tracker.db"


def ensure_tables(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS batch_emails (
        batch_id INTEGER PRIMARY KEY AUTOINCREMENT, thread_id TEXT, message_id TEXT,
        sent_at INTEGER, item_map TEXT);
    """)
    conn.commit()


def collect_pending(conn) -> list[dict]:
    items = []
    for r in conn.execute(
            "SELECT p.posting_id, p.company, p.title, p.url, e.sent_at FROM postings p "
            "JOIN emails e USING(posting_id) WHERE p.status='tailored' ORDER BY e.sent_at"):
        items.append({"kind": "resume", "posting_id": r[0], "company": r[1],
                      "title": r[2], "url": r[3]})
    try:
        for r in conn.execute(
                "SELECT ea.posting_id, p.company, p.title, ea.to_addr FROM email_apps ea "
                "JOIN postings p USING(posting_id) WHERE ea.status='awaiting_approval'"):
            items.append({"kind": "email_app", "posting_id": r[0], "company": r[1],
                          "title": r[2], "url": f"to: {r[3]}"})
    except sqlite3.OperationalError:
        pass
    return items


def send_batch() -> int:
    conn = sqlite3.connect(DB)
    ensure_tables(conn)
    items = collect_pending(conn)
    if not items:
        return 0
    import json
    lines = []
    for i, it in enumerate(items, 1):
        tag = {"resume": "RESUME", "email_app": "EMAIL-APP"}[it["kind"]]
        lines.append(f"{i}. [{tag}] {it['company']} — {it['title']}\n   {it['url']}")
    body = (
        f"{len(items)} items pending approval. Reply with commands, e.g.:\n"
        "  approve 1,3,5-7\n  skip 2\n  approve all\n  4: lead with the ML project\n\n"
        + "\n\n".join(lines)
        + "\n\n(Resumes: 'approve' -> submit queue. Email apps: 'approve' -> email sent to company.)"
    )
    resp = mailer.send(f"[jobhunt] BATCH APPROVAL — {len(items)} pending", body)
    conn.execute("INSERT OR IGNORE INTO sent_messages VALUES (?)", (resp.get("id"),))
    conn.execute("INSERT INTO batch_emails (thread_id, message_id, sent_at, item_map) VALUES (?,?,?,?)",
                 (resp.get("threadId"), resp.get("id"), int(time.time()),
                  json.dumps([{ "kind": it["kind"], "posting_id": it["posting_id"]} for it in items])))
    conn.commit()
    conn.close()
    return len(items)


def _parse_ranges(s: str) -> set[int]:
    out = set()
    for part in re.split(r"[,\s]+", s.strip()):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)-(\d+)", part)
        if m:
            out.update(range(int(m.group(1)), int(m.group(2)) + 1))
        elif part.isdigit():
            out.add(int(part))
    return out


def poll_batch_replies(verbose: bool = True) -> dict:
    import json
    conn = sqlite3.connect(DB)
    ensure_tables(conn)
    sent_ids = {x[0] for x in conn.execute("SELECT message_id FROM sent_messages")}
    acted = {"approved": 0, "skipped": 0, "revise": 0}
    for batch_id, thread_id, sent_at, item_map in conn.execute(
            "SELECT batch_id, thread_id, sent_at, item_map FROM batch_emails "
            "WHERE sent_at > ?", (int(time.time()) - 7 * 86400,)).fetchall():
        items = json.loads(item_map)
        thread = mailer.get_thread(thread_id)
        for m in thread.get("messages", []):
            if m["id"] in sent_ids:
                continue
            if int(m.get("internalDate", 0)) // 1000 <= sent_at:
                continue
            if conn.execute("SELECT 1 FROM sent_messages WHERE message_id=?", (m["id"],)).fetchone():
                continue
            text = mailer.extract_plain(m)
            if not text:
                continue
            n = len(items)
            approve_idx, skip_idx, revisions = set(), set(), {}
            for line in text.splitlines():
                l = line.strip()
                if not l:
                    continue
                low = l.lower()
                if low.startswith("approve all"):
                    approve_idx = set(range(1, n + 1))
                elif low.startswith("skip all"):
                    skip_idx = set(range(1, n + 1))
                elif low.startswith("approve"):
                    approve_idx |= _parse_ranges(l[7:])
                elif low.startswith("skip"):
                    skip_idx |= _parse_ranges(l[4:])
                else:
                    mm = re.match(r"(\d+)\s*[:\-]\s*(.+)", l)
                    if mm:
                        revisions[int(mm.group(1))] = mm.group(2)
            for i in sorted(approve_idx - skip_idx):
                if 1 <= i <= n:
                    it = items[i - 1]
                    if it["kind"] == "resume":
                        conn.execute("UPDATE postings SET status='ready' WHERE posting_id=? AND status='tailored'",
                                     (it["posting_id"],))
                    elif it["kind"] == "email_app":
                        # mark for send by email_apply.poll_approvals path
                        conn.execute("UPDATE email_apps SET status='approved_batch' WHERE posting_id=?",
                                     (it["posting_id"],))
                    acted["approved"] += 1
            for i in sorted(skip_idx):
                if 1 <= i <= n:
                    it = items[i - 1]
                    conn.execute("UPDATE postings SET status='skipped' WHERE posting_id=?", (it["posting_id"],))
                    if it["kind"] == "email_app":
                        conn.execute("UPDATE email_apps SET status='skipped' WHERE posting_id=?",
                                     (it["posting_id"],))
                    acted["skipped"] += 1
            for i, instr in revisions.items():
                if 1 <= i <= len(items):
                    acted["revise"] += 1
                    if verbose:
                        print(f"[batch] revision for item {i}: {instr[:60]} (handled by revise.py on its thread)")
            # mark reply consumed
            conn.execute("INSERT OR IGNORE INTO sent_messages VALUES (?)", (m["id"],))
            conn.commit()
    conn.close()
    return acted


if __name__ == "__main__":
    if "--send" in sys.argv:
        print("items:", send_batch())
    print("replies:", poll_batch_replies())
