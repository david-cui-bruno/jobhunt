from __future__ import annotations

import sqlite3
from dataclasses import dataclass

ATS = "ashby"
POLICY_REVISION = "ashby-canary-v1"
INTERVAL_MINUTES = (180, 90, 45)
SPAM_BLOCK_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class AshbyState:
    enabled: bool
    tier: int
    consecutive_confirmed: int
    next_attempt_at: int
    blocked_until: int
    last_outcome: str


def ensure_lane_state(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ats_lane_state (
            ats TEXT PRIMARY KEY,
            enabled INTEGER NOT NULL DEFAULT 0,
            tier INTEGER NOT NULL DEFAULT 0,
            consecutive_confirmed INTEGER NOT NULL DEFAULT 0,
            next_attempt_at INTEGER NOT NULL DEFAULT 0,
            blocked_until INTEGER NOT NULL DEFAULT 0,
            last_outcome TEXT NOT NULL DEFAULT '',
            last_reason TEXT NOT NULL DEFAULT '',
            policy_revision TEXT NOT NULL DEFAULT 'ashby-canary-v1',
            updated_at INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO ats_lane_state (
            ats, enabled, tier, consecutive_confirmed, next_attempt_at,
            blocked_until, last_outcome, last_reason, policy_revision, updated_at
        ) VALUES (?, 0, 0, 0, 0, 0, '', '', ?, 0)
        """,
        (ATS, POLICY_REVISION),
    )
    conn.commit()


def _row_to_state(row: sqlite3.Row | tuple) -> AshbyState:
    return AshbyState(
        enabled=bool(row["enabled"] if isinstance(row, sqlite3.Row) else row[0]),
        tier=int(row["tier"] if isinstance(row, sqlite3.Row) else row[1]),
        consecutive_confirmed=int(row["consecutive_confirmed"] if isinstance(row, sqlite3.Row) else row[2]),
        next_attempt_at=int(row["next_attempt_at"] if isinstance(row, sqlite3.Row) else row[3]),
        blocked_until=int(row["blocked_until"] if isinstance(row, sqlite3.Row) else row[4]),
        last_outcome=str(row["last_outcome"] if isinstance(row, sqlite3.Row) else row[5]),
    )


def load_state(conn: sqlite3.Connection) -> AshbyState:
    ensure_lane_state(conn)
    row = conn.execute(
        """
        SELECT enabled, tier, consecutive_confirmed, next_attempt_at, blocked_until, last_outcome
        FROM ats_lane_state
        WHERE ats=?
        """,
        (ATS,),
    ).fetchone()
    if row is None:
        raise RuntimeError("ashby lane state missing")
    return _row_to_state(row)


def can_attempt(state: AshbyState, *, now: int) -> bool:
    return state.enabled and now >= state.next_attempt_at and now >= state.blocked_until


def set_enabled(conn: sqlite3.Connection, enabled: bool, *, now: int) -> AshbyState:
    ensure_lane_state(conn)
    conn.execute(
        """
        UPDATE ats_lane_state
        SET enabled=?, updated_at=?
        WHERE ats=?
        """,
        (1 if enabled else 0, now, ATS),
    )
    conn.commit()
    return load_state(conn)


def _is_confirmed(outcome: str, reason: str) -> bool:
    return outcome == "submitted" and reason.strip().lower() == "confirmed"


def _is_spam(reason: str) -> bool:
    return "spam" in reason.lower()


def _needs_answers(reason: str) -> bool:
    return "needs answers" in reason.lower()


def _tier_after_confirmation(streak: int) -> int:
    if streak >= 10:
        return 2
    if streak >= 3:
        return 1
    return 0


def record_result(conn: sqlite3.Connection, *, outcome: str, reason: str, now: int) -> AshbyState:
    state = load_state(conn)
    enabled = state.enabled
    tier = state.tier
    streak = state.consecutive_confirmed
    next_attempt_at = state.next_attempt_at
    blocked_until = state.blocked_until

    if _is_confirmed(outcome, reason):
        streak += 1
        tier = _tier_after_confirmation(streak)
        next_attempt_at = now + INTERVAL_MINUTES[tier] * 60
    elif _is_spam(reason):
        tier = max(0, tier - 1)
        streak = 0
        blocked_until = now + SPAM_BLOCK_SECONDS
    elif _needs_answers(reason):
        next_attempt_at = now
    else:
        enabled = False

    conn.execute(
        """
        UPDATE ats_lane_state
        SET enabled=?, tier=?, consecutive_confirmed=?, next_attempt_at=?, blocked_until=?,
            last_outcome=?, last_reason=?, policy_revision=?, updated_at=?
        WHERE ats=?
        """,
        (
            1 if enabled else 0,
            tier,
            streak,
            next_attempt_at,
            blocked_until,
            outcome,
            reason,
            POLICY_REVISION,
            now,
            ATS,
        ),
    )
    conn.commit()
    return load_state(conn)
