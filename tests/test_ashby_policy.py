import sqlite3
from pathlib import Path

import pytest

from submission.ashby_policy import (
    can_attempt,
    ensure_lane_state,
    load_state,
    record_result,
    set_enabled,
)


def policy_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    ensure_lane_state(conn)
    return conn


def test_ashby_starts_disabled() -> None:
    state = load_state(policy_db())
    assert state.enabled is False
    assert state.tier == 0
    assert state.consecutive_confirmed == 0
    assert can_attempt(state, now=1_000) is False


def test_three_confirmations_advance_from_180_to_90_minutes() -> None:
    conn = policy_db()
    set_enabled(conn, True, now=1_000)
    state = record_result(conn, outcome="submitted", reason="confirmed", now=1_000)
    assert state.next_attempt_at == 1_000 + 180 * 60
    state = record_result(conn, outcome="submitted", reason="confirmed", now=state.next_attempt_at)
    state = record_result(conn, outcome="submitted", reason="confirmed", now=state.next_attempt_at)
    assert state.tier == 1
    assert state.consecutive_confirmed == 3
    assert state.next_attempt_at == 1_000 + 180 * 60 * 2 + 90 * 60


def test_ten_confirmations_advance_from_90_to_45_minutes() -> None:
    conn = policy_db()
    state = set_enabled(conn, True, now=1_000)
    for _ in range(10):
        state = record_result(conn, outcome="submitted", reason="confirmed", now=max(1_000, state.next_attempt_at))
    assert state.tier == 2
    assert state.consecutive_confirmed == 10
    assert state.next_attempt_at == 1_000 + 180 * 60 * 2 + 90 * 60 * 7 + 45 * 60


def test_spam_rejection_blocks_24_hours_and_slows_one_tier() -> None:
    conn = policy_db()
    set_enabled(conn, True, now=1_000)
    for now in (1_000, 11_800, 22_600):
        record_result(conn, outcome="submitted", reason="confirmed", now=now)
    state = record_result(conn, outcome="manual", reason="flagged as possible spam", now=30_000)
    assert state.blocked_until == 30_000 + 24 * 60 * 60
    assert state.tier == 0
    assert state.consecutive_confirmed == 0
    assert can_attempt(state, now=state.blocked_until - 1) is False


def test_unconfirmed_click_disables_lane_for_uncertainty_pause() -> None:
    conn = policy_db()
    set_enabled(conn, True, now=1_000)
    state = record_result(conn, outcome="manual", reason="clicked but confirmation uncertain", now=1_000)
    assert state.enabled is False
    assert state.last_outcome == "manual"
    assert can_attempt(state, now=1_001) is False


def test_needs_answers_keeps_tier_and_requires_new_posting_without_pause() -> None:
    conn = policy_db()
    set_enabled(conn, True, now=1_000)
    for now in (1_000, 11_800, 22_600):
        state = record_result(conn, outcome="submitted", reason="confirmed", now=now)
    assert state.tier == 1
    state = record_result(conn, outcome="manual", reason="needs answers before pre-click", now=30_000)
    assert state.enabled is True
    assert state.tier == 1
    assert state.consecutive_confirmed == 3
    assert state.next_attempt_at == 30_000 + 90 * 60
    assert can_attempt(state, now=30_000) is False


def test_record_result_owns_immediate_transaction_before_reading_file_backed_state(tmp_path: Path) -> None:
    db_path = tmp_path / "tracker.db"
    conn = sqlite3.connect(db_path)
    ensure_lane_state(conn)
    set_enabled(conn, True, now=1_000)

    statements: list[str] = []
    conn.set_trace_callback(statements.append)

    record_result(conn, outcome="submitted", reason="confirmed", now=1_000)

    begin_index = next(
        index for index, statement in enumerate(statements) if statement.upper().startswith("BEGIN")
    )
    state_read_index = next(
        index
        for index, statement in enumerate(statements)
        if "FROM ats_lane_state" in statement and "SELECT" in statement.upper()
    )
    assert statements[begin_index].upper().startswith("BEGIN IMMEDIATE")
    assert begin_index < state_read_index


def test_record_result_reuses_caller_transaction_without_committing_it(tmp_path: Path) -> None:
    db_path = tmp_path / "tracker.db"
    conn = sqlite3.connect(db_path)
    ensure_lane_state(conn)
    set_enabled(conn, True, now=1_000)

    conn.execute("BEGIN IMMEDIATE")
    state = record_result(conn, outcome="submitted", reason="confirmed", now=1_000)

    assert conn.in_transaction is True
    assert state.consecutive_confirmed == 1
    conn.rollback()
    assert load_state(conn).consecutive_confirmed == 0


def test_record_result_rolls_back_owned_transaction_on_update_exception(tmp_path: Path) -> None:
    db_path = tmp_path / "tracker.db"
    conn = sqlite3.connect(db_path)
    ensure_lane_state(conn)
    set_enabled(conn, True, now=1_000)
    conn.execute(
        """
        CREATE TRIGGER fail_ashby_policy_update
        BEFORE UPDATE ON ats_lane_state
        WHEN NEW.last_reason = 'explode'
        BEGIN
            SELECT RAISE(ABORT, 'boom');
        END
        """
    )
    conn.commit()

    with pytest.raises(sqlite3.DatabaseError, match="boom"):
        record_result(conn, outcome="manual", reason="explode", now=2_000)

    assert conn.in_transaction is False
    state = load_state(conn)
    assert state.enabled is True
    assert state.last_outcome == ""
