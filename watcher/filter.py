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


def _phrase_re(phrase: str) -> re.Pattern:
    """Word-boundary matcher for a keyword phrase.

    The old substring check made 'ai' match 'maintenance' and 'ml' match
    'html' (David 2026-08-19). Words must appear whole, in order, separated
    by any non-alphanumeric run: 'full stack' matches 'Full-Stack Engineer'.
    Phrases ending in digits stay prefixes ('fall 20' matches 'Fall 2027').
    """
    words = re.findall(r"[a-z0-9']+", phrase.lower())
    body = r"[^a-z0-9]+".join(re.escape(w) for w in words)
    tail = r"" if words and words[-1].isdigit() else r"(?![a-z0-9])"
    return re.compile(r"(?<![a-z0-9])" + body + tail)


INCLUDE_RES = [_phrase_re(k) for k in INCLUDE]
EXCLUDE_RES = [_phrase_re(k) for k in EXCLUDE]

# Titles that clearly aren't in scope even though they sneak into SWE lists.
# Regexes (not yaml phrases) so stems like 'actuar' can match 'actuarial'.
# Hardware stays out (David 2026-08-19) but embedded/firmware SOFTWARE is in,
# so 'electrical/mechanical engineer' are excluded while 'embedded software
# engineer' and 'firmware engineer' pass.
HARD_EXCLUDE_RES = [re.compile(p) for p in (
    r"\bmechanical\b", r"\bcivil engineer", r"\belectrical engineer",
    r"\bchemical engineer", r"\bhardware engineer", r"\baccounting\b",
    r"\btax\b", r"\baudit", r"\bhr intern", r"\bmarketing\b", r"\bsales intern",
    r"\bsupply chain", r"\bfinance intern", r"\bactuar", r"\bphd\b",
    r"\bdoctoral\b", r"\bdoctorate\b", r"\bmaster'?s degree\b",
    r"\bmaster['\u2019]s\b", r"\bms/phd\b", r"\bmba intern",
    # non-software 'engineering intern' variants (2026-08-19 requeue audit:
    # Bridge/Project/GTM Engineering Intern slipped through the generic
    # 'engineering intern' include)
    r"\bgtm\b", r"\bbridge engineer", r"\bproject engineer",
    r"\bmanufacturing engineer", r"\bindustrial engineer",
    r"\bprocess engineer", r"\bstructural engineer", r"\bfacilities\b",
    r"\bquality engineer", r"\btest technician",
)]


def _title_text(title: str) -> str:
    """Markdown-link titles ('[X](url)') match on X only.

    dreamwork rows store the whole markdown link; the pre-2026-08-19 substring
    filter matched 'ai' inside the URL's 'utm_campaign', queueing Security/
    Bridge/GTM junk for weeks. Never match against URL text.
    """
    m = re.match(r"\s*\[([^\]]+)\]\(", title or "")
    return (m.group(1) if m else title or "").lower()


def title_ok(title: str, source: str = "") -> bool:
    t = _title_text(title)
    if any(r.search(t) for r in HARD_EXCLUDE_RES):
        return False
    exclude = EXCLUDE_RES
    if source in ("waas", "abc", "bigco"):
        # Startup sources (David 2026-08-17): full-time roles are wanted too
        # at YC and Series A-D startups. Bigco is pre-filtered to intern/new-grad.
        exclude = [r for r in exclude if r.pattern != _phrase_re("new grad").pattern]
    if any(r.search(t) for r in exclude):
        return False
    if source in ("waas", "abc") and re.search(
            r"\b(founding|software|engineer|swe|ml|ai)\b|product manage|product intern\b", t):
        return True
    return any(r.search(t) for r in INCLUDE_RES)


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
        elif not title_ok(r["title"], source=r["source"] if "source" in r.keys() else ""):
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
