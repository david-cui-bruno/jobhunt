from __future__ import annotations

import math
import sqlite3
from typing import Optional

from submission.lanes import POLICIES, classify_url, lane_for

KNOWN_ATS = frozenset().union(*(policy.ats for policy in POLICIES)) - {"other"}


def _ats_name(value: Optional[str]) -> str:
    ats = (value or "").strip().lower()
    if not ats or ats not in KNOWN_ATS:
        return "other"
    return ats


def _percentile(values: list[int], percentile: float) -> Optional[int]:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    if percentile == 0.5:
        mid = len(ordered) // 2
        if len(ordered) % 2:
            return ordered[mid]
        return int(round((ordered[mid - 1] + ordered[mid]) / 2))
    rank = max(1, math.ceil(percentile * len(ordered)))
    return ordered[min(rank - 1, len(ordered) - 1)]


def attempt_metrics(conn: sqlite3.Connection, *, since: int) -> list[dict]:
    """Aggregate submission attempt health by ATS since a unix timestamp."""
    count_rows = conn.execute(
        "SELECT ats, COALESCE(outcome, 'unknown') AS outcome, "
        "COUNT(*) AS attempts, "
        "SUM(CASE WHEN confirmation_observed=1 OR outcome='submitted' THEN 1 ELSE 0 END) AS confirmed "
        "FROM submission_attempts WHERE started_at >= ? "
        "GROUP BY ats, COALESCE(outcome, 'unknown')",
        (since,),
    ).fetchall()
    by_ats: dict[str, dict] = {}
    for row in count_rows:
        ats = _ats_name(row["ats"] if isinstance(row, sqlite3.Row) else row[0])
        outcome = (row["outcome"] if isinstance(row, sqlite3.Row) else row[1]) or "unknown"
        attempts = int(row["attempts"] if isinstance(row, sqlite3.Row) else row[2])
        confirmed = int(row["confirmed"] if isinstance(row, sqlite3.Row) else row[3])
        bucket = by_ats.setdefault(
            ats,
            {
                "ats": ats,
                "attempts": 0,
                "confirmed": 0,
                "submitted": 0,
                "manual": 0,
                "failed": 0,
                "unknown": 0,
                "durations": [],
            },
        )
        bucket["attempts"] += attempts
        if outcome in bucket:
            bucket[outcome] += attempts
        else:
            bucket["unknown"] += attempts
        bucket["confirmed"] += confirmed

    duration_rows = conn.execute(
        "SELECT ats, duration_ms FROM submission_attempts "
        "WHERE started_at >= ? AND duration_ms IS NOT NULL",
        (since,),
    ).fetchall()
    for row in duration_rows:
        ats = _ats_name(row["ats"] if isinstance(row, sqlite3.Row) else row[0])
        duration = row["duration_ms"] if isinstance(row, sqlite3.Row) else row[1]
        bucket = by_ats.setdefault(
            ats,
            {
                "ats": ats,
                "attempts": 0,
                "confirmed": 0,
                "submitted": 0,
                "manual": 0,
                "failed": 0,
                "unknown": 0,
                "durations": [],
            },
        )
        if duration is not None:
            bucket["durations"].append(int(duration))

    result = []
    for bucket in by_ats.values():
        attempts = bucket["attempts"]
        durations = bucket.pop("durations")
        bucket["confirmation_rate"] = round(bucket["confirmed"] / attempts, 3) if attempts else 0.0
        bucket["p50_duration_ms"] = _percentile(durations, 0.5)
        bucket["p95_duration_ms"] = _percentile(durations, 0.95)
        result.append(bucket)
    return sorted(result, key=lambda row: (-row["attempts"], row["ats"]))


def queue_metrics(conn: sqlite3.Connection) -> list[dict]:
    """Count current queued work by shared ATS lane classification."""
    metrics = {
        policy.name: {"lane": policy.name, "depth": 0, "automatic": policy.automatic}
        for policy in POLICIES
    }
    rows = conn.execute(
        "SELECT url, status FROM postings WHERE status IN ('queued', 'ready', 'manual')"
    ).fetchall()
    for row in rows:
        url = row["url"] if isinstance(row, sqlite3.Row) else row[0]
        ats, lane = classify_url(url or "")
        if not ats:
            lane = lane_for("other")
        metrics[lane.name]["depth"] += 1
    return [metrics[policy.name] for policy in POLICIES if metrics[policy.name]["depth"]]
