"""Batch: take queued postings, tailor resumes, and queue them for submission."""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT / "apply"), str(ROOT / "tailor"), str(ROOT / "notify")]

from jd import fetch_jd  # noqa: E402
from tailor import tailor  # noqa: E402

DB = ROOT / "out" / "tracker.db"


def ensure_email_table(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS emails (
        posting_id TEXT PRIMARY KEY REFERENCES postings(posting_id),
        thread_id TEXT, message_id TEXT, resume_pdf TEXT, resume_tex TEXT,
        sent_at INTEGER, revision INTEGER DEFAULT 0)""")
    conn.execute("CREATE TABLE IF NOT EXISTS sent_messages (message_id TEXT PRIMARY KEY)")
    conn.commit()


def run_batch(limit: int = 5) -> list[str]:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    ensure_email_table(conn)
    rows = conn.execute(
        "SELECT * FROM postings WHERE status='queued' ORDER BY first_seen LIMIT ?", (limit,)
    ).fetchall()
    done = []
    for r in rows:
        company, title, url = r["company"], r["title"], r["url"]
        print(f"processing: {company} - {title}")
        jd_text = fetch_jd(url)
        pdf = tailor(r["posting_id"], company, title, jd_text)
        if pdf is None:
            print("  ! tailor+fallback failed, skipping")
            continue
        tex = pdf.with_suffix(".tex")
        conn.execute(
            "INSERT OR REPLACE INTO emails VALUES (?,?,?,?,?,?,0)",
            (r["posting_id"], None, None,
             str(pdf), str(tex), int(time.time())),
        )
        conn.execute("UPDATE postings SET status='ready' WHERE posting_id=?",
                     (r["posting_id"],))
        conn.commit()
        done.append(f"{company} — {title}")
    conn.close()
    return done


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    sent = run_batch(n)
    print(f"\nprepared {len(sent)} applications:")
    for s in sent:
        print(" ", s)
