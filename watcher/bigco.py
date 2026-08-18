"""Big-company direct board watcher.

Polls stable public JSON job feeds for companies whose postings are poorly
covered by GitHub internship lists. No authenticated APIs, no browser scraping.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DB = ROOT / "out" / "tracker.db"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 jobhunt-bigco-watcher"
_LAST_HIT: dict[str, float] = {}
_HOST_GAP = 0.8


@dataclass(frozen=True)
class CompanyFeed:
    company: str
    kind: str
    slug: str


ATS_URLS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
    "lever": "https://api.lever.co/v0/postings/{slug}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{slug}",
}

# Current stable public JSON where known. Companies without a public JSON feed or
# requiring auth/heavy scraping are intentionally omitted until a stable feed is verified.
FEEDS: tuple[CompanyFeed, ...] = (
    CompanyFeed("Stripe", "greenhouse", "stripe"),
    CompanyFeed("Databricks", "greenhouse", "databricks"),
    CompanyFeed("OpenAI", "ashby", "openai"),
    CompanyFeed("Figma", "greenhouse", "figma"),
    CompanyFeed("Notion", "ashby", "notion"),
    CompanyFeed("Airbnb", "greenhouse", "airbnb"),
    CompanyFeed("Brex", "greenhouse", "brex"),
    CompanyFeed("Vercel", "ashby", "vercel"),
    CompanyFeed("Linear", "ashby", "linear"),
    CompanyFeed("Jane Street", "greenhouse", "janestreet"),
    CompanyFeed("Hudson River Trading", "greenhouse", "wehrtyou"),
    CompanyFeed("Jump Trading", "greenhouse", "jumptrading"),
    CompanyFeed("DRW", "greenhouse", "drweng"),
    CompanyFeed("IMC", "greenhouse", "imc"),
    CompanyFeed("Optiver", "greenhouse", "optiverus"),
    CompanyFeed("Akuna Capital", "greenhouse", "akunacapital"),
    CompanyFeed("Five Rings", "greenhouse", "fiveringsllc"),
)

TITLE_INCLUDE_RE = re.compile(
    r"software|\bswe\b|machine learning|\bml\b|artificial intelligence|\bai\b|"
    r"backend|front[ -]?end|full[ -]?stack|infrastructure|platform|data engineer|"
    r"quantitative developer|quant developer|technology intern", re.I)
SEASON_RE = re.compile(r"intern|internship|co[- ]?op|new grad|university grad|entry level|early career|2027", re.I)
TITLE_SKIP_RE = re.compile(
    r"senior|staff|principal|manager|director|lead|phd|doctoral|postdoc|research scientist|"
    r"product manager|program manager|designer|sales|marketing|recruit|legal|finance|account|"
    r"mechanical|electrical|hardware|civil|security clearance", re.I)


def _get(url: str, timeout: int = 25) -> str:
    host = urllib.parse.urlparse(url).netloc
    wait = _LAST_HIT.get(host, 0) + _HOST_GAP - time.time()
    if wait > 0:
        time.sleep(wait)
    _LAST_HIT[host] = time.time()
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def feed_url(feed: CompanyFeed) -> str:
    if feed.kind in ATS_URLS:
        return ATS_URLS[feed.kind].format(slug=feed.slug)
    if feed.kind == "google":
        return "https://careers.google.com/api/v3/search/?q=software%20engineer%20intern%202027&location=United%20States"
    if feed.kind == "microsoft":
        return "https://gcsservices.careers.microsoft.com/search/api/v1/search?lc=United%20States&rt=Individual%20Contributor&l=en_us&pg=1&pgSz=200&o=Recent"
    if feed.kind == "amazon":
        return "https://www.amazon.jobs/en/search.json?base_query=software%20engineer%20intern&country=USA"
    raise ValueError(f"unknown feed kind: {feed.kind}")


def title_ok(title: str) -> bool:
    t = title or ""
    return bool(TITLE_INCLUDE_RE.search(t) and SEASON_RE.search(t) and not TITLE_SKIP_RE.search(t))


def _loc_text(value) -> str:
    if isinstance(value, dict):
        return str(value.get("name") or value.get("display") or value.get("city") or "")
    if isinstance(value, list):
        return "; ".join(_loc_text(x) for x in value if x)[:200]
    return str(value or "")


def _job_fields(kind: str, j: dict) -> tuple[str, str, str, str]:
    if kind == "greenhouse":
        return str(j.get("id") or ""), str(j.get("title") or "").strip(), _loc_text(j.get("location")), str(j.get("absolute_url") or "")
    if kind == "lever":
        cats = j.get("categories") or {}
        loc = str(cats.get("location") or "")
        if str(j.get("workplaceType") or "") == "remote":
            loc = (loc + " Remote").strip()
        return str(j.get("id") or ""), str(j.get("text") or "").strip(), loc, str(j.get("hostedUrl") or j.get("applyUrl") or "")
    if kind == "ashby":
        locs = [str(j.get("location") or "")]
        locs += [str((s or {}).get("location") or "") for s in (j.get("secondaryLocations") or [])]
        if j.get("isRemote"):
            locs.append("Remote")
        return str(j.get("id") or ""), str(j.get("title") or "").strip(), "; ".join(x for x in locs if x), str(j.get("jobUrl") or j.get("applyUrl") or "")
    if kind == "google":
        job_id = str(j.get("id") or j.get("job_id") or j.get("requisition_id") or "")
        url = str(j.get("apply_url") or j.get("url") or j.get("job_url") or "")
        if not url and job_id:
            url = f"https://careers.google.com/jobs/results/{job_id}/"
        return job_id, str(j.get("title") or "").strip(), _loc_text(j.get("locations") or j.get("location")), url
    if kind == "microsoft":
        job_id = str(j.get("jobId") or j.get("id") or "")
        return job_id, str(j.get("title") or "").strip(), _loc_text(j.get("locations") or j.get("primaryLocation")), str(j.get("url") or (f"https://jobs.careers.microsoft.com/global/en/job/{job_id}" if job_id else ""))
    if kind == "amazon":
        job_id = str(j.get("id") or j.get("job_path") or "")
        path = str(j.get("job_path") or "")
        return job_id, str(j.get("title") or "").strip(), _loc_text(j.get("location") or j.get("normalized_location")), str(j.get("url_next_step") or ("https://www.amazon.jobs" + path if path.startswith("/") else ""))
    return "", "", "", ""


def _jobs_from_payload(kind: str, text: str) -> list:
    data = json.loads(text)
    if isinstance(data, list):
        return data
    for key in ("jobs", "postings", "searchResults", "results"):
        jobs = data.get(key) if isinstance(data, dict) else None
        if isinstance(jobs, list):
            return jobs
    return []


def postings_from_payload(feed: CompanyFeed, text: str) -> list:
    from watcher import watch
    out = []
    for j in _jobs_from_payload(feed.kind, text):
        if not isinstance(j, dict) or j.get("isListed") is False:
            continue
        job_id, title, loc, url = _job_fields(feed.kind, j)
        if not job_id or not title or not url or not title_ok(title):
            continue
        out.append(watch.Posting(
            source="bigco", company=feed.company, title=title, locations=loc[:200], url=url,
            posting_id=f"bigco:{feed.kind}:{feed.slug or feed.company.lower().replace(' ', '')}:{job_id}",
        ))
    return out


def fetch_feed(feed: CompanyFeed) -> list:
    return postings_from_payload(feed, _get(feed_url(feed)))


def run() -> dict:
    from watcher import watch
    DB.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB)
    watch.init_db(conn)
    errors: dict[str, str] = {}
    source_counts: dict[str, int] = {}
    new_total = 0
    companies_polled = 0
    for feed in FEEDS:
        try:
            postings = fetch_feed(feed)
            companies_polled += 1
            source_counts[feed.company] = len(postings)
            new_total += len(watch.upsert(conn, postings))
        except Exception as e:
            errors[feed.company] = f"{type(e).__name__}: {e}"
    conn.close()
    return {"companies_polled": companies_polled, "new_postings": new_total, "source_counts": source_counts, "errors": errors}


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
