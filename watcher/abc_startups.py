"""Series A/B/C startup scout: discover funded startups, resolve their public
ATS job feeds, and poll them into the tracker.

Why: David wants SWE/ML roles (winter/summer intern AND full-time) at Series
A/B/C startups — the sweet spot poorly covered by the GitHub listing repos
(intern + big-co) and YC lists (seed). Those postings live on each company's
own careers page, but nearly all A-C startups use Greenhouse / Lever / Ashby,
whose job boards are PUBLIC JSON APIs (no auth, no browser). So:

  1. discover — harvest funding announcements (TechCrunch funding + venture
     RSS, Crunchbase News RSS) plus VC portfolio pages with parseable round
     data (a16z embeds a full portfolio JSON). claude-haiku extracts
     {company, round, date, sector, hq} from news items; seed/D+/public are
     discarded. Results land in the abc_companies table.
  2. resolve — guess ATS board slugs from the company name and probe the
     public JSON endpoints. Fall back to scanning the company homepage for
     greenhouse/lever/ashby careers links (plain urllib). 3 strikes -> dead.
  3. poll — fetch resolved boards on the drip cadence and upsert SWE/ML
     intern/full-time US-or-remote postings with source='abc'
     (posting_id 'abc:<ats>:<job id>'). The existing filter -> tailor ->
     submit pipeline takes it from there; the Greenhouse/Lever/Ashby
     adapters already exist in apply/.

Ingestion only: this module never submits anything. Fetches are sequential
with a per-host gap (polite) and a real UA string.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DB = ROOT / "out" / "tracker.db"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 jobhunt-watcher"
MODEL = "claude-haiku-4-5"

RSS_FEEDS = [
    ("techcrunch-funding", "https://techcrunch.com/tag/funding/feed/"),
    ("techcrunch-venture", "https://techcrunch.com/category/venture/feed/"),
    ("crunchbase-news", "https://news.crunchbase.com/feed/"),
]
A16Z_PORTFOLIO = "https://a16z.com/portfolio/"

ATS_ENDPOINTS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
    "lever": "https://api.lever.co/v0/postings/{slug}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{slug}",
}
CAREERS_URLS = {
    "greenhouse": "https://boards.greenhouse.io/{slug}",
    "lever": "https://jobs.lever.co/{slug}",
    "ashby": "https://jobs.ashbyhq.com/{slug}",
}
RESOLVE_MAX_FAILS = 3   # discovery -> dead after this many resolution attempts
POLL_MAX_FAILS = 5      # resolved -> dead after this many consecutive poll errors

# ---------------------------------------------------------------- polite fetch
_LAST_HIT: dict = {}
_HOST_GAP = 0.8  # seconds between requests to the same host


def _get(url: str, timeout: int = 25) -> str:
    host = urllib.parse.urlparse(url).netloc
    wait = _LAST_HIT.get(host, 0) + _HOST_GAP - time.time()
    if wait > 0:
        time.sleep(wait)
    _LAST_HIT[host] = time.time()
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


# ---------------------------------------------------------------- LLM helper
def _claude(prompt: str, max_tokens: int = 8000) -> str:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        return ""
    body = json.dumps({
        "model": MODEL, "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body,
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            resp = json.load(r)
    except Exception as e:
        print(f"[abc] claude unavailable: {type(e).__name__}", file=sys.stderr)
        return ""
    return "".join(b.get("text", "") for b in resp["content"] if b.get("type") == "text")


# ---------------------------------------------------------------- helpers
def _compact(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


def _normalize_round(value) -> str | None:
    """'Series B' / 'b' -> 'B'; seed / D+ / IPO / junk -> None."""
    s = str(value or "").strip().upper()
    s = re.sub(r"^SERIES\s+", "", s)
    return s if s in {"A", "B", "C"} else None


_SLUG_SUFFIXES = {"inc", "io", "ai", "labs", "lab", "hq", "technologies",
                  "technology", "tech", "co", "corp", "company", "systems", "app"}


def slug_guesses(name: str) -> list[str]:
    """Ordered ATS board-slug candidates from a company name.

    'Harvey AI' -> ['harveyai', 'harvey-ai', 'harvey']; 'Character.AI' ->
    ['characterai', 'character-ai', 'character']. Kept small on purpose: each
    guess costs up to 3 live probes.
    """
    tokens = re.findall(r"[a-z0-9]+", str(name or "").lower())
    if not tokens:
        return []
    out: list[str] = []

    def add(s: str) -> None:
        if s and len(s) >= 2 and s not in out:
            out.append(s)

    add("".join(tokens))
    add("-".join(tokens))
    if len(tokens) > 1 and tokens[-1] in _SLUG_SUFFIXES:
        core = tokens[:-1]
        add("".join(core))
        add("-".join(core))
    return out[:5]


# ---------------------------------------------------------------- discovery
def _parse_rss(xml_text: str, via: str) -> list[dict]:
    items = []
    root = ET.fromstring(xml_text)
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        desc = re.sub(r"<[^>]+>", " ", item.findtext("description") or "")
        desc = re.sub(r"\s+", " ", desc).strip()[:1200]
        date = ""
        pub = item.findtext("pubDate") or ""
        try:
            date = parsedate_to_datetime(pub).date().isoformat()
        except Exception:
            pass
        if title:
            items.append({"title": title, "summary": desc, "date": date, "via": via})
    return items


def fetch_funding_news() -> list[dict]:
    items = []
    for via, url in RSS_FEEDS:
        try:
            items += _parse_rss(_get(url), via)
        except Exception as e:
            print(f"[abc] {via} feed failed: {e}", file=sys.stderr)
    return items


_EXTRACT_PROMPT = (
    "You extract venture funding rounds from news items. For each item below, "
    "identify every STARTUP that raised a Series A, Series B, or Series C "
    "equity round (an item may contain several, e.g. weekly digests, or none). "
    "Skip: seed/pre-seed rounds, Series D or later, debt, IPOs, acquisitions, "
    "and VC firms raising funds (a fund is not a startup).\n"
    "Return ONLY a JSON array; one object per funded startup:\n"
    '{"i": <item index>, "company": str, "round": "A"|"B"|"C", '
    '"date": "YYYY-MM-DD" or null, "sector": str, "hq": str or "", '
    '"website": str or null}\n'
    "Items:\n{items}"
)


def extract_companies(items: list[dict]) -> list[dict]:
    """claude-haiku turns funding-news items into {company, round, ...} rows."""
    if not items:
        return []
    batch = [{"i": i, "title": r["title"], "summary": r["summary"][:600]}
             for i, r in enumerate(items)]
    text = _claude(_EXTRACT_PROMPT.replace("{items}", json.dumps(batch)))
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        return []
    try:
        parsed = json.loads(m.group(0))
    except Exception:
        return []
    out = []
    for p in parsed:
        if not isinstance(p, dict):
            continue
        rnd = _normalize_round(p.get("round"))
        name = str(p.get("company") or "").strip()
        if not rnd or len(_compact(name)) < 2:
            continue
        idx = p.get("i")
        date = p.get("date") or (items[idx]["date"] if isinstance(idx, int) and 0 <= idx < len(items) else "")
        via = items[idx]["via"] if isinstance(idx, int) and 0 <= idx < len(items) else "rss"
        out.append({"company": name, "round": rnd, "announced_at": date or "",
                    "sector": str(p.get("sector") or "")[:80],
                    "hq": str(p.get("hq") or "")[:80],
                    "website": str(p.get("website") or "") or "",
                    "via": via})
    return out


def fetch_a16z() -> list[dict]:
    """a16z embeds its whole portfolio as JSON (window.a16z_portfolio_companies).

    Round comes from the announcement excerpt ('a16z leads X's $25M Series A'),
    which is explicit enough that a regex beats an LLM here. Exits/IPO/growth/
    seed stages are skipped.
    """
    html = _get(A16Z_PORTFOLIO, timeout=40)
    start = html.find("window.a16z_portfolio_companies = ")
    if start < 0:
        return []
    start += len("window.a16z_portfolio_companies = ")
    depth = 0
    blob = ""
    for k in range(start, len(html)):
        if html[k] == "[":
            depth += 1
        elif html[k] == "]":
            depth -= 1
            if depth == 0:
                blob = html[start:k + 1]
                break
    if not blob:
        return []
    out = []
    for d in json.loads(blob):
        stages = set(d.get("stage") or [])
        if stages & {"m&a", "ipo", "exit", "spac", "dpo", "seed", "growth"}:
            continue
        ann = d.get("announcement") or {}
        excerpt = str(ann.get("excerpt") or "") if isinstance(ann, dict) else ""
        m = re.search(r"Series ([ABC])\b", excerpt)
        if not m:
            continue
        out.append({"company": str(d.get("title") or "").strip(),
                    "round": m.group(1),
                    "announced_at": str(d.get("invest_date") or ""),
                    "sector": str(d.get("overview") or "")[:80],
                    "hq": "",
                    "website": str(d.get("web") or ""),
                    "via": "a16z-portfolio"})
    return out


# ---------------------------------------------------------------- storage
def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS abc_companies (
        key TEXT PRIMARY KEY,          -- compact(lower(name))
        name TEXT,
        round TEXT,                    -- A | B | C
        announced_at TEXT,
        sector TEXT, hq TEXT, website TEXT,
        discovered_via TEXT,
        careers_url TEXT, ats TEXT, ats_board_id TEXT,
        status TEXT DEFAULT 'discovered',  -- discovered|resolved|polling|dead
        fail_count INTEGER NOT NULL DEFAULT 0,
        first_seen INTEGER, last_checked INTEGER,
        notes TEXT
    );
    """)


def store_companies(conn: sqlite3.Connection, rows: list[dict]) -> int:
    new = 0
    now = int(time.time())
    for r in rows:
        key = _compact(r["company"])
        if len(key) < 2:
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO abc_companies "
            "(key, name, round, announced_at, sector, hq, website, discovered_via, "
            " status, first_seen, notes) VALUES (?,?,?,?,?,?,?,?,'discovered',?,?)",
            (key, r["company"], r["round"], r.get("announced_at", ""),
             r.get("sector", ""), r.get("hq", ""), r.get("website", ""),
             r.get("via", ""), now, ""))
        new += cur.rowcount
    conn.commit()
    return new


# ---------------------------------------------------------------- ATS resolution
def _probe(ats: str, slug: str) -> list | None:
    """Hit the public board API; a job list (possibly empty) means it exists."""
    try:
        d = json.loads(_get(ATS_ENDPOINTS[ats].format(slug=slug), timeout=12))
    except Exception:
        return None
    jobs = d.get("jobs") if isinstance(d, dict) else d
    return jobs if isinstance(jobs, list) else None


def _gh_board_name(slug: str) -> str:
    try:
        d = json.loads(_get(f"https://boards-api.greenhouse.io/v1/boards/{slug}", timeout=12))
        return str(d.get("name") or "")
    except Exception:
        return ""


_ATS_LINK_RE = re.compile(
    r"(?:boards|job-boards)\.greenhouse\.io/([A-Za-z0-9_-]{2,})"
    r"|greenhouse\.io/embed/job_board\?for=([A-Za-z0-9_-]{2,})"
    r"|jobs\.lever\.co/([A-Za-z0-9_-]{2,})"
    r"|jobs\.ashbyhq\.com/([A-Za-z0-9_-]{2,})")


def _homepage_ats(website: str) -> tuple | None:
    """Scan the company homepage (and one careers link) for an ATS board URL."""
    if not website.startswith("http"):
        website = "https://" + website.lstrip("/")
    pages = [website]
    for depth, url in enumerate(pages):
        try:
            html = _get(url, timeout=15)
        except Exception:
            continue
        m = _ATS_LINK_RE.search(html)
        if m:
            gh1, gh2, lever, ashby = m.groups()
            if gh1 or gh2:
                return "greenhouse", (gh1 or gh2)
            if lever:
                return "lever", lever
            return "ashby", ashby
        if depth == 0:
            c = re.search(r'href="([^"]*(?:careers|jobs)[^"]*)"', html, re.I)
            if c and not c.group(1).startswith("mailto"):
                pages.append(urllib.parse.urljoin(url, c.group(1)))
    return None


def resolve_company(name: str, website: str = "") -> tuple | None:
    """Find (ats, board_id, note) for a company via slug guessing, then the
    homepage fallback. Greenhouse hits are verified against the board's own
    company name to avoid slug collisions (e.g. 'linear')."""
    compact = _compact(name)
    for slug in slug_guesses(name):
        exact = _compact(slug) == compact
        for ats in ("greenhouse", "ashby", "lever"):
            if not exact and len(slug) < 5:
                continue  # short truncated guesses collide too easily
            jobs = _probe(ats, slug)
            if jobs is None:
                continue
            if ats == "greenhouse":
                board = _compact(_gh_board_name(slug))
                if board and not (board in compact or compact in board):
                    continue  # someone else's board
            return ats, slug, f"resolved via slug guess '{slug}'"
    if website:
        hit = _homepage_ats(website)
        if hit:
            ats, slug = hit
            if _probe(ats, slug) is not None:
                return ats, slug, f"resolved via homepage careers link ({website})"
    return None


def resolve_pending(conn: sqlite3.Connection, limit: int = 40) -> dict:
    rows = conn.execute(
        "SELECT key, name, website, fail_count FROM abc_companies "
        "WHERE status='discovered' ORDER BY first_seen DESC LIMIT ?", (limit,)).fetchall()
    resolved, failed, dead = 0, 0, 0
    now = int(time.time())
    for key, name, website, fails in rows:
        hit = resolve_company(name, website or "")
        if hit:
            ats, slug, note = hit
            conn.execute(
                "UPDATE abc_companies SET status='resolved', ats=?, ats_board_id=?, "
                "careers_url=?, notes=?, fail_count=0, last_checked=? WHERE key=?",
                (ats, slug, CAREERS_URLS[ats].format(slug=slug), note, now, key))
            resolved += 1
        else:
            fails += 1
            status = "dead" if fails >= RESOLVE_MAX_FAILS else "discovered"
            note = (f"ats resolution failed {fails}x (greenhouse/ashby/lever probes"
                    f"{' + homepage' if website else ''})")
            conn.execute(
                "UPDATE abc_companies SET fail_count=?, status=?, notes=?, last_checked=? "
                "WHERE key=?", (fails, status, note, now, key))
            failed += 1
            dead += status == "dead"
        conn.commit()
    return {"attempted": len(rows), "resolved": resolved, "failed": failed, "newly_dead": dead}


# ---------------------------------------------------------------- polling
TITLE_OK_RE = re.compile(
    r"software|engineer|\bswe\b|\bml\b|machine learning|\bai\b|developer"
    r"|full[ -]?stack|back[ -]?end|front[ -]?end|infrastructure|platform"
    r"|data engineer|research|founding|intern", re.I)
TITLE_SKIP_RE = re.compile(
    r"sales|marketing|solutions engineer|field engineer|support|success"
    r"|account|recruit|talent|people|designer|counsel|legal|finance|customer"
    r"|community|hardware|mechanical|electrical|civil|admin|chief|\bvp\b"
    r"|head of|director|staff |principal|senior|\bsr\.?\b|manager"
    # hardware/defense/biotech startups list plenty of non-software "engineers"
    # (Saronic 2026-08-17: shipyard/industrial/quality roles got ingested)
    r"|industrial|manufactur|\bquality\b|\bnpi\b|supply|logistics|shipyard"
    r"|\bweld|facilities|mission operations|sustainment|propulsion|avionics"
    r"|structur|thermal|electrician|technician|\blead\b|leadership|\bgtm\b"
    r"|test engineer|process engineer|integrations? engineer|maintenance"
    r"|\bux\b|wire harness|\bharness\b|postdoc|clinical|build engineer"
    r"|accuracy control|marine|outfit|tooling|operations specialist", re.I)

_STATES = ("AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI"
           "|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX"
           "|UT|VT|VA|WA|WV|WI|WY|DC")
US_RE = re.compile(
    r"united states|\busa?\b|u\.s\.|san francisco|new york|nyc|boston|seattle"
    r"|austin|denver|chicago|los angeles|palo alto|mountain view|menlo park"
    r"|sunnyvale|san jose|san mateo|oakland|santa clara|redwood|cambridge"
    r"|washington|atlanta|miami|philadelphia|pittsburgh|portland|salt lake"
    r"|phoenix|dallas|houston|san diego|irvine|bellevue|brooklyn|manhattan"
    r"|,\s*(?:" + _STATES.lower() + r")\b", re.I)
NON_US_RE = re.compile(
    r"canada|united kingdom|\buk\b|london|toronto|vancouver|montreal|berlin"
    r"|munich|paris|amsterdam|india|bangalore|bengaluru|singapore|australia"
    r"|sydney|dublin|israel|tel aviv|germany|france|netherlands|japan|tokyo"
    r"|brazil|mexico|poland|warsaw|spain|madrid|barcelona|europe|emea|apac"
    r"|latam|china|korea|stockholm|zurich|copenhagen|lisbon|tallinn"
    r"|s[aã]o paulo|bogot[aá]|mexico city|buenos aires|santiago|colombia"
    r"|argentina|nigeria|lagos|cairo|dubai|philippines|manila|vietnam"
    r"|portugal|lisboa|t[uü]rkiye|turkey|istanbul|greece|athens|italy|rome"
    r"|milan|belgium|brussels|austria|vienna|switzerland|geneva|prague", re.I)


def _us_or_remote(loc: str) -> bool:
    l = (loc or "").strip().lower()
    if not l:
        return True  # unknown: let the JD fetch downstream decide
    if US_RE.search(l):
        return True
    return "remote" in l and not NON_US_RE.search(l)


def _job_fields(ats: str, j: dict) -> tuple:
    """-> (job_id, title, location, url) for one raw ATS job record."""
    if ats == "greenhouse":
        return (str(j.get("id") or ""), str(j.get("title") or "").strip(),
                str((j.get("location") or {}).get("name") or ""),
                str(j.get("absolute_url") or ""))
    if ats == "lever":
        cats = j.get("categories") or {}
        loc = str(cats.get("location") or "")
        if str(j.get("workplaceType") or "") == "remote":
            loc = (loc + " Remote").strip()
        return (str(j.get("id") or ""), str(j.get("text") or "").strip(), loc,
                str(j.get("hostedUrl") or j.get("applyUrl") or ""))
    # ashby
    locs = [str(j.get("location") or "")]
    locs += [str((s or {}).get("location") or "") for s in (j.get("secondaryLocations") or [])]
    if j.get("isRemote"):
        locs.append("Remote")
    return (str(j.get("id") or ""), str(j.get("title") or "").strip(),
            "; ".join(x for x in locs if x), str(j.get("jobUrl") or j.get("applyUrl") or ""))


def _postings_from_jobs(ats: str, company: str, jobs: list) -> list:
    """Raw board jobs -> watch.Posting rows (source='abc'). Light SWE/ML +
    US-or-remote gate here; filter.py title_ok does the real gating later."""
    from watcher import watch
    out = []
    for j in jobs:
        if not isinstance(j, dict) or j.get("isListed") is False:
            continue
        job_id, title, loc, url = _job_fields(ats, j)
        if not job_id or not title or not url:
            continue
        if not TITLE_OK_RE.search(title) or TITLE_SKIP_RE.search(title):
            continue
        if not _us_or_remote(loc):
            continue
        out.append(watch.Posting(
            source="abc", company=company, title=title, locations=loc[:200],
            url=url, posting_id=f"abc:{ats}:{job_id}"))
    return out


def poll_boards(conn: sqlite3.Connection) -> dict:
    from watcher import watch
    rows = conn.execute(
        "SELECT key, name, ats, ats_board_id, fail_count FROM abc_companies "
        "WHERE status IN ('resolved','polling')").fetchall()
    new, errors = 0, 0
    now = int(time.time())
    for key, name, ats, slug, fails in rows:
        jobs = _probe(ats, slug)
        if jobs is None:
            fails += 1
            status = "dead" if fails >= POLL_MAX_FAILS else "polling"
            conn.execute(
                "UPDATE abc_companies SET fail_count=?, status=?, last_checked=?, notes=? "
                "WHERE key=?",
                (fails, status, now, f"poll error {fails}x on {ats}:{slug}", key))
            errors += 1
        else:
            postings = _postings_from_jobs(ats, name, jobs)
            new += len(watch.upsert(conn, postings))
            conn.execute(
                "UPDATE abc_companies SET status='polling', fail_count=0, last_checked=?, "
                "notes=? WHERE key=?",
                (now, f"ok: {len(jobs)} open roles, {len(postings)} matched", key))
        conn.commit()
    return {"companies_polled": len(rows), "new_postings": new, "poll_errors": errors}


# ---------------------------------------------------------------- entry point
def run(discover: bool = False, resolve_limit: int = 40) -> dict:
    DB.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB)
    from watcher import watch
    watch.init_db(conn)
    init_db(conn)
    res: dict = {}
    if discover:
        cands = []
        try:
            cands += extract_companies(fetch_funding_news())
        except Exception as e:
            res["rss_error"] = str(e)
        try:
            cands += fetch_a16z()
        except Exception as e:
            res["a16z_error"] = str(e)
        res["candidates"] = len(cands)
        res["companies_new"] = store_companies(conn, cands)
        res["resolve"] = resolve_pending(conn, limit=resolve_limit)
    res["poll"] = poll_boards(conn)
    res["companies_total"] = conn.execute("SELECT COUNT(*) FROM abc_companies").fetchone()[0]
    res["companies_live"] = conn.execute(
        "SELECT COUNT(*) FROM abc_companies WHERE status IN ('resolved','polling')").fetchone()[0]
    conn.close()
    return res


if __name__ == "__main__":
    print(json.dumps(run(discover="--discover" in sys.argv,
                         resolve_limit=200 if "--discover" in sys.argv else 40), indent=2))
