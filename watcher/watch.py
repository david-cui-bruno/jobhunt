"""Watcher: polls Summer 2027 listing repos, diffs against tracker DB, emits new postings.

Sources:
  - SimplifyJobs/Summer2027-Internships (structured listings.json)
  - vanshb03/Summer2027-Internships (README markdown table)
  - speedyapply/2027-SWE-College-Jobs (README markdown tables)
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
import urllib.request
from dataclasses import dataclass, asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "out" / "tracker.db"

SIMPLIFY_URL = "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json"
VANSH_URL = "https://raw.githubusercontent.com/vanshb03/Summer2027-Internships/main/README.md"
SPEEDY_URL = "https://raw.githubusercontent.com/speedyapply/2027-SWE-College-Jobs/main/README.md"


@dataclass
class Posting:
    source: str
    company: str
    title: str
    locations: str
    url: str
    posting_id: str  # stable dedupe key
    sponsorship: str = ""
    citizenship_required: bool = False
    closed: bool = False


def _fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "jobhunt-watcher"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode()


def _clean_url(url: str) -> str:
    url = re.sub(r"[?&]utm_source=[^&]*", "", url)
    return url.rstrip("?&")


def fetch_simplify() -> list[Posting]:
    data = json.loads(_fetch(SIMPLIFY_URL))
    out = []
    for d in data:
        if not d.get("is_visible") or not d.get("active", True):
            continue
        if "Summer 2027" not in (d.get("terms") or []):
            continue
        out.append(Posting(
            source="simplify",
            company=d["company_name"].strip(),
            title=d["title"].strip(),
            locations="; ".join(d.get("locations") or []),
            url=_clean_url(d["url"]),
            posting_id=f"simplify:{d['id']}",
            sponsorship=d.get("sponsorship", ""),
        ))
    return out


_MD_LINK = re.compile(r'href="([^"]+)"')


def _parse_md_table(md: str, source: str) -> list[Posting]:
    out = []
    last_company = ""
    for line in md.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 4 or set(cells[0]) <= {"-", " "} or cells[0] in ("Company",):
            continue
        company = re.sub(r"<[^>]+>", "", cells[0])          # strip embedded HTML
        company = re.sub(r"\*\*|\[|\]\([^)]*\)|↳", "", company).strip() or last_company
        last_company = company
        title = cells[1]
        m = _MD_LINK.search(line)
        if not m:
            continue
        url = _clean_url(m.group(1))
        closed = "🔒" in line
        out.append(Posting(
            source=source,
            company=company,
            title=re.sub(r"🛂|🇺🇸|🔒", "", title).strip(),
            locations=re.sub(r"<[^>]+>", " ", cells[2]).strip()[:200],
            url=url,
            posting_id=f"{source}:{company.lower()}:{re.sub(r'[^a-z0-9]', '', title.lower())[:60]}",
            sponsorship="no-sponsorship" if "🛂" in title else "",
            citizenship_required="🇺🇸" in title,
            closed=closed,
        ))
    return out


def fetch_vansh() -> list[Posting]:
    return _parse_md_table(_fetch(VANSH_URL), "vansh")


def fetch_speedy() -> list[Posting]:
    md = _fetch(SPEEDY_URL)  # USA internships page only (no new grad, no intl)
    return _parse_md_table(md, "speedy")


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS postings (
        posting_id TEXT PRIMARY KEY,
        source TEXT, company TEXT, title TEXT, locations TEXT, url TEXT,
        sponsorship TEXT, citizenship_required INTEGER, closed INTEGER,
        first_seen INTEGER, status TEXT DEFAULT 'new'
        , outcome TEXT
        , last_attempt_at INTEGER
        , attempt_count INTEGER NOT NULL DEFAULT 0
        , last_error TEXT
        -- status: new -> filtered_out | queued -> tailored -> ready -> submitted | failed | skipped_dupe_company
    );
    CREATE TABLE IF NOT EXISTS applications (
        posting_id TEXT PRIMARY KEY REFERENCES postings(posting_id),
        resume_path TEXT, ats TEXT, submitted_at INTEGER, confirmation TEXT, notes TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_company ON postings(company);
    """)


def upsert(conn: sqlite3.Connection, postings: list[Posting]) -> list[Posting]:
    """Insert unseen postings, return the genuinely new ones."""
    new = []
    now = int(time.time())
    for p in postings:
        cur = conn.execute("SELECT 1 FROM postings WHERE posting_id=?", (p.posting_id,))
        if cur.fetchone():
            continue
        # cross-source dedupe: same company + very similar title already tracked
        cur = conn.execute(
            "SELECT 1 FROM postings WHERE lower(company)=lower(?) AND url=?", (p.company, p.url)
        )
        if cur.fetchone():
            continue
        conn.execute(
            "INSERT INTO postings (posting_id, source, company, title, locations, url, "
            "sponsorship, citizenship_required, closed, first_seen, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (p.posting_id, p.source, p.company, p.title, p.locations, p.url,
             p.sponsorship, int(p.citizenship_required), int(p.closed), now, "new"),
        )
        new.append(p)
    conn.commit()
    return new


def run() -> dict:
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    all_new: list[Posting] = []
    errors = {}
    for name, fn in [("simplify", fetch_simplify), ("vansh", fetch_vansh), ("speedy", fetch_speedy)]:
        try:
            all_new += upsert(conn, fn())
        except Exception as e:  # keep other sources alive
            errors[name] = str(e)
    summary = {
        "new_count": len(all_new),
        "new": [asdict(p) for p in all_new],
        "errors": errors,
        "total_tracked": conn.execute("SELECT COUNT(*) FROM postings").fetchone()[0],
    }
    conn.close()
    return summary


if __name__ == "__main__":
    s = run()
    print(json.dumps({k: v for k, v in s.items() if k != "new"}, indent=2))
    for p in s["new"][:15]:
        print(f"  NEW: {p['company']} - {p['title']} [{p['source']}]")
    if len(s["new"]) > 15:
        print(f"  ... and {len(s['new']) - 15} more")
