"""Watcher: polls target-season listing repos and emits new postings.

Sources:
  - SimplifyJobs/Summer2027-Internships (structured seasonal listings)
  - vanshb03/Summer2027-Internships (README markdown table)
  - speedyapply/2027-SWE-College-Jobs (README markdown tables)
  - speedyapply/2027-AI-College-Jobs (README markdown tables)
  - zapplyjobs/Internships-2027
  - dreamworkhq/Tech-Internships-2027
"""
from __future__ import annotations

import json
import hashlib
import re
import sqlite3
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict
from pathlib import Path

from submission.identity import canonical_conflict_reason
from submission.resolutions import cached_resolution, ensure_resolution_schema, record_resolution
from watcher.url_resolver import resolve_dreamwork_html

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "out" / "tracker.db"

SIMPLIFY_URL = "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json"
VANSH_URL = "https://raw.githubusercontent.com/vanshb03/Summer2027-Internships/main/README.md"
SPEEDY_URL = "https://raw.githubusercontent.com/speedyapply/2027-SWE-College-Jobs/main/README.md"
SPEEDY_AI_URL = "https://raw.githubusercontent.com/speedyapply/2027-AI-College-Jobs/main/README.md"
VANSH_OFFSEASON_URL = "https://raw.githubusercontent.com/vanshb03/Summer2027-Internships/main/OFFSEASON_README.md"
ZAPPLY_2027_URL = "https://raw.githubusercontent.com/zapplyjobs/Internships-2027/main/README.md"
DREAMWORK_2027_URL = "https://raw.githubusercontent.com/dreamworkhq/Tech-Internships-2027/main/README.md"

# David wants Winter and Summer 2027 only. Simplify carries other seasons in
# the same JSON feed, including titles that do not repeat the season.
TARGET_TERMS = {"Winter 2027", "Summer 2027"}


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
    """Remove UTM tracking parameters without corrupting the query string."""
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    clean_query = [(key, value) for key, value in query if not key.lower().startswith("utm_")]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(clean_query)))


def fetch_simplify() -> list[Posting]:
    data = json.loads(_fetch(SIMPLIFY_URL))
    out = []
    for d in data:
        if not d.get("is_visible") or not d.get("active", True):
            continue
        if not TARGET_TERMS.intersection(d.get("terms") or []):
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


_MD_LINK = re.compile(r'href="([^"]+)"|\]\((https?://[^)]+)\)')


def _parse_md_table(md: str, source: str) -> list[Posting]:
    out = []
    last_company = ""
    posting_column: int | None = None
    for line in md.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if cells and cells[0] == "Company":
            posting_column = next(
                (index for index, value in enumerate(cells)
                 if value.lower() in {"posting", "application", "apply"}),
                None,
            )
            continue
        if len(cells) < 4 or set(cells[0]) <= {"-", " "}:
            continue
        company = re.sub(r"<[^>]+>", "", cells[0])          # strip embedded HTML
        company = re.sub(r"\*\*|\[|\]\([^)]*\)|↳", "", company).strip() or last_company
        last_company = company
        title = cells[1]
        # Never use the first link in the full row. Several source repos link the
        # company name to its homepage and put the real application in a later
        # Posting column, which previously queued homepage-only false positives.
        m = None
        if posting_column is not None and posting_column < len(cells):
            m = _MD_LINK.search(cells[posting_column])
        if not m:
            for cell in reversed(cells[1:]):
                m = _MD_LINK.search(cell)
                if m:
                    break
        if not m:
            continue
        url = _clean_url(m.group(1) or m.group(2))
        url_hash = hashlib.sha256(url.encode()).hexdigest()[:12]
        closed = "🔒" in line
        out.append(Posting(
            source=source,
            company=company,
            title=re.sub(r"🛂|🇺🇸|🔒", "", title).strip(),
            locations=re.sub(r"<[^>]+>", " ", cells[2]).strip()[:200],
            url=url,
            posting_id=(
                f"{source}:{company.lower()}:"
                f"{re.sub(r'[^a-z0-9]', '', title.lower())[:60]}:{url_hash}"
            ),
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


def fetch_speedy_ai() -> list[Posting]:
    md = _fetch(SPEEDY_AI_URL)  # USA internships page only (no new grad, no intl)
    return _parse_md_table(md, "speedy-ai")


def fetch_vansh_offseason() -> list[Posting]:
    return _parse_md_table(_fetch(VANSH_OFFSEASON_URL), "vansh-offseason")


def fetch_zapply_2027() -> list[Posting]:
    return _parse_md_table(_fetch(ZAPPLY_2027_URL), "zapply-2027")


def fetch_dreamwork_2027() -> list[Posting]:
    return _parse_md_table(_fetch(DREAMWORK_2027_URL), "dreamwork-2027")


WATCH_SOURCES = (
    ("simplify", fetch_simplify),
    ("vansh", fetch_vansh),
    ("speedy", fetch_speedy),
    ("speedy-ai", fetch_speedy_ai),
    ("zapply-2027", fetch_zapply_2027),
    ("dreamwork-2027", fetch_dreamwork_2027),
)


def init_db(conn: sqlite3.Connection) -> None:
    for statement in [part.strip() for part in WATCH_SCHEMA.split(";") if part.strip()]:
        conn.execute(statement)
    ensure_resolution_schema(conn)


WATCH_SCHEMA = """
CREATE TABLE IF NOT EXISTS postings (
    posting_id TEXT PRIMARY KEY,
    source TEXT, company TEXT, title TEXT, locations TEXT, url TEXT,
    sponsorship TEXT, citizenship_required INTEGER, closed INTEGER,
    first_seen INTEGER, status TEXT DEFAULT 'new'
    , outcome TEXT
    , last_attempt_at INTEGER
    , attempt_count INTEGER NOT NULL DEFAULT 0
    , last_error TEXT
    -- status: new -> filtered_out | queued -> tailoring/sprinting -> ready/submitting -> submitted/manual/failed
);
CREATE TABLE IF NOT EXISTS applications (
    posting_id TEXT PRIMARY KEY REFERENCES postings(posting_id),
    resume_path TEXT, ats TEXT, submitted_at INTEGER, confirmation TEXT, notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_company ON postings(company);
"""


def _reconcile_dreamwork_alias(conn: sqlite3.Connection, posting: Posting) -> bool:
    """Repair or retire the malformed Dreamwork URL form emitted by old code."""
    if posting.source != "dreamwork-2027":
        return False
    match = re.search(r"/job/([0-9a-f-]{36})", posting.url, re.I)
    if not match:
        return False
    clean_url = posting.url[:match.end()]
    rows = conn.execute(
        """
        SELECT p.posting_id,p.url,p.status,p.outcome,p.last_error,
               EXISTS(SELECT 1 FROM applications a WHERE a.posting_id=p.posting_id)
        FROM postings p
        WHERE p.source='dreamwork-2027' AND (p.url=? OR p.url LIKE ?)
        ORDER BY p.rowid
        """,
        (clean_url, clean_url + "&utm%"),
    ).fetchall()
    if not rows:
        return False

    primary = next((row for row in rows if row[5]), None)
    if primary is None:
        primary = next((row for row in rows if row[0] == posting.posting_id), rows[0])

    if primary[1] != clean_url:
        false_stale = (
            primary[3] == "stale"
            and "liveness check marked posting stale" in (primary[4] or "")
        )
        conn.execute(
            "UPDATE postings SET url=?, outcome=?, last_error=? WHERE posting_id=?",
            (
                clean_url,
                None if false_stale else primary[3],
                None if false_stale else primary[4],
                primary[0],
            ),
        )

    for row in rows:
        if row[0] == primary[0] or row[5]:
            continue
        conn.execute(
            "UPDATE postings SET status='filtered_out', outcome='deduplicated', "
            "last_error='replaced malformed Dreamwork URL' WHERE posting_id=?",
            (row[0],),
        )
    conn.commit()
    return True


def upsert(conn: sqlite3.Connection, postings: list[Posting]) -> list[Posting]:
    """Insert unseen postings, return the genuinely new ones."""
    new = []
    now = int(time.time())
    for p in postings:
        if _reconcile_dreamwork_alias(conn, p):
            continue
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


def _is_dreamwork_wrapper(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    return parsed.netloc.lower().endswith("dreamworkhq.com") and "/job/" in parsed.path


def _has_postings_column(conn: sqlite3.Connection, name: str) -> bool:
    return any(row[1] == name for row in conn.execute("PRAGMA table_info(postings)").fetchall())


def resolve_dreamwork_postings(conn: sqlite3.Connection, fetch_page=_fetch) -> dict[str, int]:
    summary = {"cached": 0, "resolved": 0, "failed": 0, "conflicts": 0}
    source_filter = "WHERE source='dreamwork-2027'" if _has_postings_column(conn, "source") else ""
    rows = conn.execute(
        f"SELECT posting_id,url FROM postings {source_filter} ORDER BY rowid"
    ).fetchall()
    for row in rows:
        posting_id = row["posting_id"] if isinstance(row, sqlite3.Row) else row[0]
        source_url = row["url"] if isinstance(row, sqlite3.Row) else row[1]
        if not source_url or not _is_dreamwork_wrapper(source_url):
            continue
        try:
            target_url = cached_resolution(conn, posting_id, source_url)
            used_cache = target_url is not None
            if target_url is None:
                result = resolve_dreamwork_html(source_url, fetch_page(source_url))
                record_resolution(conn, result, posting_id=posting_id)
                target_url = result.resolved_url
                if target_url is None:
                    conn.commit()
                    summary["failed"] += 1
                    continue

            conflict = canonical_conflict_reason(conn, posting_id, target_url)
            if conflict:
                conn.commit()
                summary["conflicts"] += 1
                continue
            conn.execute("UPDATE postings SET url=? WHERE posting_id=?", (target_url, posting_id))
            conn.commit()
            summary["cached" if used_cache else "resolved"] += 1
        except Exception:
            conn.rollback()
            summary["failed"] += 1
    return summary


def run() -> dict:
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    all_new: list[Posting] = []
    errors = {}
    source_counts = {}
    current_postings: list[Posting] = []
    for name, fn in WATCH_SOURCES:
        try:
            postings = fn()
            source_counts[name] = len(postings)
            current_postings.extend(postings)
            all_new += upsert(conn, postings)
        except Exception as e:  # keep other sources alive
            errors[name] = str(e)
    resolution_summary = resolve_dreamwork_postings(conn)
    print(f"dreamwork_resolution={resolution_summary}")
    current_ids: set[str] = set()
    for posting in current_postings:
        row = conn.execute(
            "SELECT posting_id FROM postings WHERE posting_id=?",
            (posting.posting_id,),
        ).fetchone()
        if not row:
            row = conn.execute(
                "SELECT posting_id FROM postings WHERE lower(company)=lower(?) AND url=?",
                (posting.company, posting.url),
            ).fetchone()
        if row:
            current_ids.add(row[0])

    summary = {
        "new_count": len(all_new),
        "new": [asdict(p) for p in all_new],
        "current_posting_ids": sorted(current_ids),
        "errors": errors,
        "dreamwork_resolution": resolution_summary,
        "source_counts": source_counts,
        "total_tracked": conn.execute("SELECT COUNT(*) FROM postings").fetchone()[0],
    }
    conn.close()
    return summary


if __name__ == "__main__":
    s = run()
    print(json.dumps({
        k: v for k, v in s.items()
        if k not in {"new", "current_posting_ids"}
    }, indent=2))
    for p in s["new"][:15]:
        print(f"  NEW: {p['company']} - {p['title']} [{p['source']}]")
    if len(s["new"]) > 15:
        print(f"  ... and {len(s['new']) - 15} more")
