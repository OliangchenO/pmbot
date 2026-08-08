"""Audit semantics for price-exit lifecycle metrics."""

import sqlite3

from pmbot.metrics import MetricsStore


def test_exit_decision_is_not_recorded_as_realized_pnl(tmp_path):
    store = MetricsStore(str(tmp_path / "metrics.db"))
    store.record_exit_event(
        "cid-1", "market", decision_id="d-1", event="decision",
        action="same_side_sell", reason="same_side_executable",
        requested_size=10.0, filled_size=0.0, remaining_size=10.0,
        basis=0.55, target_offset=0.01, limit_price=0.56,
        expected_net_pnl_per_share=0.02, phase="profit", mode="shadow",
    )

    row = store._conn.execute(
        "SELECT decision_id,event,filled_size,actual_avg_price,"
        "realized_net_pnl_per_share,mode FROM exit_events").fetchone()

    assert row == ("d-1", "decision", 0.0, None, None, "shadow")


def test_legacy_exit_events_table_is_migrated_before_its_new_index(tmp_path):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE exit_events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, cid TEXT, market TEXT)")
    conn.commit()
    conn.close()

    store = MetricsStore(str(path))

    columns = {row[1] for row in store._conn.execute("PRAGMA table_info(exit_events)")}
    assert {"decision_id", "event", "action"} <= columns
