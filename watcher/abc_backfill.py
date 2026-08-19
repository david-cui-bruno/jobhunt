"""One-time-ish backfill: seed abc_companies from the TechCrunch archive.

David 2026-08-19: "backfill the last 2-3 years of A-D rounds" so the scout
starts from hundreds of funded startups instead of the few weeks of RSS
window. TechCrunch exposes its full archive via the public WordPress JSON API
(wp-json/wp/v2/posts?search=...), which tolerates polite sequential paging.
Crunchbase News and VentureBeat block wp-json (403), so TC is the archive
source; the a16z portfolio (already in the scout) covers older rounds too.

Flow: page search results for 'Series A/B/C/D' since CUTOFF -> the scout's
own claude-haiku extractor -> store_companies (INSERT OR IGNORE, so re-runs
are cheap and safe) -> resolve in batches with the careers-page-first
resolver. Safe to interrupt; state lives in the DB.

Run:  python3 -m watcher.abc_backfill            # discover + resolve
      python3 -m watcher.abc_backfill --no-resolve  # discover only
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
import urllib.parse

from watcher.abc_startups import (
    DB, _get, extract_companies, init_db, resolve_pending, store_companies,
)

CUTOFF = "2023-09-01T00:00:00"  # ~3 years back
TC_API = ("https://techcrunch.com/wp-json/wp/v2/posts"
          "?search={q}&after={after}&per_page=100&page={page}"
          "&_fields=title,date,excerpt,link&orderby=date&order=desc")
QUERIES = ('"Series A"', '"Series B"', '"Series C"', '"Series D"')
BATCH = 40  # news items per haiku call (same shape the scout uses)


def _strip(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")).strip()


def fetch_archive() -> list[dict]:
    items, seen = [], set()
    for q in QUERIES:
        for page in range(1, 40):  # wp caps at 100/page; 40 pages is plenty
            url = TC_API.format(q=urllib.parse.quote(q), after=CUTOFF, page=page)
            try:
                raw = _get(url, timeout=30)
            except Exception as e:
                # wp-json returns 400 past the last page; anything else is a
                # real (but non-fatal) fetch problem
                if "400" not in str(e):
                    print(f"[backfill] {q} p{page}: {e}", file=sys.stderr)
                break
            try:
                posts = json.loads(raw)
            except Exception:
                break
            if not isinstance(posts, list) or not posts:
                break
            for p in posts:
                title = _strip((p.get("title") or {}).get("rendered", ""))
                link = str(p.get("link") or "")
                if not title or link in seen:
                    continue
                seen.add(link)
                items.append({
                    "title": title,
                    "summary": _strip((p.get("excerpt") or {}).get("rendered", ""))[:600],
                    "date": str(p.get("date") or "")[:10],
                    "via": "tc-archive",
                })
            print(f"[backfill] {q} page {page}: {len(items)} items total", flush=True)
            if len(posts) < 100:
                break
    return items


def run(resolve: bool = True) -> dict:
    DB.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB)
    init_db(conn)
    items = fetch_archive()
    print(f"[backfill] {len(items)} archive items; extracting rounds...", flush=True)
    new = 0
    for i in range(0, len(items), BATCH):
        rows = extract_companies(items[i:i + BATCH])
        n = store_companies(conn, rows)
        new += n
        print(f"[backfill] extract {i}-{i+BATCH}: +{n} companies (total new {new})",
              flush=True)
        time.sleep(1)
    out = {"items": len(items), "companies_new": new}
    if resolve:
        # careers-page-first resolution until the discovered pool drains
        rounds = 0
        while rounds < 40:
            r = resolve_pending(conn, limit=25)
            rounds += 1
            print(f"[backfill] resolve round {rounds}: {r}", flush=True)
            if r["attempted"] == 0:
                break
        out["resolve_rounds"] = rounds
    for status, cnt in conn.execute(
            "SELECT status, COUNT(*) FROM abc_companies GROUP BY status"):
        out[f"status_{status}"] = cnt
    conn.close()
    return out


if __name__ == "__main__":
    print(json.dumps(run(resolve="--no-resolve" not in sys.argv), indent=2))
