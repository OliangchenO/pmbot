"""Tests for closed-loop per-market accounting (pmbot.outcomes)."""

from datetime import datetime, timedelta, timezone

import pytest

from pmbot.metrics import MetricsStore
from pmbot.outcomes import MarketOutcome, build_market_outcomes


# ── helper ──

def _insert_fill(conn, ts, cid, market, side, token, price, size, **kw):
    conn.execute(
        "INSERT INTO fills (ts,cid,market,side,token,price,size,taker,exit,merged,fee) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (ts, cid, market, side, token, price, size,
         int(kw.get("taker", 0)), int(kw.get("exit", 0)),
         kw.get("merged", 0), kw.get("fee", 0)),
    )


def _insert_merge(conn, ts, cid, pairs):
    conn.execute("INSERT INTO merges (ts,cid,pairs) VALUES (?,?,?)",
                 (ts, cid, pairs))


# ── tests ──


def test_short_yes_with_mid_is_positive_value(tmp_path):
    """Long NO MTM from ending inventory snapshot → positive asset value."""
    store = MetricsStore(str(tmp_path / "test.db"))
    conn = store._conn
    ts = datetime(2026, 8, 1, 12, tzinfo=timezone.utc).timestamp()

    # Buy 10 NO at 0.60 → long NO = short YES
    _insert_fill(conn, ts, "sh2", "SH2", "NO", "n", 0.60, 10)
    # Ending inventory snapshot: unpaired=-10 (long NO), yes_mid=0.70
    # Mid comes from the live order book, stored directly in yes_mid column.
    # MTM = 10 * (1-0.70) = 3.00 → positive!
    ts_end = ts + 100
    conn.execute(
        "INSERT INTO inventory_snapshots (ts,cid,market,unpaired_shares,cost_basis,exposure_usd,status,yes_mid) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (ts_end, "sh2", "SH2", -10.0, 0.60, -3.0, "unpaired", 0.70),
    )
    conn.commit()

    outcomes = build_market_outcomes(conn, ts, ts + 3600)
    store.close()

    assert len(outcomes) == 1
    o = outcomes[0]
    # Ending snapshot uses yes_mid=0.70 → mid from live book
    # unpaired_signed=-10, mid=0.70 → MTM = 10 * (1-0.70) = 3.00
    # cash_pnl = -6.00 (the buy), carry_cost = 0 (no pre-window snapshot)
    # trading_pnl = 3.00 - 0 + (-6.00) = -3.00
    assert o.unpaired_mtm_usd == pytest.approx(3.00)
    assert o.unpaired_mtm_usd > 0.0
    assert o.state == "unpaired_marked"


def test_carry_in_pairs_merged_during_window_not_phantom_pnl(tmp_path):
    """Pre-window paired shares merged during window → $0 PnL contribution.

    Entering with 10 pairs (YES+NO) at open and merging them during the window
    must not show +$10 phantom PnL — the paired value already existed before the
    window.  Only the pair-cost loss (buy prices above/below mid) is window PnL.
    """
    store = MetricsStore(str(tmp_path / "test.db"))
    conn = store._conn
    ts_pre = datetime(2026, 8, 1, 11, tzinfo=timezone.utc).timestamp()  # pre-window
    ts_win = datetime(2026, 8, 1, 12, tzinfo=timezone.utc).timestamp()   # window start
    ts_mid = datetime(2026, 8, 1, 12, 30, tzinfo=timezone.utc).timestamp()

    # Pre-window: buy 10 YES @0.46 + 10 NO @0.52 — total cost $9.80 for 10 pairs
    _insert_fill(conn, ts_pre, "carry", "Carry", "YES", "cy", 0.46, 10)
    _insert_fill(conn, ts_pre, "carry", "Carry", "NO", "cn", 0.52, 10)
    # Pre-window snapshot: 10 YES, 10 NO = 10 pairs ($10 paired value)
    conn.execute(
        "INSERT INTO inventory_snapshots (ts,cid,market,unpaired_shares,cost_basis,exposure_usd,status,yes_mid,yes_shares,no_shares) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (ts_win - 60, "carry", "Carry", 0.0, None, 0.0, "flat", 0.50, 10.0, 10.0),
    )
    # Window: merge those 10 existing pairs (cash in $10)
    _insert_merge(conn, ts_mid, "carry", 10)
    # Ending snapshot: flat (no unpaired, no mid needed)
    conn.commit()

    outcomes = build_market_outcomes(conn, ts_win, ts_win + 3600)
    store.close()

    assert len(outcomes) == 1
    o = outcomes[0]
    assert o.state == "realized"
    # merge_cash = $10, carry_paired_value = min(10,10) = $10
    # paired_value at end = 0 (all merged), ending_value = 0
    # trading_pnl = 0 - 0 - 10 + 10 = 0  (no phantom profit)
    assert o.trading_pnl_usd == pytest.approx(0.0, abs=1e-9)
    assert "carry_in_pos_10.0y_10.0n" in o.evidence_flags


def test_stale_book_ts_makes_market_incomplete(tmp_path):
    """book_updated_ts older than snapshot_max_age_seconds → incomplete.

    A snapshot from a market we stopped sampling days ago has a book_updated_ts
    far behind end_ts.  That mid is stale and must NOT anchor MTM — the market
    is incomplete even though yes_mid is present and in range.
    """
    store = MetricsStore(str(tmp_path / "test.db"))
    conn = store._conn
    ts = datetime(2026, 8, 1, 12, tzinfo=timezone.utc).timestamp()

    _insert_fill(conn, ts, "stale", "Stale", "YES", "sy", 0.50, 10)
    # Snapshot within window, but book_updated_ts is 30 hours old (>12h max).
    stale_book_ts = ts - 30 * 3600
    conn.execute(
        "INSERT INTO inventory_snapshots (ts,cid,market,unpaired_shares,cost_basis,exposure_usd,status,yes_mid,book_updated_ts) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (ts + 60, "stale", "Stale", 10.0, 0.50, 5.0, "unpaired", 0.55, stale_book_ts),
    )
    conn.commit()

    outcomes = build_market_outcomes(conn, ts, ts + 3600,
                                    snapshot_max_age_seconds=12 * 3600)
    store.close()

    assert len(outcomes) == 1
    o = outcomes[0]
    assert o.state == "incomplete"
    assert "unpaired_no_persistent_mid" in o.evidence_flags


def test_book_ts_zero_guarded_as_incomplete(tmp_path):
    """book_updated_ts <= 0 (uninitialized) → market incomplete.

    Old snapshots migrated from before the book_updated_ts column was added
    have book_updated_ts = 0 (or NULL → 0 via COALESCE).  Guard must treat
    this the same as a stale timestamp — the market is incomplete.
    """
    store = MetricsStore(str(tmp_path / "test.db"))
    conn = store._conn
    ts = datetime(2026, 8, 1, 12, tzinfo=timezone.utc).timestamp()

    _insert_fill(conn, ts, "zero", "Zero", "YES", "zy", 0.50, 10)
    # book_updated_ts = 0 (overnight data before column existed)
    conn.execute(
        "INSERT INTO inventory_snapshots (ts,cid,market,unpaired_shares,cost_basis,exposure_usd,status,yes_mid,book_updated_ts) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (ts + 60, "zero", "Zero", 10.0, 0.50, 5.0, "unpaired", 0.55, 0),
    )
    conn.commit()

    outcomes = build_market_outcomes(conn, ts, ts + 3600,
                                    snapshot_max_age_seconds=12 * 3600)
    store.close()

    assert len(outcomes) == 1
    o = outcomes[0]
    assert o.state == "incomplete"


def test_carry_in_cost_unknown_rejects_outcome(tmp_path):
    """cost_basis missing on unpaired carry-in → market is incomplete.

    Without the true carry-in cost, ending_value would attribute the full
    MTM change to the window.  The accounting engine must refuse to produce
    a tradeable PnL, marking the market incomplete.
    """
    store = MetricsStore(str(tmp_path / "test.db"))
    conn = store._conn
    ts_pre = datetime(2026, 8, 1, 10, tzinfo=timezone.utc).timestamp()
    ts_win = datetime(2026, 8, 1, 12, tzinfo=timezone.utc).timestamp()

    # Pre-window: long YES 10 with cost_basis=NULL
    conn.execute(
        "INSERT INTO inventory_snapshots (ts,cid,market,unpaired_shares,cost_basis,exposure_usd,status,yes_mid,yes_shares,no_shares) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (ts_pre, "cost_unk", "CostUnk", 10.0, None, 5.0, "unpaired", 0.55, 10.0, 0.0),
    )
    # Window: exit the position at 0.60
    _insert_fill(conn, ts_win, "cost_unk", "CostUnk", "NO", "nu", 0.60, 10, taker=1, exit=1)
    # Ending snapshot: flat
    conn.execute(
        "INSERT INTO inventory_snapshots (ts,cid,market,unpaired_shares,cost_basis,exposure_usd,status,yes_mid,yes_shares,no_shares,book_updated_ts) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (ts_win + 60, "cost_unk", "CostUnk", 0.0, None, 0.0, "flat", 0.55, 0.0, 0.0, ts_win + 60),
    )
    conn.commit()

    outcomes = build_market_outcomes(conn, ts_win, ts_win + 3600,
                                    snapshot_max_age_seconds=12 * 3600)
    store.close()

    assert len(outcomes) == 1
    o = outcomes[0]
    assert o.state == "incomplete"
    assert "carry_in_cost_unknown" in o.evidence_flags
    assert o.trading_pnl_usd is None


def test_per_day_cycle_count_multi_day_same_cid(tmp_path):
    """Each UTC day a CID closes is one cycle — not one-per-CID-per-window.

    A CID that closes on 3 different UTC days in the lookback must show 3
    closed cycles in the report.
    """
    store = MetricsStore(str(tmp_path / "test.db"))
    ts1 = datetime(2026, 8, 1, 12, tzinfo=timezone.utc).timestamp()
    ts2 = datetime(2026, 8, 3, 12, tzinfo=timezone.utc).timestamp()
    ts3 = datetime(2026, 8, 5, 12, tzinfo=timezone.utc).timestamp()

    # Day 1: pair and merge
    _insert_fill(store._conn, ts1, "multi", "Multi", "YES", "my", 0.50, 2)
    _insert_fill(store._conn, ts1, "multi", "Multi", "NO", "mn", 0.50, 2)
    _insert_merge(store._conn, ts1, "multi", 2)
    # Day 3: another cycle
    _insert_fill(store._conn, ts2, "multi", "Multi", "YES", "my", 0.60, 2)
    _insert_fill(store._conn, ts2, "multi", "Multi", "NO", "mn", 0.40, 2)
    _insert_merge(store._conn, ts2, "multi", 2)
    # Day 5: third cycle
    _insert_fill(store._conn, ts3, "multi", "Multi", "YES", "my", 0.55, 2)
    _insert_fill(store._conn, ts3, "multi", "Multi", "NO", "mn", 0.45, 2)
    _insert_merge(store._conn, ts3, "multi", 2)
    store._conn.commit()

    report = store.outcome_report("2026-08-05", lookback_days=14)
    store.close()

    # One CID, three cycles on three different days.
    assert report["complete_utc_days"] >= 3
    assert report["total_closed_cycles"] >= 3
    assert report["cids_with_min_cycles"] >= 1
