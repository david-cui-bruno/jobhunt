from __future__ import annotations

import hashlib
import re
import sqlite3
import time
import urllib.parse


TRACKING_QUERY_NAMES = frozenset({
    "gh_src",
    "lever-source",
    "ref",
    "referrer",
    "source",
    "sourceid",
})


def _identity_query(parsed: urllib.parse.ParseResult) -> str:
    pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    kept = [
        (key, value)
        for key, value in pairs
        if not key.lower().startswith("utm_")
        and key.lower() not in TRACKING_QUERY_NAMES
    ]
    return urllib.parse.urlencode(kept)


def _first_query_value(query: dict[str, list[str]], name: str) -> str | None:
    return (query.get(name) or [None])[0]


def _canonical_identity_material(posting_id: str, url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)

    ashby_id = _first_query_value(query, "ashby_jid")
    if ashby_id and re.fullmatch(r"[0-9a-f-]{36}", ashby_id, re.I):
        labels = [part for part in parsed.netloc.lower().split(".")
                  if part not in {"www", "jobs", "careers"}]
        if labels:
            org = urllib.parse.quote(labels[0], safe="")
            job_id = urllib.parse.quote(ashby_id, safe="")
            return f"https://jobs.ashbyhq.com/{org}/{job_id}"

    greenhouse_id = _first_query_value(query, "gh_jid")
    if greenhouse_id and re.fullmatch(r"\d+", greenhouse_id):
        token = urllib.parse.quote(greenhouse_id, safe="")
        return f"greenhouse:token:{token}"

    host = parsed.netloc.lower()
    path = parsed.path.rstrip("/")
    if host.endswith("dreamworkhq.com"):
        # The old watcher removed ?utm_source but left &utm_campaign in the
        # path, producing a 404 URL for the same Dreamwork job UUID.
        path = re.sub(r"&utm_[^/]*$", "", path, flags=re.I)
    if host.endswith("ashbyhq.com"):
        return urllib.parse.urlunparse(
            (parsed.scheme, host, path.removesuffix("/application"), "", "", "")
        )

    if "greenhouse.io" in host:
        path_match = re.search(r"/(?:jobs/)?(\d+)$", path)
        if path_match:
            return f"greenhouse:token:{urllib.parse.quote(path_match.group(1), safe='')}"
        token = _first_query_value(query, "token")
        if token and re.fullmatch(r"\d+", token):
            return f"greenhouse:token:{urllib.parse.quote(token, safe='')}"

    return urllib.parse.urlunparse(
        (parsed.scheme, host, path, "", _identity_query(parsed), "")
    ) or posting_id


def canonical_posting_key(posting_id: str, url: str) -> str:
    material = _canonical_identity_material(posting_id, url)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def canonical_conflict_reason(conn: sqlite3.Connection, posting_id: str, url: str) -> str | None:
    wanted = canonical_posting_key(posting_id, url)

    rows = conn.execute(
        "SELECT p.posting_id,p.url FROM applications a JOIN postings p USING(posting_id)"
    ).fetchall()
    if any(canonical_posting_key(row[0], row[1]) == wanted for row in rows):
        return "canonical posting already applied"

    rows = conn.execute(
        "SELECT posting_id,url FROM postings "
        "WHERE posting_id<>? AND status IN ('submitting','sprinting')",
        (posting_id,),
    ).fetchall()
    if any(canonical_posting_key(row[0], row[1]) == wanted for row in rows):
        return "canonical posting already claimed"

    terminal_clause = "status IN ('submitted','skipped')"
    if _has_postings_column(conn, "outcome"):
        terminal_clause = "(outcome IN ('stale','submitted','deduplicated') OR " + terminal_clause + ")"
    rows = conn.execute(
        "SELECT posting_id,url FROM postings "
        f"WHERE posting_id<>? AND {terminal_clause}",
        (posting_id,),
    ).fetchall()
    if any(canonical_posting_key(row[0], row[1]) == wanted for row in rows):
        return "canonical posting already terminal"

    return None


def posting_already_applied(conn: sqlite3.Connection, posting_id: str, url: str) -> bool:
    return canonical_conflict_reason(conn, posting_id, url) == "canonical posting already applied"


def _active_posting_claimed(conn: sqlite3.Connection, posting_id: str, url: str) -> bool:
    return canonical_conflict_reason(conn, posting_id, url) == "canonical posting already claimed"


def _has_postings_column(conn: sqlite3.Connection, name: str) -> bool:
    return any(row[1] == name for row in conn.execute("PRAGMA table_info(postings)").fetchall())


def claim_submission(
    conn: sqlite3.Connection,
    *,
    posting_id: str,
    url: str,
    from_status: str = "ready",
) -> str:
    """Atomically reserve one canonical posting's external-submission slot."""
    if from_status not in {"ready", "sprinting"}:
        raise ValueError(f"invalid submission source status: {from_status}")
    try:
        conn.execute("BEGIN IMMEDIATE")
        if posting_already_applied(conn, posting_id, url):
            conn.rollback()
            return "already_applied"
        if _active_posting_claimed(conn, posting_id, url):
            conn.rollback()
            return "posting_claimed"
        changed = conn.execute(
            "UPDATE postings SET status='submitting', last_attempt_at=? "
            "WHERE posting_id=? AND status=?",
            (int(time.time()), posting_id, from_status),
        ).rowcount
        conn.commit()
        return "claimed" if changed == 1 else "claim_lost"
    except Exception:
        conn.rollback()
        raise
