"""Mirror active jobhunt postings to Kith for deployed, read-only referral review.

The local tracker remains the source of truth. This exporter only reads SQLite,
upserts the current queued/ready/failed rows, and removes rows from older sync
versions so stale or filtered-out postings do not remain in the deployed queue.
It refuses to publish an empty snapshot unless --allow-empty is explicit.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DB = ROOT / "out" / "tracker.db"
ACTIVE_STATUSES = ("queued", "tailoring", "sprinting", "ready", "submitting", "failed")
BATCH_SIZE = 100


def load_env_file(path: Path) -> None:
    """Load only missing simple KEY=VALUE entries from a local env file."""
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def required_env(name: str, *aliases: str) -> str:
    for key in (name, *aliases):
        value = os.environ.get(key, "").strip()
        if value:
            return value
    raise RuntimeError(f"Missing {name} (or one of: {', '.join(aliases)})")


def read_active_postings(path: Path = DB) -> list[dict[str, Any]]:
    if not path.exists():
        raise RuntimeError(f"Tracker database not found: {path}")
    uri = f"file:{path}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT posting_id, source, company, title, locations, url,
                       CASE status
                           WHEN 'tailoring' THEN 'queued'
                           WHEN 'sprinting' THEN 'queued'
                           WHEN 'submitting' THEN 'ready'
                           ELSE status
                       END AS status,
                       first_seen
                 FROM postings
                WHERE status IN (?,?,?,?,?,?)
                ORDER BY CASE status WHEN 'ready' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END,
                         first_seen DESC""",
            ACTIVE_STATUSES,
        ).fetchall()
    return [dict(row) for row in rows if row["posting_id"] and row["company"] and row["title"] and row["url"]]


class SupabaseRest:
    def __init__(self, base_url: str, service_key: str) -> None:
        self.endpoint = base_url.rstrip("/") + "/rest/v1/job_queue_snapshot"
        self.headers = {
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
            "Content-Type": "application/json",
        }

    def request(self, method: str, url: str, payload: Any = None, prefer: str | None = None) -> None:
        headers = dict(self.headers)
        if prefer:
            headers["Prefer"] = prefer
        body = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                response.read()
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace")
            raise RuntimeError(f"Supabase {method} failed ({error.code}): {detail}") from error


def sync(rows: list[dict[str, Any]], owner_id: str, client: SupabaseRest, allow_empty: bool = False) -> dict[str, Any]:
    if not rows and not allow_empty:
        raise RuntimeError("Refusing to publish an empty queue. Re-run with --allow-empty if intentional.")
    version = uuid.uuid4().hex
    synced_at = datetime.now(timezone.utc).isoformat()
    payload = [
        {
            "owner_id": owner_id,
            "posting_id": row["posting_id"],
            "source": row["source"],
            "company": row["company"],
            "title": row["title"],
            "locations": row["locations"],
            "url": row["url"],
            "status": row["status"],
            "first_seen": row["first_seen"],
            "sync_version": version,
            "synced_at": synced_at,
        }
        for row in rows
    ]
    for start in range(0, len(payload), BATCH_SIZE):
        client.request(
            "POST",
            client.endpoint + "?on_conflict=owner_id,posting_id",
            payload[start : start + BATCH_SIZE],
            "resolution=merge-duplicates,return=minimal",
        )
    query = urllib.parse.urlencode({"owner_id": f"eq.{owner_id}", "sync_version": f"neq.{version}"})
    client.request("DELETE", f"{client.endpoint}?{query}", prefer="return=minimal")
    return {"synced": len(payload), "sync_version": version}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path(os.environ.get("JOBHUNT_DB_PATH", DB)))
    parser.add_argument("--env-file", type=Path, default=None)
    parser.add_argument("--allow-empty", action="store_true")
    args = parser.parse_args(argv)
    if args.env_file:
        load_env_file(args.env_file)
    rows = read_active_postings(args.db)
    if not rows and not args.allow_empty:
        raise RuntimeError("Refusing to publish an empty queue. Re-run with --allow-empty if intentional.")
    base_url = required_env("KITH_SUPABASE_URL", "NEXT_PUBLIC_SUPABASE_URL")
    service_key = required_env("KITH_SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_SERVICE_ROLE_KEY")
    owner_id = required_env("KITH_OWNER_ID", "OWNER_ID")
    result = sync(rows, owner_id, SupabaseRest(base_url, service_key), allow_empty=args.allow_empty)
    print(json.dumps({"active_rows": len(rows), **result}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"queue sync failed: {error}", file=sys.stderr)
        raise SystemExit(1)
