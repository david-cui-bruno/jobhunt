import hashlib
import sqlite3
import time
import urllib.parse

from apply.jd import canonical_application_url


def canonical_posting_key(posting_id: str, url: str) -> str:
    canonical = canonical_application_url(url).split("#", 1)[0].rstrip("/")
    parsed = urllib.parse.urlparse(canonical)
    if parsed.netloc.lower().endswith("ashbyhq.com"):
        path = parsed.path.removesuffix("/application").rstrip("/")
        canonical = urllib.parse.urlunparse(
            (parsed.scheme, parsed.netloc.lower(), path, "", "", "")
        )
    elif "greenhouse.io" in parsed.netloc.lower():
        query = urllib.parse.parse_qs(parsed.query)
        token = (query.get("token") or [None])[0]
        board = (query.get("for") or [None])[0]
        if token:
            params = {"token": token}
            if board:
                params = {"for": board, "token": token}
            canonical = urllib.parse.urlunparse(
                (
                    parsed.scheme,
                    parsed.netloc.lower(),
                    parsed.path.rstrip("/"),
                    "",
                    urllib.parse.urlencode(params),
                    "",
                )
            )
    material = canonical or posting_id
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def posting_already_applied(conn: sqlite3.Connection, posting_id: str, url: str) -> bool:
    wanted = canonical_posting_key(posting_id, url)
    rows = conn.execute(
        "SELECT p.posting_id,p.url FROM applications a JOIN postings p USING(posting_id)"
    ).fetchall()
    return any(canonical_posting_key(row[0], row[1]) == wanted for row in rows)


def _active_posting_claimed(conn: sqlite3.Connection, posting_id: str, url: str) -> bool:
    wanted = canonical_posting_key(posting_id, url)
    rows = conn.execute(
        "SELECT posting_id,url FROM postings "
        "WHERE posting_id<>? AND status IN ('submitting','sprinting')",
        (posting_id,),
    ).fetchall()
    return any(canonical_posting_key(row[0], row[1]) == wanted for row in rows)


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
