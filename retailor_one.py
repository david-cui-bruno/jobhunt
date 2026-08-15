#!/usr/bin/env python3
"""Re-tailor ONE pending posting through the deep pipeline (plan->write->critique).
Usage: retailor_one.py <posting_id>. Reads company/title/url from tracker.db and
publishes a new immutable artifact only while the posting remains manual/failed."""
import os
import sys
import sqlite3
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT / "tailor"), str(ROOT / "apply"), str(ROOT)]

import tailor  # noqa: E402
import submit  # noqa: E402
from jd import fetch_jd  # noqa: E402


def main() -> int:
    pid = sys.argv[1]
    conn = sqlite3.connect(ROOT / "out" / "tracker.db")
    conn.row_factory = sqlite3.Row
    r = conn.execute(
        "SELECT p.company, p.title, p.url, p.status, e.resume_pdf, e.resume_tex "
        "FROM postings p JOIN emails e USING(posting_id) WHERE p.posting_id=?",
        (pid,),
    ).fetchone()
    if not r:
        conn.close()
        print(f"SKIP {pid}: no row")
        return 0
    if r["status"] not in {"manual", "failed"}:
        conn.close()
        print(f"SKIP {pid}: status {r['status']} is not safe to retailor")
        return 0
    try:
        jd = fetch_jd(r["url"]) or ""
    except Exception:
        jd = ""
    if len(jd) < 300:
        print(f"SKIP {r['company']}: jd too thin ({len(jd)} chars), keeping existing")
        return 0
    original_out_dir = tailor.OUT_DIR
    with tempfile.TemporaryDirectory(prefix="jobhunt-retailor-") as tmp:
        tailor.OUT_DIR = Path(tmp)
        try:
            out = tailor.tailor(pid, r["company"], r["title"], jd)
        finally:
            tailor.OUT_DIR = original_out_dir
        if not out:
            conn.close()
            print(f"FAIL {r['company']}: tailor returned None, existing PDF kept")
            return 1

        # Publish under a new name. Never overwrite the path an already-started
        # submit worker may have opened.
        destination_dir = ROOT / "out" / "resumes"
        destination_dir.mkdir(parents=True, exist_ok=True)
        stem = f"{out.stem}_retailored_{time.time_ns()}"
        published = {}
        for suffix in (".pdf", ".tex", ".quality.json", ".plan.txt"):
            source = out.with_suffix(suffix)
            if source.is_file():
                destination = destination_dir / f"{stem}{suffix}"
                os.replace(source, destination)
                published[suffix] = destination
        new_pdf = published.get(".pdf")
        new_tex = published.get(".tex")
        if not new_pdf or not new_tex:
            conn.close()
            print(f"FAIL {r['company']}: incomplete generated artifact, existing PDF kept")
            return 1
        quality_ok, quality_reason = submit._resume_quality_ready(new_pdf, pid)
        if not quality_ok:
            conn.close()
            print(f"FAIL {r['company']}: resume quality gate: {quality_reason}")
            return 1

    try:
        conn.execute("BEGIN IMMEDIATE")
        safe = conn.execute(
            "SELECT 1 FROM postings p WHERE p.posting_id=? "
            "AND p.status IN ('manual','failed') "
            "AND NOT EXISTS (SELECT 1 FROM applications a WHERE a.posting_id=p.posting_id)",
            (pid,),
        ).fetchone()
        if not safe:
            conn.rollback()
            print(f"SKIP {r['company']}: posting state changed; new artifact not attached")
            return 0
        changed = conn.execute(
            "UPDATE emails SET resume_pdf=?, resume_tex=? WHERE posting_id=?",
            (str(new_pdf), str(new_tex), pid),
        ).rowcount
        if changed != 1:
            raise RuntimeError("resume path update lost")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    print(f"OK {r['company']}: retailored ({len(jd)} char JD)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
