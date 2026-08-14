"""Filter: decides which tracked postings are worth applying to, per profile rules.

Rules from profile.yaml:
  - roles_include / roles_exclude keyword match on title
  - skip closed postings
  - one application per company (intern vs new grad conflict -> prefer intern)
  - dedupe near-identical titles at same company (keep first)
"""
from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "out" / "tracker.db"
PROFILE = yaml.safe_load((ROOT / "profile" / "profile.yaml").read_text())

INCLUDE = [k.lower() for k in PROFILE["preferences"]["roles_include"]]
EXCLUDE = [k.lower() for k in PROFILE["preferences"]["roles_exclude"]]
EXCLUDE_COMPANIES = {c.lower() for c in PROFILE["preferences"]["exclude_companies"]}

# Titles that clearly aren't SWE/ML even though they sneak into SWE lists
HARD_EXCLUDE = [
    "mechanical", "civil engineer", "electrical engineer", "chemical engineer",
    "accounting", "tax ", "audit", "hr intern", "marketing", "sales intern",
    "supply chain", "finance intern", "actuar", "phd", "doctoral", "doctorate",
    "master's", "master’s", "masters degree", "ms/phd", "mba intern",
]


def title_ok(title: str) -> bool:
    t = title.lower()
    if any(k in t for k in EXCLUDE + HARD_EXCLUDE):
        return False
    return any(k in t for k in INCLUDE)


def run(verbose: bool = False) -> dict:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM postings WHERE status='new'").fetchall()
    queued, filtered = 0, 0
    for r in rows:
        reason = None
        if r["closed"]:
            reason = "closed"
        elif r["company"].lower() in EXCLUDE_COMPANIES:
            reason = "excluded company"
        elif not title_ok(r["title"]):
            reason = "title mismatch"
        else:
            # one-app-per-company: any other posting already queued or beyond?
            # (David 2026-08-08: never intern + full-time at the same company.
            # When the new posting is an INTERN role and the one in the pipeline
            # is a not-yet-submitted full-time role, swap: intern wins.)
            dup = conn.execute(
                "SELECT posting_id, title, status FROM postings WHERE lower(company)=lower(?) "
                "AND status IN ('queued','tailoring','sprinting','submitting','tailored','ready','manual','failed','submitted') LIMIT 1",
                (r["company"],),
            ).fetchone()
            if dup:
                new_is_intern = bool(re.search(r"\bintern|co[- ]?op\b", r["title"], re.I))
                old_is_intern = bool(re.search(r"\bintern|co[- ]?op\b", dup["title"], re.I))
                if (new_is_intern and not old_is_intern
                        and dup["status"] not in {"submitted", "tailoring", "sprinting", "submitting"}):
                    conn.execute(
                        "UPDATE postings SET status='filtered_out' WHERE posting_id=?",
                        (dup["posting_id"],))
                    if verbose:
                        print(f"  SWAP: intern beats full-time at {r['company']} ({dup['title']})")
                else:
                    reason = "company already in pipeline"
        if reason:
            conn.execute("UPDATE postings SET status='filtered_out' WHERE posting_id=?",
                         (r["posting_id"],))
            filtered += 1
            if verbose:
                print(f"  SKIP ({reason}): {r['company']} - {r['title']}")
        else:
            conn.execute("UPDATE postings SET status='queued' WHERE posting_id=?",
                         (r["posting_id"],))
            queued += 1
            if verbose:
                print(f"  QUEUE: {r['company']} - {r['title']}")
    conn.commit()
    out = {"queued": queued, "filtered_out": filtered}
    conn.close()
    return out


if __name__ == "__main__":
    print(run(verbose="-v" in sys.argv))
