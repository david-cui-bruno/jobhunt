import hashlib
import json
import sqlite3
from decimal import Decimal
from typing import Any, Dict, Optional

from compensation.models import CompensationResolution, EvidenceObservation, JobContext

SCHEMA = """
CREATE TABLE IF NOT EXISTS compensation_evidence (
  cache_key TEXT PRIMARY KEY,
  posting_id TEXT NOT NULL,
  company TEXT NOT NULL,
  normalized_role TEXT NOT NULL,
  location TEXT NOT NULL,
  employment_type TEXT NOT NULL,
  currency TEXT NOT NULL,
  period TEXT NOT NULL,
  resolved_amount TEXT NOT NULL,
  method TEXT NOT NULL,
  evidence_json TEXT NOT NULL,
  researched_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL
)
"""


def ensure_compensation_schema(conn: sqlite3.Connection) -> None:
    should_commit = not conn.in_transaction
    conn.execute(SCHEMA)
    if should_commit:
        conn.commit()


def store_resolution(conn: sqlite3.Connection, resolution: CompensationResolution) -> None:
    context = resolution.context
    conn.execute(
        """
        INSERT INTO compensation_evidence
        (cache_key, posting_id, company, normalized_role, location, employment_type, currency,
         period, resolved_amount, method, evidence_json, researched_at, expires_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(cache_key) DO UPDATE SET
            posting_id=excluded.posting_id,
            company=excluded.company,
            normalized_role=excluded.normalized_role,
            location=excluded.location,
            employment_type=excluded.employment_type,
            currency=excluded.currency,
            period=excluded.period,
            resolved_amount=excluded.resolved_amount,
            method=excluded.method,
            evidence_json=excluded.evidence_json,
            researched_at=excluded.researched_at,
            expires_at=excluded.expires_at
        """,
        (
            cache_key(context),
            context.posting_id,
            context.company,
            context.title,
            context.location,
            context.employment_type,
            context.currency,
            context.period,
            str(resolution.amount),
            resolution.method,
            _evidence_json(resolution),
            resolution.researched_at,
            resolution.expires_at,
        ),
    )
    conn.commit()


def load_resolution(
    conn: sqlite3.Connection, context: JobContext, now: int
) -> Optional[CompensationResolution]:
    row = conn.execute(
        """
        SELECT resolved_amount, method, evidence_json, researched_at, expires_at
        FROM compensation_evidence
        WHERE cache_key=? AND posting_id=? AND company=? AND normalized_role=? AND location=?
          AND employment_type=? AND currency=? AND period=? AND expires_at>?
        """,
        (
            cache_key(context),
            context.posting_id,
            context.company,
            context.title,
            context.location,
            context.employment_type,
            context.currency,
            context.period,
            now,
        ),
    ).fetchone()
    if row is None:
        return None
    evidence_data = json.loads(row[2])
    return CompensationResolution(
        context=context,
        amount=Decimal(row[0]),
        method=row[1],
        evidence=tuple(_observation(item) for item in evidence_data),
        researched_at=int(row[3]),
        expires_at=int(row[4]),
    )


def cache_key(context: JobContext) -> str:
    payload = json.dumps(_context_values(context), separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _context_values(context: JobContext) -> list:
    return [
        context.posting_id,
        context.company,
        context.title,
        context.location,
        context.employment_type,
        context.currency,
        context.period,
    ]


def _evidence_json(resolution: CompensationResolution) -> str:
    return json.dumps(
        [
            {
                "url": item.url,
                "domain": item.domain,
                "title": item.title,
                "low": _decimal_text(item.low),
                "high": _decimal_text(item.high),
                "point": _decimal_text(item.point),
                "currency": item.currency,
                "period": item.period,
                "source_kind": item.source_kind,
                "observed_at": item.observed_at,
            }
            for item in resolution.evidence
        ],
        sort_keys=True,
        separators=(",", ":"),
    )


def _decimal_text(value: Optional[Decimal]) -> Optional[str]:
    return None if value is None else str(value)


def _optional_decimal(value: Optional[str]) -> Optional[Decimal]:
    return None if value is None else Decimal(value)


def _observation(item: Dict[str, Any]) -> EvidenceObservation:
    return EvidenceObservation(
        url=item["url"],
        domain=item["domain"],
        title=item["title"],
        low=_optional_decimal(item["low"]),
        high=_optional_decimal(item["high"]),
        point=_optional_decimal(item["point"]),
        currency=item["currency"],
        period=item["period"],
        source_kind=item["source_kind"],
        observed_at=int(item["observed_at"]),
    )
