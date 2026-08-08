#!/usr/bin/env python3
"""Re-tailor ONE pending posting through the deep pipeline (plan->write->critique).
Usage: retailor_one.py <posting_id>. Reads company/title/url from tracker.db,
writes the regenerated PDF over the existing resume path. No DB status changes."""
import sys
import shutil
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT / "tailor"), str(ROOT / "apply"), str(ROOT)]

import tailor  # noqa: E402
from jd import fetch_jd  # noqa: E402


def main() -> int:
    pid = sys.argv[1]
    conn = sqlite3.connect(ROOT / "out" / "tracker.db")
    conn.row_factory = sqlite3.Row
    r = conn.execute(
        "SELECT p.company, p.title, p.url, e.resume_pdf, e.resume_tex "
        "FROM postings p JOIN emails e USING(posting_id) WHERE p.posting_id=?",
        (pid,),
    ).fetchone()
    conn.close()
    if not r:
        print(f"SKIP {pid}: no row")
        return 0
    try:
        jd = fetch_jd(r["url"]) or ""
    except Exception:
        jd = ""
    if len(jd) < 300:
        print(f"SKIP {r['company']}: jd too thin ({len(jd)} chars), keeping existing")
        return 0
    out = tailor.tailor(pid, r["company"], r["title"], jd)
    if not out:
        print(f"FAIL {r['company']}: tailor returned None, existing PDF kept")
        return 1
    # tailor() derives its own safe filename; sync onto the paths the emails row
    # (and therefore submit.py) actually uses.
    want_pdf, want_tex = Path(r["resume_pdf"]), Path(r["resume_tex"])
    if out != want_pdf:
        shutil.copyfile(out, want_pdf)
        shutil.copyfile(out.with_suffix(".tex"), want_tex)
    print(f"OK {r['company']}: retailored ({len(jd)} char JD)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
