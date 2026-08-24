"""Filter: decides which tracked postings are worth applying to, per profile rules.

Rules from profile.yaml:
  - roles_include / roles_exclude keyword match on title
  - skip closed postings
  - apply to every distinct canonical posting, including multiple roles at one company
  - re-evaluate current source rows when the filter policy changes
"""
from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from submission.identity import canonical_posting_key

DB_PATH = ROOT / "out" / "tracker.db"
PROFILE = yaml.safe_load((ROOT / "profile" / "profile.yaml").read_text())

INCLUDE = [k.lower() for k in PROFILE["preferences"]["roles_include"]]
EXCLUDE = [k.lower() for k in PROFILE["preferences"]["roles_exclude"]]
EXCLUDE_COMPANIES = {c.lower() for c in PROFILE["preferences"]["exclude_companies"]}
FILTER_REVISION = "filter-v3-summer-winter-multi-posting"


def _phrase_re(phrase: str) -> re.Pattern:
    """Word-boundary matcher for a keyword phrase.

    The old substring check made 'ai' match 'maintenance' and 'ml' match
    'html' (David 2026-08-19). Words must appear whole, in order, separated
    by any non-alphanumeric run: 'full stack' matches 'Full-Stack Engineer'.
    Phrases ending in digits stay prefixes ('fall 20' matches 'Fall 2027').
    """
    words = re.findall(r"[a-z0-9']+", phrase.lower())
    body = r"[^a-z0-9]+".join(re.escape(w) for w in words)
    # plural-tolerant tail: 'software engineer' matches 'Software Engineers'.
    # Digit-tailed phrases stay prefixes ('fall 20' matches 'Fall 2027').
    tail = r"" if words and words[-1].isdigit() else r"s?(?![a-z0-9])"
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
    r"(?<![a-z])ph\.?d\.?(?![a-z])",
    r"\bdoctoral\b", r"\bdoctorate\b", r"\bmaster'?s degree\b",
    r"\bmaster['\u2019]s\b", r"\bms/phd\b", r"\bmba intern",
    # non-software 'engineering intern' variants (2026-08-19 requeue audit:
    # Bridge/Project/GTM Engineering Intern slipped through the generic
    # 'engineering intern' include)
    r"\bgtm\b", r"\bbridge engineer", r"\bproject engineer",
    r"\bmanufacturing engineer", r"\bindustrial engineer",
    r"\bprocess engineer", r"\bstructural engineer", r"\bfacilities\b",
    r"\bquality engineer", r"\btest technician",
    r"\bfpga\b", r"\bengineering intern\b.{0,30}\bcivil\b",
    r"\btransducer\b", r"\bpropulsion test\b", r"\bdc design\b",
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
    if re.search(r"\b20\d{2}\b", t) and not re.search(r"\b2027\b", t):
        return False
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
            # startup titles get a slightly looser gate, but 'engineer' alone
            # is NOT enough: the backfilled A-D pool includes aerospace/
            # hardware startups whose Mission/Controls/Guidance Engineers all
            # matched the old bare-\bengineer\b shortcut (2026-08-19).
            r"\b(founding|software|swe|ml|ai)\b|product manage|product intern\b", t):
        return True
    return any(r.search(t) for r in INCLUDE_RES)


def run(
    verbose: bool = False,
    current_posting_ids: set[str] | None = None,
) -> dict:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    current_posting_ids = current_posting_ids or set()
    rows = conn.execute(
        "SELECT * FROM postings WHERE status IN ('new','filtered_out')"
    ).fetchall()
    reserved_canonical = {
        canonical_posting_key(row["posting_id"], row["url"] or "")
        for row in conn.execute(
            "SELECT posting_id,url FROM postings "
            "WHERE status NOT IN ('new','filtered_out') "
            "OR (status='filtered_out' AND outcome IS NOT NULL)"
        )
    }
    queued, filtered = 0, 0
    for r in rows:
        if r["status"] == "filtered_out":
            if r["posting_id"] not in current_posting_ids or r["outcome"] is not None:
                continue
            if (r["last_error"] or "").startswith(f"{FILTER_REVISION}:"):
                continue
        canonical_key = canonical_posting_key(r["posting_id"], r["url"] or "")
        reason = None
        if r["closed"]:
            reason = "closed"
        elif r["company"].lower() in EXCLUDE_COMPANIES:
            reason = "excluded company"
        elif not title_ok(r["title"], source=r["source"] if "source" in r.keys() else ""):
            reason = "title mismatch"
        elif canonical_key in reserved_canonical:
            reason = "canonical posting already tracked"
        if reason:
            conn.execute(
                "UPDATE postings SET status='filtered_out', last_error=? WHERE posting_id=?",
                (f"{FILTER_REVISION}:{reason}", r["posting_id"]),
            )
            filtered += 1
            if verbose:
                print(f"  SKIP ({reason}): {r['company']} - {r['title']}")
        else:
            conn.execute(
                "UPDATE postings SET status='queued', outcome=NULL, last_error=NULL WHERE posting_id=?",
                (r["posting_id"],),
            )
            reserved_canonical.add(canonical_key)
            queued += 1
            if verbose:
                print(f"  QUEUE: {r['company']} - {r['title']}")
    conn.commit()
    out = {"queued": queued, "filtered_out": filtered}
    conn.close()
    return out


if __name__ == "__main__":
    print(run(verbose="-v" in sys.argv))
