from __future__ import annotations

import hashlib
import re
import sqlite3
import time
import urllib.parse

from apply.oraclecloud_url import parse_oracle_posting_url


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

    parsed_oracle = parse_oracle_posting_url(url)
    if parsed_oracle:
        return f"oraclecloud:{parsed_oracle.host}:{parsed_oracle.site.lower()}:{parsed_oracle.job_id}"

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

    if _any_candidate_url_matches(conn, "1=1", (), wanted, applications=True):
        return "canonical posting already applied"
    if _dreamwork_applied_sibling_matches(conn, posting_id, url):
        return "canonical posting already applied"

    if _any_candidate_url_matches(
        conn,
        "p.posting_id<>? AND p.status IN ('submitting','sprinting')",
        (posting_id,),
        wanted,
    ):
        return "canonical posting already claimed"

    active_clause = "posting_id<>? AND status IN ('queued','tailoring','ready','manual')"
    if _has_postings_column(conn, "outcome"):
        active_clause += " AND COALESCE(outcome, '') NOT IN ('stale','submitted','deduplicated')"
    if _any_candidate_url_matches(
        conn,
        active_clause,
        (posting_id,),
        wanted,
    ):
        return "canonical posting already active"

    terminal_clause = "status IN ('submitted','skipped')"
    if _has_postings_column(conn, "outcome"):
        terminal_clause = "(outcome IN ('stale','submitted','deduplicated') OR " + terminal_clause + ")"
    if _any_candidate_url_matches(
        conn,
        f"p.posting_id<>? AND {terminal_clause}",
        (posting_id,),
        wanted,
    ):
        return "canonical posting already terminal"

    return None


def _any_candidate_url_matches(
    conn: sqlite3.Connection,
    where: str,
    params: tuple,
    wanted: str,
    applications: bool = False,
) -> bool:
    for row in _candidate_rows(conn, where, params, applications=applications):
        row_id = row[0]
        for candidate_url in row[1:]:
            if candidate_url and canonical_posting_key(row_id, candidate_url) == wanted:
                return True
    return False


def _candidate_rows(
    conn: sqlite3.Connection,
    where: str,
    params: tuple,
    applications: bool = False,
) -> list[tuple]:
    app_join = "JOIN applications a USING(posting_id)" if applications else ""
    resolution_join = ""
    resolved_expr = "NULL AS resolved_url"
    source_expr = "NULL AS source_url"
    if _has_table(conn, "posting_url_resolutions"):
        resolution_join = "LEFT JOIN posting_url_resolutions r USING(posting_id)"
        resolved_expr = "r.resolved_url AS resolved_url"
        source_expr = "r.source_url AS source_url"
    query = (
        f"SELECT p.posting_id,p.url,{resolved_expr},{source_expr} "
        f"FROM postings p {app_join} {resolution_join} WHERE {where}"
    )
    return conn.execute(query, params).fetchall()


def _dreamwork_applied_sibling_matches(conn: sqlite3.Connection, posting_id: str, url: str) -> bool:
    required_columns = {"company", "title", "source"}
    if not required_columns.issubset(_postings_columns(conn)):
        return False
    current = conn.execute(
        "SELECT company,title,source,url FROM postings WHERE posting_id=?",
        (posting_id,),
    ).fetchone()
    if current is None:
        return False
    if not _is_dreamwork_source(current[2], current[3]):
        return False
    if _is_dreamwork_url(url):
        return False
    company = _normalize_match_text(current[0])
    title = _normalize_match_text(current[1])
    if not company or not title:
        return False
    rows = conn.execute(
        "SELECT p.posting_id,p.company,p.title,p.source,p.url "
        "FROM applications a JOIN postings p USING(posting_id) "
        "WHERE p.posting_id<>?",
        (posting_id,),
    ).fetchall()
    for row in rows:
        if not _is_dreamwork_source(row[3], row[4]):
            continue
        if _normalize_match_text(row[1]) == company and _normalize_match_text(row[2]) == title:
            return True
    return False


def _is_dreamwork_source(source: str | None, url: str | None) -> bool:
    return source == "dreamwork-2027" or _is_dreamwork_url(url or "")


def _is_dreamwork_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower()
    return (host == "dreamworkhq.com" or host.endswith(".dreamworkhq.com")) and "/job/" in parsed.path


def _normalize_match_text(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def posting_already_applied(conn: sqlite3.Connection, posting_id: str, url: str) -> bool:
    return canonical_conflict_reason(conn, posting_id, url) == "canonical posting already applied"


def _active_posting_claimed(conn: sqlite3.Connection, posting_id: str, url: str) -> bool:
    return canonical_conflict_reason(conn, posting_id, url) == "canonical posting already claimed"


def _has_postings_column(conn: sqlite3.Connection, name: str) -> bool:
    return any(row[1] == name for row in conn.execute("PRAGMA table_info(postings)").fetchall())


def _postings_columns(conn: sqlite3.Connection) -> set[str]:
    return {row[1] for row in conn.execute("PRAGMA table_info(postings)").fetchall()}


def _has_table(conn: sqlite3.Connection | None, name: str) -> bool:
    if conn is None:
        return False
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone() is not None


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
