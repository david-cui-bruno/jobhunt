"""Series A-D startup scout: discover funded startups, resolve their public
ATS job feeds, and poll them into the tracker.

Why: David wants engineer-adjacent roles (winter/summer intern AND full-time)
at Series A-D startups — the sweet spot poorly covered by the GitHub listing
repos (intern + big-co) and YC lists (seed). Those postings live on each
company's own careers page, but nearly all A-D startups use Greenhouse /
Lever / Ashby / Workable / SmartRecruiters, whose job boards are PUBLIC JSON
APIs (no auth, no browser). So:

  1. discover — harvest funding announcements (TechCrunch funding + venture
     RSS, Crunchbase News RSS, VentureBeat) plus VC portfolio pages with
     parseable round data (a16z embeds a full portfolio JSON). claude-haiku
     extracts {company, round, date, sector, hq} from news items; seed/E+/
     public are discarded. Results land in the abc_companies table.
  2. resolve — CAREERS-PAGE FIRST (David 2026-08-19: "go onto their websites
     and look at their career pages instead of guessing the slug"): find the
     official domain (news item website, else Clearbit autocomplete verified
     by name), fetch the homepage + its careers/jobs links, and pull the real
     ATS board out of the page. Slug guessing survives only as a cheap last
     resort. 3 strikes -> dead (with the careers URL kept in notes so a human
     or a future generic-page parser can pick it up).
  3. poll — fetch resolved boards on the drip cadence and upsert role-matched
     US-or-remote postings with source='abc' (posting_id 'abc:<ats>:<job id>').
     The existing filter -> tailor -> submit pipeline takes it from there; all
     five ATS adapters already exist in apply/.

Backfill: watcher/abc_backfill.py seeds this table from the TechCrunch
archive (last ~3 years of Series A-D announcements) so the scout does not
start from only the current news window.

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
    ("techcrunch-startups", "https://techcrunch.com/category/startups/feed/"),
    ("crunchbase-news", "https://news.crunchbase.com/feed/"),
    ("techcrunch-ai", "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("venturebeat", "https://venturebeat.com/feed"),  # no trailing slash: /feed/ 308s
]
A16Z_PORTFOLIO = "https://a16z.com/portfolio/"

ATS_ENDPOINTS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
    "lever": "https://api.lever.co/v0/postings/{slug}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{slug}",
    # Workable widget API: {'name', 'description', 'jobs': [...]}
    "workable": "https://apply.workable.com/api/v1/widget/accounts/{slug}",
    # SmartRecruiters public postings API: {'totalFound', 'content': [...]}
    "smartrecruiters": "https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100",
}
CAREERS_URLS = {
    "greenhouse": "https://boards.greenhouse.io/{slug}",
    "lever": "https://jobs.lever.co/{slug}",
    "ashby": "https://jobs.ashbyhq.com/{slug}",
    "workable": "https://apply.workable.com/{slug}/",
    "smartrecruiters": "https://careers.smartrecruiters.com/{slug}",
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
    """'Series B' / 'b' -> 'B'; seed / E+ / IPO / junk -> None.

    Series D included since 2026-08-19 (David: "i care about the series
    a/b/c/d thing a lot").
    """
    s = str(value or "").strip().upper()
    s = re.sub(r"^SERIES\s+", "", s)
    return s if s in {"A", "B", "C", "D"} else None


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
    "identify every STARTUP that raised a Series A, Series B, Series C, or "
    "Series D equity round (an item may contain several, e.g. weekly digests, "
    "or none). "
    "Skip: seed/pre-seed rounds, Series E or later, debt, IPOs, acquisitions, "
    "and VC firms raising funds (a fund is not a startup).\n"
    "Return ONLY a JSON array; one object per funded startup:\n"
    '{"i": <item index>, "company": str, "round": "A"|"B"|"C"|"D", '
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
        m = re.search(r"Series ([ABCD])\b", excerpt)
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
    if ats == "smartrecruiters":
        jobs = d.get("content") if isinstance(d, dict) else None
    else:
        jobs = d.get("jobs") if isinstance(d, dict) else d
    return jobs if isinstance(jobs, list) else None


def _gh_board_name(slug: str) -> str:
    try:
        d = json.loads(_get(f"https://boards-api.greenhouse.io/v1/boards/{slug}", timeout=12))
        return str(d.get("name") or "")
    except Exception:
        return ""


_ATS_LINK_RE = re.compile(
    r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board\?for=)?([A-Za-z0-9_-]{2,})"
    r"|greenhouse\.io/embed/job_board\?for=([A-Za-z0-9_-]{2,})"
    r"|jobs\.lever\.co/([A-Za-z0-9_-]{2,})"
    r"|jobs\.ashbyhq\.com/([A-Za-z0-9_%-]{2,})"
    r"|apply\.workable\.com/(?:api/v\d/accounts/)?([A-Za-z0-9_-]{2,})"
    r"|(?:careers|jobs)\.smartrecruiters\.com/([A-Za-z0-9_-]{2,})")

_CAREERS_LINK_RE = re.compile(
    r'href=["\']([^"\']*(?:career|jobs|join[- ]?us|join-the-team|work[- ]with[- ]us|openings|hiring)[^"\']*)["\']',
    re.I)

_BAD_SLUGS = {"embed", "job", "jobs", "board", "boards", "careers", "api", "www", "j"}


def _match_to_hit(m: re.Match) -> tuple | None:
    gh1, gh2, lever, ashby, workable, smartrec = m.groups()
    for ats, slug in (("greenhouse", gh1 or gh2), ("lever", lever),
                      ("ashby", ashby), ("workable", workable),
                      ("smartrecruiters", smartrec)):
        if slug and slug.lower() not in _BAD_SLUGS:
            return ats, urllib.parse.unquote(slug)
    return None


def find_official_domain(name: str) -> str:
    """Company name -> official website domain via Clearbit autocomplete.

    Free, no key, fast. The name must roughly match to avoid grabbing a
    lookalike (compact containment either way, e.g. 'Harvey' ~ 'harvey.ai').
    """
    q = urllib.parse.quote(str(name or "").strip())
    if not q:
        return ""
    try:
        rows = json.loads(_get(
            f"https://autocomplete.clearbit.com/v1/companies/suggest?query={q}", timeout=10))
    except Exception:
        return ""
    want = _compact(name)
    for r in rows if isinstance(rows, list) else []:
        got = _compact(str(r.get("name") or ""))
        domain = str(r.get("domain") or "")
        dom_core = _compact(domain.split(".")[0])
        # Exact matches only: containment let 'Cursor' resolve to
        # cursorinfo.co.il. A missed domain just falls through to slug
        # guessing; a wrong domain resolves someone else's job board.
        if domain and (got == want or dom_core == want):
            return domain
    return ""


def careers_page_ats(website: str, max_pages: int = 6) -> tuple | None:
    """Crawl the company site (homepage -> careers/jobs links, 2 hops) for an
    ATS board link. This is the PRIMARY resolution path (David 2026-08-19):
    read the real careers page instead of guessing board slugs.

    Careers pages often live on subdomains (careers.x.com) or paths (/careers,
    /about/jobs), and the ATS link usually sits on that second page; sometimes
    a third ('View openings'). Bounded breadth-first walk, same-site only,
    max_pages fetches total.
    """
    if not website:
        return None
    if not website.startswith("http"):
        website = "https://" + website.lstrip("/")
    root_host = urllib.parse.urlparse(website).netloc.lower().removeprefix("www.")
    seen: set[str] = set()
    queue: list[tuple[str, int]] = [(website, 0)]
    while queue and len(seen) < max_pages:
        url, depth = queue.pop(0)
        norm = url.rstrip("/")
        if norm in seen:
            continue
        seen.add(norm)
        try:
            html = _get(url, timeout=15)
        except Exception:
            continue
        m = _ATS_LINK_RE.search(html)
        if m:
            hit = _match_to_hit(m)
            if hit:
                return hit
        if depth >= 2:
            continue
        for link in _CAREERS_LINK_RE.findall(html)[:4]:
            if link.startswith(("mailto:", "tel:", "#", "javascript:")):
                continue
            nxt = urllib.parse.urljoin(url, link)
            host = urllib.parse.urlparse(nxt).netloc.lower().removeprefix("www.")
            # same site or its careers.* subdomain only
            if host == root_host or host.endswith("." + root_host):
                queue.append((nxt, depth + 1))
    return None


def resolve_company(name: str, website: str = "") -> tuple | None:
    """Find (ats, board_id, note) for a company.

    Order (David 2026-08-19): real careers page first — known website, else
    Clearbit-resolved official domain — then slug guessing as the cheap last
    resort. Greenhouse hits are verified against the board's own company name
    to avoid slug collisions (e.g. 'linear')."""
    site = website or find_official_domain(name)
    if site:
        hit = careers_page_ats(site)
        if hit:
            ats, slug = hit
            if _probe(ats, slug) is not None:
                return ats, slug, f"resolved via careers page ({site})"
    compact = _compact(name)
    for slug in slug_guesses(name):
        exact = _compact(slug) == compact
        for ats in ("greenhouse", "ashby", "lever", "workable", "smartrecruiters"):
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
            note = (f"ats resolution failed {fails}x (careers page"
                    f"{' via ' + website if website else ' via clearbit domain'}"
                    f" + 5-ats slug probes)")
            conn.execute(
                "UPDATE abc_companies SET fail_count=?, status=?, notes=?, last_checked=? "
                "WHERE key=?", (fails, status, note, now, key))
            failed += 1
            dead += status == "dead"
        conn.commit()
    return {"attempted": len(rows), "resolved": resolved, "failed": failed, "newly_dead": dead}


# ---------------------------------------------------------------- polling
# Wide include (David 2026-08-19): everything engineer-adjacent plus PM and
# quant; no hardware. filter.py title_ok does the authoritative gating later,
# so this pre-gate only needs to keep junk volume down.
TITLE_OK_RE = re.compile(
    r"software|engineer|\bswe\b|\bsde\b|\bml\b|machine learning|\bai\b|developer"
    r"|full[ -]?stack|back[ -]?end|front[ -]?end|infrastructure|platform"
    r"|data engineer|data scientist|applied scientist|research|founding|intern"
    r"|member of technical staff|\bmts\b|devops|site reliability|\bsre\b"
    r"|security|systems|compiler|\bios\b|android|mobile|embedded|firmware"
    r"|robotics|autonomy|perception|simulation|quant|product manager"
    r"|product management|\bapm\b|forward deployed|solutions engineer"
    r"|product intern|computer scientist", re.I)
TITLE_SKIP_RE = re.compile(
    r"sales|marketing|\bfield engineer\b|support|success"
    r"|account|recruit|talent|people|designer|counsel|legal|finance|customer"
    r"|community|\bhardware\b|mechanical|electrical|civil|admin|chief|\bvp\b"
    r"|head of|director|staff |principal|senior|\bsr\.?\b|manager"
    # hardware/defense/biotech startups list plenty of non-software "engineers"
    # (Saronic 2026-08-17: shipyard/industrial/quality roles got ingested)
    r"|industrial|manufactur|\bquality\b|\bnpi\b|supply|logistics|shipyard"
    r"|\bweld|facilities|mission operations|sustainment|propulsion|avionics"
    r"|structur|thermal|electrician|technician|\blead\b|leadership|\bgtm\b"
    r"|process engineer|maintenance"
    r"|\bux\b|wire harness|\bharness\b|postdoc|clinical|build engineer"
    r"|accuracy control|marine|outfit|tooling|operations specialist", re.I)
# 'Product Manager' would trip the generic 'manager' skip, so PM titles get a
# bypass, but only when no seniority marker is present ('Senior Product
# Manager' stays skipped):
PM_RE = re.compile(r"product manage|associate product manager|\bapm\b|product intern", re.I)
SENIORITY_RE = re.compile(
    r"senior|\bsr\.?\b|staff |principal|director|head of|chief|\bvp\b|\blead\b", re.I)

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
    if ats == "workable":
        # widget API: shortcode is the job id; urls are account-form pages
        loc = ", ".join(x for x in (str(j.get("city") or ""), str(j.get("state") or ""),
                                    str(j.get("country") or "")) if x)
        if j.get("telecommuting"):
            loc = (loc + " Remote").strip()
        return (str(j.get("shortcode") or ""), str(j.get("title") or "").strip(), loc,
                str(j.get("url") or j.get("shortlink") or ""))
    if ats == "smartrecruiters":
        loc_d = j.get("location") or {}
        loc = ", ".join(str(loc_d.get(k) or "") for k in ("city", "region", "country") if loc_d.get(k))
        if loc_d.get("remote"):
            loc = (loc + " Remote").strip()
        company_id = str((j.get("company") or {}).get("identifier") or "")
        jid = str(j.get("id") or "")
        return (jid, str(j.get("name") or "").strip(), loc,
                f"https://jobs.smartrecruiters.com/{company_id}/{jid}" if company_id and jid else "")
    # ashby
    locs = [str(j.get("location") or "")]
    locs += [str((s or {}).get("location") or "") for s in (j.get("secondaryLocations") or [])]
    if j.get("isRemote"):
        locs.append("Remote")
    return (str(j.get("id") or ""), str(j.get("title") or "").strip(),
            "; ".join(x for x in locs if x), str(j.get("jobUrl") or j.get("applyUrl") or ""))


def _postings_from_jobs(ats: str, company: str, jobs: list, slug: str = "") -> list:
    """Raw board jobs -> watch.Posting rows (source='abc'). Light role +
    US-or-remote gate here; filter.py title_ok does the real gating later."""
    from watcher import watch
    out = []
    for j in jobs:
        if not isinstance(j, dict) or j.get("isListed") is False:
            continue
        job_id, title, loc, url = _job_fields(ats, j)
        if ats == "workable" and job_id and slug:
            # widget shortlinks omit the account; the workable adapter's URL
            # parser needs apply.workable.com/<account>/j/<CODE>
            url = f"https://apply.workable.com/{slug}/j/{job_id}/"
        if not job_id or not title or not url:
            continue
        if not TITLE_OK_RE.search(title):
            continue
        if TITLE_SKIP_RE.search(title) and not (
                PM_RE.search(title) and not SENIORITY_RE.search(title)):
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
            postings = _postings_from_jobs(ats, name, jobs, slug=slug)
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
