"""Startup discovery: sources beyond the GitHub listing repos.

Sources:
  1. Off-season listing repos (Simplify README-Off-Season, vansh OFFSEASON_README)
  2. YC companies API (recent batches) -> their careers/ATS pages -> intern/new-grad roles
  3. HN "Who is hiring?" monthly thread -> intern-friendly + LLM-parsed entries

Emits postings into the same tracker DB with source tags; the existing
filter -> tailor -> email -> submit pipeline picks them up unchanged.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "out" / "tracker.db"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

OFFSEASON_SOURCES = [
    ("simplify-offseason", "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/README-Off-Season.md"),
    ("vansh-offseason", "https://raw.githubusercontent.com/vanshb03/Summer2027-Internships/main/OFFSEASON_README.md"),
]

YC_BATCHES = ["Summer 2026", "Fall 2026", "Winter 2026"]  # recent, hiring-active
YC_API = "https://api.ycombinator.com/v0.1/companies"

HN_ALGOLIA = "https://hn.algolia.com/api/v1"


def _get(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


# ---------------------------------------------------------------- off-season repos
def fetch_offseason() -> list[dict]:
    import sys
    sys.path.insert(0, str(ROOT / "watcher"))
    from watch import _parse_md_table  # reuse the hardened parser
    out = []
    for source, url in OFFSEASON_SOURCES:
        try:
            for p in _parse_md_table(_get(url), source):
                d = p.__dict__.copy()
                out.append(d)
        except Exception as e:
            print(f"[startups] {source} failed: {e}")
    return out


# ---------------------------------------------------------------- YC batches
def fetch_yc_companies() -> list[dict]:
    companies = []
    for batch in YC_BATCHES:
        page = 1
        while True:
            q = urllib.parse.urlencode({"batch": batch, "page": page})
            try:
                d = json.loads(_get(f"{YC_API}?{q}"))
            except Exception:
                break
            cs = d.get("companies", [])
            companies += [{**c, "batch": batch} for c in cs]
            if page >= int(d.get("totalPages") or 1):
                break
            page += 1
    return companies


def yc_job_urls(company: dict) -> list[dict]:
    """Probe a YC company's careers presence for intern/new-grad roles via
    known ATS URL patterns (cheap, no browser)."""
    site = (company.get("website") or "").rstrip("/")
    slug = company.get("slug", "")
    name = company.get("name", "")
    found = []
    # workatastartup page always exists for YC cos
    waas = f"https://www.workatastartup.com/companies/{slug}"
    # common ATS guesses
    guesses = [
        ("ashby", f"https://api.ashbyhq.com/posting-api/job-board/{slug}"),
        ("greenhouse", f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"),
        ("lever", f"https://api.lever.co/v0/postings/{slug}?mode=json"),
    ]
    for ats, url in guesses:
        try:
            body = _get(url, timeout=10)
            d = json.loads(body)
        except Exception:
            continue
        jobs = d.get("jobs") if isinstance(d, dict) else d
        if not isinstance(jobs, list):
            continue
        for j in jobs:
            title = j.get("title") or j.get("text") or ""
            if not re.search(r"intern|new grad|entry", title, re.I):
                continue
            u = j.get("jobUrl") or j.get("applyUrl") or j.get("absolute_url") or j.get("hostedUrl") or ""
            loc = ""
            if ats == "ashby":
                loc = j.get("location") or ""
            elif ats == "greenhouse":
                loc = (j.get("location") or {}).get("name", "")
            elif ats == "lever":
                loc = ((j.get("categories") or {}).get("location")) or ""
            if u:
                found.append({"company": name, "title": title.strip(), "url": u,
                              "locations": loc, "source": f"yc-{company['batch'].replace(' ', '')}",
                              "waas": waas})
        if found:
            break  # one ATS per company is enough
    return found


# ---------------------------------------------------------------- HN who's hiring
def enrich_hn(rows: list[dict]) -> list[dict]:
    """Claude parses each HN comment into (company, title, fit). Only rows that
    plausibly fit SWE/ML intern/junior-with-0yoe roles survive, with real titles."""
    import os
    import urllib.request as ur
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key or not rows:
        return rows
    out = []
    batch = [{"i": i, "text": r["text"][:900]} for i, r in enumerate(rows) if r.get("text")]
    prompt = (
        "For each HN 'Who is hiring?' comment, extract JSON: "
        '{"i": <index>, "company": str, "title": str, "location": str, '
        '"fits": bool}  — fits=true ONLY if the posting plausibly suits a strong '
        "CS undergrad (SWE/ML/AI/infra intern, junior, or entry-level; US or remote; "
        "not senior-only, not sales/marketing).\n"
        f"Comments: {json.dumps(batch)}\n"
        "Return ONLY a JSON array."
    )
    body = json.dumps({
        "model": "claude-sonnet-5", "max_tokens": 4000,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = ur.Request("https://api.anthropic.com/v1/messages", data=body,
                     headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                              "content-type": "application/json"})
    try:
        with ur.urlopen(req, timeout=120) as r:
            resp = json.load(r)
        text = "".join(b.get("text", "") for b in resp["content"] if b.get("type") == "text")
        m = re.search(r"\[.*\]", text, re.S)
        parsed = {p["i"]: p for p in json.loads(m.group(0))} if m else {}
    except Exception as e:
        print(f"[startups] hn enrichment failed: {e}")
        return rows
    for i, r in enumerate(rows):
        p = parsed.get(i)
        if not p or not p.get("fits"):
            continue
        r = r.copy()
        r["company"] = p.get("company") or r["company"]
        r["title"] = p.get("title") or r["title"]
        r["locations"] = p.get("location") or r["locations"]
        out.append(r)
    return out


def fetch_hn_hiring(limit_comments: int = 400) -> list[dict]:
    d = json.loads(_get(f"{HN_ALGOLIA}/search_by_date?tags=story,author_whoishiring&query=who%20is%20hiring&hitsPerPage=1"))
    if not d.get("hits"):
        return []
    story_id = d["hits"][0]["objectID"]
    out = []
    page = 0
    while len(out) < limit_comments:
        c = json.loads(_get(f"{HN_ALGOLIA}/search_by_date?tags=comment,story_{story_id}&hitsPerPage=100&page={page}"))
        hits = c.get("hits", [])
        if not hits:
            break
        for h in hits:
            text = re.sub(r"<[^>]+>", " ", h.get("comment_text") or "")
            text = re.sub(r"\s+", " ", text).strip()
            if not text or len(text) < 60:
                continue
            if not re.search(r"intern|new grad|junior|entry.level", text, re.I):
                continue
            # company = leading token before | or —
            m = re.match(r"^([A-Za-z0-9 .&\-]{2,40}?)\s*[|—\-–]", text)
            company = (m.group(1).strip() if m else "").strip()
            urls = re.findall(r"https?://[^\s\"<>]+", h.get("comment_text") or "")
            out.append({"company": company or "HN posting", "title": "Intern/Junior (HN)",
                        "url": urls[0] if urls else f"https://news.ycombinator.com/item?id={h['objectID']}",
                        "locations": "", "source": "hn-hiring", "text": text[:1500]})
        page += 1
    return out


# ---------------------------------------------------------------- persist
def upsert(rows: list[dict]) -> int:
    conn = sqlite3.connect(DB)
    new = 0
    now = int(time.time())
    for r in rows:
        pid = f"{r['source']}:{r['company'].lower()}:{re.sub(r'[^a-z0-9]', '', r['title'].lower())[:60]}"
        if conn.execute("SELECT 1 FROM postings WHERE posting_id=?", (pid,)).fetchone():
            continue
        if conn.execute("SELECT 1 FROM postings WHERE url=?", (r["url"],)).fetchone():
            continue
        conn.execute("INSERT INTO postings VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (pid, r["source"], r["company"], r["title"], r.get("locations", ""),
                      r["url"], "", 0, 0, now, "new"))
        new += 1
    conn.commit()
    conn.close()
    return new


def run() -> dict:
    res = {}
    rows = fetch_offseason()
    res["offseason_new"] = upsert(rows)
    ycs = fetch_yc_companies()
    res["yc_companies"] = len(ycs)
    yc_jobs = []
    for c in ycs:
        yc_jobs += yc_job_urls(c)
    res["yc_jobs_found"] = len(yc_jobs)
    res["yc_new"] = upsert(yc_jobs)
    hn = enrich_hn(fetch_hn_hiring())
    res["hn_candidates"] = len(hn)
    res["hn_new"] = upsert(hn)
    return res


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
