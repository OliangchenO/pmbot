"""Tests for metrics store."""

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pmbot.gamma import Market
from pmbot.metrics import MetricsStore


def test_report_totals_use_the_requested_utc_day(tmp_path):
    store = MetricsStore(str(tmp_path / "metrics.db"))
    old = datetime(2026, 7, 30, tzinfo=timezone.utc)
    new = old + timedelta(days=1)
    store.record_fill({
        "ts": old.timestamp() + 60, "cid": "old", "market": "Old",
        "side": "YES", "token": "yes", "price": 0.40, "size": 10,
    })
    store.record_merge("old", 10, ts=old.timestamp() + 120)
    store.record_fill({
        "ts": new.timestamp() + 60, "cid": "new", "market": "New",
        "side": "YES", "token": "yes", "price": 0.40, "size": 20,
    })
    store.record_realized_reward("2026-07-30", 1.25)
    store.record_realized_reward("2026-07-31", 2.50)

    assert store.reward_totals("2026-07-30")["realized_24h"] == 1.25
    assert store.trading_pnl_ledger("2026-07-30")["realized_24h"] == 6.0
    store.close()


def test_metrics_daily_report(tmp_path):
    db = tmp_path / "test.db"
    store = MetricsStore(str(db))
    store.record_merge("cid1", 10.0)
    store.record_est_reward(1.5)
    store.record_fill({
        "ts": time.time(), "cid": "cid1", "market": "Test",
        "side": "YES", "token": "y", "price": 0.47, "size": 10,
    })
    store.record_fill({
        "ts": time.time(), "cid": "cid2", "market": "Fee market",
        "side": "NO", "token": "n", "price": 0.24, "size": 40, "fee": 0.96,
    })
    report = store.daily_report()
    store.close()
    assert report["spread_capture_usd"] == 10.0
    assert report["est_rewards_usd"] == 1.5
    assert report["maker_fills"] == 2
    assert report["fees_usd"] == -0.96


def test_recent_fills_and_trades_log(tmp_path):
    db = tmp_path / "test.db"
    log_path = tmp_path / "trades.jsonl"
    store = MetricsStore(str(db), trades_log=str(log_path))
    ts = time.time()
    store.record_fill({
        "ts": ts, "cid": "cid1", "market": "Rain tomorrow?",
        "side": "YES", "token": "y", "price": 0.45, "size": 20,
    })
    store.record_fill({
        "ts": ts + 1, "cid": "cid1", "market": "Rain tomorrow?",
        "side": "NO", "token": "n", "price": 0.52, "size": 20, "taker": True,
    })
    fills = store.recent_fills(limit=10)
    assert len(fills) == 2
    assert fills[0]["taker"] is True
    assert fills[1]["taker"] is False
    store.close()
    lines = log_path.read_text().strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["side"] == "YES"

def test_sum_earnings_parses_clob_total_shape():
    # Exact shape returned by GET /rewards/user/total (one row per asset).
    rows = [{
        "date": "2026-06-17T00:00:00Z",
        "asset_address": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
        "maker_address": "0xabc",
        "earnings": 6.539251,
        "asset_rate": 0.999791,
    }]
    assert abs(MetricsStore._sum_earnings(rows) - 6.539251 * 0.999791) < 1e-9
    # Multiple collateral assets sum; {"data": [...]} wrapper also handled.
    multi = {"data": [
        {"earnings": 2.0, "asset_rate": 1.0},
        {"earnings": 3.0, "asset_rate": 0.5},
    ]}
    assert MetricsStore._sum_earnings(multi) == 3.5
    assert MetricsStore._sum_earnings([]) == 0.0
    assert MetricsStore._sum_earnings(None) == 0.0


def test_fetch_realized_rewards_records_total(tmp_path):
    store = MetricsStore(str(tmp_path / "test.db"))

    class FakeClient:
        def get_total_earnings_for_user_for_day(self, date):
            return [{"earnings": 8.02109, "asset_rate": 0.999601}]

    total = store.fetch_realized_rewards(FakeClient(), date="2026-06-16")
    assert abs(total - 8.02109 * 0.999601) < 1e-9
    report = store.daily_report("2026-06-16")
    store.close()
    assert abs(report["realized_rewards_usd"] - total) < 1e-9


def test_fetch_market_realized_rewards_records_official_condition_ids(tmp_path):
    store = MetricsStore(str(tmp_path / "test.db"))

    class FakeClient:
        def get_earnings_for_user_for_day(self, date):
            return {"data": [
                {"condition_id": "cid-a", "earnings": 2.0, "asset_rate": 0.5},
                {"condition_id": "cid-b", "earnings": 1.25, "asset_rate": 1.0},
            ]}

    assert store.fetch_market_realized_rewards(FakeClient(), "2026-06-16") == 2
    rows = store._conn.execute(
        "SELECT cid,realized,source FROM market_rewards ORDER BY cid").fetchall()
    store.close()
    assert rows == [("cid-a", 1.0, "clob_rewards_user"),
                    ("cid-b", 1.25, "clob_rewards_user")]


def test_fetch_realized_rewards_keeps_prior_value_on_error(tmp_path):
    store = MetricsStore(str(tmp_path / "test.db"))
    store.record_realized_reward("2026-06-16", 8.0)

    class BoomClient:
        def get_total_earnings_for_user_for_day(self, date):
            raise RuntimeError("401")

    assert store.fetch_realized_rewards(BoomClient(), date="2026-06-16") == 0.0
    # The transient failure must NOT overwrite the previously recorded value.
    report = store.daily_report("2026-06-16")
    store.close()
    assert report["realized_rewards_usd"] == 8.0


def test_performance_report(tmp_path):
    db = tmp_path / "test.db"
    store = MetricsStore(str(db))
    ts = time.time()
    store.record_fill({
        "ts": ts, "cid": "cid1", "market": "Good market",
        "side": "YES", "token": "y", "price": 0.48, "size": 10, "merged": 10,
    })
    store.record_hedge("cid1", 0.50, 10)
    store.record_markout({
        "ts": ts + 30, "fill_ts": ts, "cid": "cid1", "market": "Good market",
        "horizon": 300, "markout": 0.01,
    })
    store.record_markout({
        "ts": ts + 31, "fill_ts": ts, "cid": "cid1", "market": "Good market",
        "horizon": 30, "markout": 0.005,
    })
    with store._lock:
        minute = int(ts) // 60
        store._conn.execute(
            "INSERT INTO uptime (minute_ts, cid, in_band) VALUES (?,?,?)",
            (minute, "cid1", 1),
        )
        store._conn.execute(
            "INSERT INTO uptime (minute_ts, cid, in_band) VALUES (?,?,?)",
            (minute, "cid1", 0),
        )
        store._conn.commit()
    report = store.performance_report()
    store.close()
    assert len(report["markets"]) == 1
    m = report["markets"][0]
    assert m["maker_fills"] == 1
    assert m["merged_pairs"] == 10
    assert m["hedge_cost_usd"] == 5.0
    assert m["markout_cents"] == 1.0
    assert m["uptime_pct"] == 50.0


def test_net_shadow_inputs_are_market_scoped_and_charge_negative_markout(tmp_path):
    """P2.1 must never borrow another market's measured loss or activity."""
    store = MetricsStore(str(tmp_path / "test.db"))
    ts = time.time()
    store.record_fill({"ts": ts, "cid": "A", "market": "A", "side": "YES",
                       "token": "ay", "price": 0.45, "size": 10})
    store.record_fill({"ts": ts, "cid": "B", "market": "B", "side": "YES",
                       "token": "by", "price": 0.45, "size": 50})
    store.record_markout({"ts": ts, "fill_ts": ts, "cid": "A", "market": "A",
                          "horizon": 300, "markout": -0.02})
    with store._lock:
        store._conn.execute("INSERT INTO uptime (minute_ts,cid,in_band) VALUES (?,?,?)",
                            (int(ts) // 60, "A", 1))
        store._conn.commit()
    inputs = store.net_shadow_inputs(lookback_hours=1, now=ts)
    store.close()

    assert inputs["A"]["maker_fill_count"] == 1
    assert inputs["A"]["markout_cost_per_hour"] == 0.02
    assert inputs["B"]["maker_fill_count"] == 1
    assert inputs["B"]["markout_samples"] == 0


def test_net_shadow_inputs_use_explicit_market_reward_realization(tmp_path):
    """Only an official cid-attributed reward may calibrate a market's pool."""
    store = MetricsStore(str(tmp_path / "test.db"))
    now = time.time()
    with store._lock:
        store._conn.execute("INSERT INTO reward_samples (minute_ts,cid,est_usd) VALUES (?,?,?)",
                            (int(now) // 60, "A", 2.0))
        store._conn.commit()
    store.record_market_realized_reward(
        datetime.now(timezone.utc).strftime("%Y-%m-%d"), "A", 1.5, "official")

    inputs = store.net_shadow_inputs(lookback_hours=1, now=now)
    store.close()

    assert inputs["A"]["reward_realization"] == 0.75
    assert inputs["A"]["reward_samples"] == 1


def test_net_shadow_snapshot_preserves_distinct_legacy_and_shadow_rankings(tmp_path):
    """The report needs both rankings to make P2.1 comparable after a scan."""
    store = MetricsStore(str(tmp_path / "test.db"))
    scanned_at = datetime(2026, 8, 1, tzinfo=timezone.utc).timestamp()
    a = Market("A", "A", "ay", "an", 1, 2, 240, 100, 1, 0.01, None, False,
               score=2.0, net_shadow_score=0.1,
               net_shadow_inputs={"insufficient_sample": False})
    b = Market("B", "B", "by", "bn", 1, 2, 120, 100, 1, 0.01, None, False,
               score=1.0, net_shadow_score=0.2,
               net_shadow_inputs={"insufficient_sample": True})
    store.record_net_shadow_snapshot([a, b], scanned_at, {"top_n": 1})
    report = store.net_shadow_report("2026-08-01")
    store.close()

    assert [row["cid"] for row in report["legacy_top"]] == ["A"]
    assert [row["cid"] for row in report["shadow_top"]] == ["B"]
    assert report["status"] == "ok"


def test_performance_report_marks_missing_shadow_scan_data_explicitly(tmp_path):
    """An empty history is uncertainty, not evidence that old and new picks agree."""
    store = MetricsStore(str(tmp_path / "test.db"))
    report = store.performance_report("2026-08-01")
    store.close()

    assert report["shadow_selection"]["status"] == "no_shadow_scan_data"


def test_performance_report_attributes_market_cashflow_and_estimated_reward(tmp_path):
    """A market report must expose only its own auditable cashflows and estimates."""
    store = MetricsStore(str(tmp_path / "test.db"))
    ts = time.time()
    store.record_fill({"ts": ts, "cid": "cid1", "market": "Market one",
                       "side": "YES", "token": "yes", "price": 0.46, "size": 10})
    store.record_fill({"ts": ts, "cid": "cid1", "market": "Market one",
                       "side": "NO", "token": "no", "price": 0.55, "size": 10,
                       "taker": True})
    store.record_merge("cid1", 10)
    store.record_reward_sample("cid1", 0.20)

    report = store.performance_report()
    store.close()

    market = report["markets"][0]
    assert abs(market["buy_cost_usd"] - 10.10) < 1e-9
    assert abs(market["exit_proceeds_usd"] - 0.0) < 1e-9
    assert abs(market["trading_pnl_usd"] - (-0.10)) < 1e-9
    assert abs(market["est_rewards_usd"] - 0.20) < 1e-9
    assert abs(market["net_pnl_est_usd"] - 0.10) < 1e-9


def test_performance_report_includes_latest_market_inventory_snapshot(tmp_path):
    store = MetricsStore(str(tmp_path / "test.db"))
    ts = time.time()
    store.record_inventory_snapshot(
        "cid1", "Market one", unpaired_shares=12.0, cost_basis=0.47,
        exposure_usd=5.64, status="unpaired", ts=ts,
    )
    store.record_inventory_snapshot(
        "cid1", "Market one", unpaired_shares=0.0, cost_basis=None,
        exposure_usd=0.0, status="flat", ts=ts + 1,
    )

    report = store.performance_report()
    store.close()

    market = report["markets"][0]
    assert market["inventory_status"] == "flat"
    assert market["unpaired_shares"] == 0.0
    assert market["unpaired_cost_basis"] is None
    assert market["inventory_exposure_usd"] == 0.0


def test_performance_report_exposes_latest_inventory_event_without_inference(tmp_path):
    store = MetricsStore(str(tmp_path / "test.db"))
    ts = time.time()
    store.record_fill({
        "ts": ts, "cid": "cid1", "market": "Market one",
        "side": "YES", "token": "yes", "price": 0.52, "size": 5,
        "exit": True,
    })
    store.record_inventory_snapshot(
        "cid1", "Market one", unpaired_shares=0.0, cost_basis=None,
        exposure_usd=0.0, status="flat", ts=ts + 1,
    )

    report = store.performance_report()
    store.close()

    market = report["markets"][0]
    assert market["last_inventory_event"] == "exit"
    assert market["inventory_status"] == "flat"


def test_performance_report_proves_inventory_terminal_states_from_event_and_snapshot(tmp_path):
    """Only enough post-inventory quantity plus a flat observation proves a terminal state."""
    store = MetricsStore(str(tmp_path / "test.db"))
    ts = time.time()

    # A confirmed merge of the complete observed excess proves pairing.
    store.record_inventory_snapshot("paired", "Paired", unpaired_shares=5.0,
                                    cost_basis=0.46, exposure_usd=2.3,
                                    status="unpaired", ts=ts)
    store.record_merge("paired", 5.0, ts=ts + 1)
    store.record_inventory_snapshot("paired", "Paired", unpaired_shares=0.0,
                                    cost_basis=None, exposure_usd=0.0,
                                    status="flat", ts=ts + 2)

    # A reduce-only exit of the complete observed excess proves an exit.
    store.record_inventory_snapshot("exited", "Exited", unpaired_shares=-4.0,
                                    cost_basis=0.54, exposure_usd=-2.16,
                                    status="unpaired", ts=ts)
    store.record_fill({"ts": ts + 1, "cid": "exited", "market": "Exited",
                       "side": "NO", "token": "no", "price": 0.50, "size": 4,
                       "exit": True})
    store.record_inventory_snapshot("exited", "Exited", unpaired_shares=0.0,
                                    cost_basis=None, exposure_usd=0.0,
                                    status="flat", ts=ts + 2)

    # A complete forced complement buy, followed by flat inventory, proves a hedge.
    store.record_inventory_snapshot("hedged", "Hedged", unpaired_shares=3.0,
                                    cost_basis=0.49, exposure_usd=1.47,
                                    status="unpaired", ts=ts)
    store.record_hedge("hedged", 0.50, 3.0, ts=ts + 1)
    store.record_inventory_snapshot("hedged", "Hedged", unpaired_shares=0.0,
                                    cost_basis=None, exposure_usd=0.0,
                                    status="flat", ts=ts + 2)

    # Flat without an observed disposition is deliberately left unresolved.
    store.record_inventory_snapshot("unknown", "Unknown", unpaired_shares=2.0,
                                    cost_basis=0.40, exposure_usd=0.8,
                                    status="unpaired", ts=ts)
    store.record_inventory_snapshot("unknown", "Unknown", unpaired_shares=0.0,
                                    cost_basis=None, exposure_usd=0.0,
                                    status="flat", ts=ts + 2)

    report = store.performance_report()
    store.close()
    markets = {m["cid"]: m for m in report["markets"]}
    assert markets["paired"]["inventory_terminal_status"] == "paired"
    assert markets["exited"]["inventory_terminal_status"] == "exit"
    assert markets["hedged"]["inventory_terminal_status"] == "hedged"
    assert markets["unknown"]["inventory_terminal_status"] == "unresolved"


def test_performance_report_separates_market_reward_facts_from_account_total(tmp_path):
    """Only explicitly attributed rewards produce a per-market realized payout."""
    store = MetricsStore(str(tmp_path / "test.db"))
    today = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).strftime("%Y-%m-%d")
    store.record_reward_sample("attributed", 0.20)
    store.record_reward_sample("unattributed", 0.10)
    store.record_realized_reward(today, 0.25)  # official account-level daily total
    store.record_market_realized_reward(today, "attributed", 0.15, source="official_detail")

    report = store.performance_report(today)
    store.close()

    markets = {m["cid"]: m for m in report["markets"]}
    attributed = markets["attributed"]
    assert attributed["realized_rewards_usd"] == 0.15
    assert attributed["reward_attribution_status"] == "attributed"
    assert abs(attributed["reward_calibration_ratio"] - 0.75) < 1e-9

    unattributed = markets["unattributed"]
    assert unattributed["realized_rewards_usd"] is None
    assert unattributed["reward_attribution_status"] == "account_total_only"
    assert unattributed["reward_calibration_ratio"] is None


def test_performance_report_marks_cashflow_mixed_with_carry_in_inventory(tmp_path):
    """A merge funded by pre-day tokens must not be presented as today's selection cashflow."""
    store = MetricsStore(str(tmp_path / "test.db"))
    prior_ts = __import__("datetime").datetime(2026, 7, 31, 23, 0,
                                                 tzinfo=__import__("datetime").timezone.utc).timestamp()
    day_ts = __import__("datetime").datetime(2026, 8, 1, 1, 0,
                                               tzinfo=__import__("datetime").timezone.utc).timestamp()
    for side, token, price in (("YES", "yes", 0.46), ("NO", "no", 0.52)):
        store.record_fill({"ts": prior_ts, "cid": "carry", "market": "Carry",
                           "side": side, "token": token, "price": price, "size": 10})
    store.record_merge("carry", 10, ts=day_ts)

    # This comparison market opened and merged entirely on the reporting day.
    for side, token, price in (("YES", "yes", 0.47), ("NO", "no", 0.51)):
        store.record_fill({"ts": day_ts, "cid": "today", "market": "Today",
                           "side": side, "token": token, "price": price, "size": 10})
    store.record_merge("today", 10, ts=day_ts + 1)

    report = store.performance_report("2026-08-01")
    store.close()
    markets = {m["cid"]: m for m in report["markets"]}

    carry = markets["carry"]
    assert carry["cashflow_attribution_status"] == "mixed_with_carry_in"
    assert carry["carry_in_paired_shares"] == 10.0
    assert carry["cross_day_merge_pairs_upper_bound"] == 10.0
    assert carry["selection_cashflow_usd"] is None

    today = markets["today"]
    assert today["cashflow_attribution_status"] == "same_day_cashflow"
    assert today["carry_in_paired_shares"] == 0.0
    assert today["selection_cashflow_usd"] == today["trading_pnl_usd"]


def test_reward_calibration_report_keeps_unattributed_market_days_inconclusive(tmp_path):
    """A daily calibration ratio exists only when both estimate and market fact exist."""
    store = MetricsStore(str(tmp_path / "test.db"))
    day = "2026-07-31"
    minute = int(__import__("datetime").datetime(2026, 7, 31, 12,
                                                   tzinfo=__import__("datetime").timezone.utc).timestamp()) // 60
    with store._lock:
        store._conn.executemany(
            "INSERT INTO reward_samples (minute_ts,cid,est_usd) VALUES (?,?,?)",
            [(minute, "attributed", 0.10), (minute + 1, "attributed", 0.20),
             (minute, "unattributed", 0.30)],
        )
        store._conn.executemany(
            "INSERT INTO uptime (minute_ts,cid,in_band) VALUES (?,?,?)",
            [(minute, "attributed", 1), (minute + 1, "attributed", 0)],
        )
        ts = minute * 60
        store._conn.executemany(
            "INSERT INTO recovery_events (ts,cid,event,reason,unpaired,recovery_path) "
            "VALUES (?,?,?,?,?,?)",
            [(ts, "attributed", "skip", "unknown_cost_basis", 10, "passive"),
             (ts + 1, "attributed", "skip", "unknown_cost_basis", 10, "passive")],
        )
        store._conn.commit()
    store.record_market_realized_reward(day, "attributed", 0.15, "official_detail")

    report = store.reward_calibration_report(days=1, end_date=day)
    store.close()
    rows = {r["cid"]: r for r in report["market_days"]}

    attributed = rows["attributed"]
    assert attributed["status"] == "calibrated"
    assert abs(attributed["estimated_usd"] - 0.30) < 1e-9
    assert abs(attributed["realized_usd"] - 0.15) < 1e-9
    assert abs(attributed["calibration_ratio"] - 0.5) < 1e-9
    assert attributed["uptime_samples"] == 2
    assert attributed["uptime_pct"] == 50.0
    assert attributed["recovery_skips"] == 2
    assert attributed["recovery_skip_reasons"] == {"unknown_cost_basis": 2}
    assert attributed["guard_interruptions_status"] == "not_recorded"

    unattributed = rows["unattributed"]
    assert unattributed["status"] == "unattributed"
    assert unattributed["realized_usd"] is None
    assert unattributed["calibration_ratio"] is None


def test_reward_totals_all_time_and_24h(tmp_path):
    store = MetricsStore(str(tmp_path / "test.db"))
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    store.record_realized_reward("2026-01-01", 5.0)   # old day
    store.record_realized_reward(today, 2.25)         # today
    store.record_est_reward(3.0)                      # today (est)
    out = store.reward_totals()
    store.close()
    assert abs(out["realized_total"] - 7.25) < 1e-9
    assert abs(out["realized_24h"] - 2.25) < 1e-9
    assert abs(out["est_total"] - 3.0) < 1e-9
    assert abs(out["est_24h"] - 3.0) < 1e-9


def test_reward_sample_records_per_minute_per_market(tmp_path):
    store = MetricsStore(str(tmp_path / "test.db"))
    store.record_reward_sample("cidA", 0.01)
    store.record_reward_sample("cidB", 0.02)
    store.record_reward_sample("cidA", float("nan"))  # NaN must be dropped
    rows = store._conn.execute(
        "SELECT cid, est_usd FROM reward_samples ORDER BY cid").fetchall()
    store.close()
    assert rows == [("cidA", 0.01), ("cidB", 0.02)]


def test_reward_rate_recent_and_by_market(tmp_path):
    store = MetricsStore(str(tmp_path / "test.db"))
    now_min = int(time.time()) // 60
    # 10 minutes of accrual: cidA $0.01/min, cidB $0.02/min.
    with store._lock:
        for i in range(10):
            m = now_min - i
            store._conn.execute(
                "INSERT INTO reward_samples (minute_ts, cid, est_usd) VALUES (?,?,?)",
                (m, "cidA", 0.01))
            store._conn.execute(
                "INSERT INTO reward_samples (minute_ts, cid, est_usd) VALUES (?,?,?)",
                (m, "cidB", 0.02))
        store._conn.commit()

    rate = store.reward_rate_recent(60)
    # 10 distinct minutes, $0.30 total -> $0.03/min -> $1.80/hr.
    assert rate["minutes"] == 10
    assert abs(rate["usd"] - 0.30) < 1e-9
    assert abs(rate["usd_per_hr"] - 1.80) < 1e-9

    by_mkt = store.reward_rate_by_market(now_min - 9)
    store.close()
    assert abs(by_mkt["cidA"]["usd"] - 0.10) < 1e-9
    assert abs(by_mkt["cidA"]["usd_per_hr"] - 0.60) < 1e-9
    assert abs(by_mkt["cidB"]["usd_per_hr"] - 1.20) < 1e-9


def test_reward_rate_recent_empty_window(tmp_path):
    store = MetricsStore(str(tmp_path / "test.db"))
    rate = store.reward_rate_recent(60)
    store.close()
    assert rate == {"usd": 0.0, "minutes": 0, "usd_per_hr": 0.0}


def test_hedge_pnl_uses_maker_basis(tmp_path):
    store = MetricsStore(str(tmp_path / "test.db"))
    ts = time.time()
    # We hold a YES maker leg bought at 0.55; basis for cid1 = 0.55.
    store.record_fill({
        "ts": ts, "cid": "cid1", "market": "M", "side": "YES",
        "token": "y", "price": 0.55, "size": 10,
    })
    # Forced hedge buys the NO complement at 0.50 -> pair cost 1.05 -> loss 0.05/sh.
    store.record_hedge("cid1", 0.50, 10)
    out = store.hedge_pnl_totals()
    store.close()
    # 10 * (1 - 0.50 - 0.55) = -0.5
    assert abs(out["pnl_total"] - (-0.5)) < 1e-9
    assert abs(out["pnl_24h"] - (-0.5)) < 1e-9
    assert abs(out["spend_total"] - 5.0) < 1e-9
    assert out["shares_total"] == 10


def test_trading_pnl_ledger_reconciles_cashflows(tmp_path):
    store = MetricsStore(str(tmp_path / "test.db"))
    ts = time.time()
    # Assemble one 10-pair batch: buy YES @0.46 and NO @0.56 (cost 1.02/pair),
    # then merge 10 pairs ($1 each). Net = 10*1 - (4.6 + 5.6) = -0.20.
    store.record_fill({"ts": ts, "cid": "c", "market": "M", "side": "YES",
                       "token": "y", "price": 0.46, "size": 10})
    store.record_fill({"ts": ts, "cid": "c", "market": "M", "side": "NO",
                       "token": "n", "price": 0.56, "size": 10, "taker": True})
    store.record_merge("c", 10)
    # A reduce-only exit sells 2 shares @0.48 (cash in), and a fee is charged.
    store.record_fill({"ts": ts, "cid": "c", "market": "M", "side": "YES",
                       "token": "y", "price": 0.48, "size": 2, "exit": True,
                       "fee": 0.01})
    # Inventory mark for the mark-to-market line.
    store.record_equity(100.0, 3.5)
    out = store.trading_pnl_ledger()
    store.close()
    # merges 10 + sells 0.96 - buys 10.20 - fees 0.01 = +0.75
    assert abs(out["realized_total"] - 0.75) < 1e-9
    assert abs(out["realized_24h"] - 0.75) < 1e-9
    assert abs(out["inventory_usd"] - 3.5) < 1e-9
    assert abs(out["mtm_total"] - (0.75 + 3.5)) < 1e-9


def test_trading_pnl_ledger_empty(tmp_path):
    store = MetricsStore(str(tmp_path / "test.db"))
    out = store.trading_pnl_ledger()
    store.close()
    assert out == {"realized_total": 0.0, "realized_24h": 0.0,
                   "inventory_usd": 0.0, "mtm_total": 0.0}


def test_inception_date_prunes_and_blocks(tmp_path):
    db = tmp_path / "test.db"
    # Seed pre-inception rows without the floor.
    seed = MetricsStore(str(db))
    from datetime import datetime, timezone
    old_ts = datetime(2026, 6, 10, tzinfo=timezone.utc).timestamp()
    new_ts = datetime(2026, 6, 15, tzinfo=timezone.utc).timestamp()
    seed.record_fill({"ts": old_ts, "cid": "c", "market": "M", "side": "YES",
                      "token": "y", "price": 0.5, "size": 10})
    seed.record_fill({"ts": new_ts, "cid": "c", "market": "M", "side": "YES",
                      "token": "y", "price": 0.5, "size": 10})
    seed.record_realized_reward("2026-06-10", 5.0)
    seed.record_realized_reward("2026-06-15", 4.0)
    with seed._lock:
        seed._conn.execute(
            "INSERT INTO reward_samples (minute_ts, cid, est_usd) VALUES (?,?,?)",
            (int(old_ts) // 60, "c", 0.01))
        seed._conn.execute(
            "INSERT INTO reward_samples (minute_ts, cid, est_usd) VALUES (?,?,?)",
            (int(new_ts) // 60, "c", 0.02))
        seed._conn.commit()
    seed.close()

    # Reopen with an inception floor: pre-Jun-14 rows are pruned on startup.
    store = MetricsStore(str(db), inception_date="2026-06-14")
    assert store._conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 1
    assert store._conn.execute(
        "SELECT COUNT(*) FROM reward_samples").fetchone()[0] == 1
    assert store.reward_totals()["realized_total"] == 4.0

    # A backfill must never (re)write a pre-inception date.
    class FakeClient:
        def get_total_earnings_for_user_for_day(self, date):
            return [{"earnings": 99.0, "asset_rate": 1.0}]

    out = store.backfill_realized_rewards(FakeClient(), days=10)
    store.close()
    assert all(d >= "2026-06-14" for d in out)
    assert "2026-06-10" not in out


def test_performance_reports_unpaired_mtm_and_completed_pair_metrics(tmp_path):
    store = MetricsStore(str(tmp_path / "test.db"))
    ts = datetime(2026, 8, 1, 12, tzinfo=timezone.utc).timestamp()
    for side, price in (("YES", 0.46), ("NO", 0.55)):
        store.record_fill({"ts": ts, "cid": "c", "market": "M", "side": side,
                           "token": side, "price": price, "size": 10})
    store.record_merge("c", 10, ts=ts)
    store.record_inventory_snapshot("c", "M", unpaired_shares=2,
                                    cost_basis=0.9, exposure_usd=1.1,
                                    status="unpaired", ts=ts + 1)
    row = store.performance_report("2026-08-01")["markets"][0]
    store.close()
    assert row["unpaired_inventory_mtm_usd"] == 1.1
    assert abs(row["net_pnl_with_unpaired_mtm_est_usd"] - 1.0) < 1e-9
    assert row["completed_pair_count"] == 10
    assert abs(row["cashflow_per_completed_pair_usd"] - (-0.01)) < 1e-9
    assert row["trade_event_count"] == 2


def test_reward_calibration_reports_structured_guard_interruptions(tmp_path):
    store = MetricsStore(str(tmp_path / "test.db"))
    ts = datetime(2026, 8, 1, 12, tzinfo=timezone.utc).timestamp()
    minute = int(ts) // 60
    with store._lock:
        store._conn.execute(
            "INSERT INTO reward_samples (minute_ts,cid,est_usd) VALUES (?,?,?)",
            (minute, "c", 0.02))
        store._conn.commit()
    store.record_guard_event("c", "market", "market_guard_pull", ts=ts)
    row = store.reward_calibration_report(1, "2026-08-01")["market_days"][0]
    store.close()
    assert row["guard_interruptions_status"] == "recorded"
    assert row["guard_interruptions"] == 1
    assert row["guard_interruption_reasons"] == {"market_guard_pull": 1}


def test_recovery_event_persists_pair_economics_for_audit(tmp_path):
    """Dropping a recovery decision's cost inputs must make later PnL review impossible."""
    store = MetricsStore(str(tmp_path / "test.db"))
    store.record_recovery_event(
        "cid1", "forced_hedge_deferred", 12.0,
        reason="over_hard_cap", recovery_path="forced_hedge",
        quote_price=0.53, pair_cap=0.50, proposed_price=0.53,
        cost_basis=0.49, fee_per_share=0.003, expected_pair_pnl=-0.023,
        ts=1_700_000_000.0,
    )
    row = store._conn.execute(
        "SELECT event,reason,unpaired,quote_price,pair_cap,proposed_price,"
        "cost_basis,fee_per_share,expected_pair_pnl,ts FROM recovery_events "
        "WHERE cid='cid1'").fetchone()
    store.close()

    assert row == (
        "forced_hedge_deferred", "over_hard_cap", 12.0, 0.53, 0.50, 0.53,
        0.49, 0.003, -0.023, 1_700_000_000.0,
    )


def test_pause_day_event_persists_smoothed_loss_and_inventory(tmp_path):
    """A daily-loss pause without its calculation cannot be reviewed or tuned."""
    store = MetricsStore(str(tmp_path / "test.db"))
    store.record_pause_day_event(
        "triggered", reason="daily_loss_limit", equity=470.0,
        smoothed_equity=475.0, day_loss=25.0, inventory_usd=13.5,
        ts=1_700_000_001.0,
    )
    row = store._conn.execute(
        "SELECT event,reason,equity,smoothed_equity,day_loss,inventory_usd,ts "
        "FROM pause_day_events").fetchone()
    store.close()

    assert row == (
        "triggered", "daily_loss_limit", 470.0, 475.0, 25.0, 13.5,
        1_700_000_001.0,
    )


def test_recovery_history_returns_one_market_timeline_and_latest_inventory(tmp_path):
    """A market drill-down must neither mix other markets nor lose decision order."""
    store = MetricsStore(str(tmp_path / "test.db"))
    store.record_recovery_event("wanted", "quote_placed", 8, quote_price=0.44,
                                pair_cap=0.45, ts=100.0)
    store.record_recovery_event("other", "skip", 3, reason="stale_book", ts=101.0)
    store.record_recovery_event("wanted", "forced_hedge_deferred", 8,
                                reason="over_hard_cap", quote_price=0.48,
                                pair_cap=0.45, ts=102.0)
    store.record_inventory_snapshot("wanted", "Question", unpaired_shares=8,
                                    cost_basis=0.53, exposure_usd=4.24,
                                    status="unpaired", ts=103.0)

    history = store.recovery_history("wanted")
    store.close()

    assert [event["event"] for event in history["events"]] == [
        "quote_placed", "forced_hedge_deferred"]
    assert history["events"][1]["reason"] == "over_hard_cap"
    assert history["inventory"] == {
        "market": "Question", "unpaired_shares": 8.0, "cost_basis": 0.53,
        "exposure_usd": 4.24, "status": "unpaired", "ts": 103.0,
    }


# ── P0 quote risk decision persistence ──


def test_quote_risk_decision_persists_all_fields(tmp_path):
    """决策的所有字段都必须可持久化且可回读。"""
    from pmbot.risk import QuoteRiskDecision

    store = MetricsStore(str(tmp_path / "test.db"))
    d = QuoteRiskDecision(
        yes_action="widen", no_action="allow",
        yes_widen=0.02, no_widen=0.0,
        reason="flow=0.70", score=0.70,
    )
    store.record_quote_risk_decision("cid1", "shadow", d, ts=1_700_000_100.0)

    row = store._conn.execute(
        "SELECT ts, cid, mode, yes_action, no_action, yes_widen, no_widen, score, reason "
        "FROM quote_risk_decisions WHERE cid='cid1'").fetchone()
    store.close()

    assert row == (
        1_700_000_100.0, "cid1", "shadow",
        "widen", "allow", 0.02, 0.0, 0.70, "flow=0.70",
    )


def test_quote_risk_decision_write_failure_does_not_raise(tmp_path):
    """SQLite 写入失败时只能告警，不能中止报价循环。"""
    from pmbot.risk import QuoteRiskDecision

    store = MetricsStore(str(tmp_path / "test.db"))
    d = QuoteRiskDecision("allow", "allow", 0.0, 0.0, "no_signal", 0.0)
    # Force an error by closing the connection early.
    store._conn.close()

    # Must NOT raise — the caller is the quote loop.
    store.record_quote_risk_decision("cid1", "shadow", d)


def test_quote_risk_report_filters_by_cid_and_time(tmp_path):
    """报表支持按 cid 和时间窗口过滤。"""
    from pmbot.risk import QuoteRiskDecision

    store = MetricsStore(str(tmp_path / "test.db"))
    store.record_quote_risk_decision(
        "cidA", "shadow",
        QuoteRiskDecision("allow", "allow", 0.0, 0.0, "no_signal", 0.0),
        ts=1000.0)
    store.record_quote_risk_decision(
        "cidB", "shadow",
        QuoteRiskDecision("widen", "allow", 0.01, 0.0, "flow=0.65", 0.65),
        ts=2000.0)
    store.record_quote_risk_decision(
        "cidB", "active",
        QuoteRiskDecision("pull", "allow", 0.0, 0.0, "flow=0.90", 0.90),
        ts=3000.0)
    store.record_quote_risk_decision(
        "cidB", "shadow",
        QuoteRiskDecision("allow", "pull", 0.0, 0.0, "flow=0.88", 0.88),
        ts=4000.0)

    # Filter by cid
    report_cid = store.quote_risk_report(cid="cidA")
    assert len(report_cid["decisions"]) == 1

    # Filter by time window
    report_window = store.quote_risk_report(since_ts=2500.0)
    assert len(report_window["decisions"]) == 2  # ts=3000, 4000

    # Both filters
    report_both = store.quote_risk_report(cid="cidB", since_ts=3500.0)
    assert len(report_both["decisions"]) == 1

    store.close()


def test_quote_risk_report_action_distribution(tmp_path):
    """动作分布统计正确（两侧独立计数）。"""
    from pmbot.risk import QuoteRiskDecision

    store = MetricsStore(str(tmp_path / "test.db"))
    # 1: both allow
    store.record_quote_risk_decision(
        "c", "shadow",
        QuoteRiskDecision("allow", "allow", 0.0, 0.0, "no_signal", 0.0),
        ts=1000.0)
    # 2: yes=widen, no=allow
    store.record_quote_risk_decision(
        "c", "shadow",
        QuoteRiskDecision("widen", "allow", 0.01, 0.0, "flow=0.65", 0.65),
        ts=2000.0)
    # 3: yes=widen, no=pull
    store.record_quote_risk_decision(
        "c", "shadow",
        QuoteRiskDecision("widen", "pull", 0.01, 0.0, "flow=0.75", 0.75),
        ts=3000.0)

    report = store.quote_risk_report()
    store.close()

    dist = report["action_distribution"]
    # Each row counts 2 sides: allow x 3, widen x 2, pull x 1
    assert dist.get("allow", 0) == 3
    assert dist.get("widen", 0) == 2
    assert dist.get("pull", 0) == 1


def test_quote_risk_report_shadow_vs_active_interception(tmp_path):
    """shadow/active 拦截计数分别统计。"""
    from pmbot.risk import QuoteRiskDecision

    store = MetricsStore(str(tmp_path / "test.db"))
    # shadow, both allow (not intercepted)
    store.record_quote_risk_decision(
        "c", "shadow",
        QuoteRiskDecision("allow", "allow", 0.0, 0.0, "no_signal", 0.0),
        ts=1000.0)
    # shadow, widen yes (intercepted)
    store.record_quote_risk_decision(
        "c", "shadow",
        QuoteRiskDecision("widen", "allow", 0.01, 0.0, "flow=0.70", 0.70),
        ts=2000.0)
    # active, pull no (intercepted)
    store.record_quote_risk_decision(
        "c", "active",
        QuoteRiskDecision("allow", "pull", 0.0, 0.0, "flow=0.90", 0.90),
        ts=3000.0)
    # active, both allow (not intercepted)
    store.record_quote_risk_decision(
        "c", "active",
        QuoteRiskDecision("allow", "allow", 0.0, 0.0, "no_signal", 0.0),
        ts=4000.0)

    report = store.quote_risk_report()
    store.close()

    assert report["shadow_intercepted"] == 1
    assert report["active_intercepted"] == 1


def test_quote_risk_report_avg_scores(tmp_path):
    """平均分和拦截平均分统计正确。"""
    from pmbot.risk import QuoteRiskDecision

    store = MetricsStore(str(tmp_path / "test.db"))
    store.record_quote_risk_decision(
        "c", "shadow",
        QuoteRiskDecision("allow", "allow", 0.0, 0.0, "no_signal", 0.0),
        ts=1000.0)
    store.record_quote_risk_decision(
        "c", "shadow",
        QuoteRiskDecision("widen", "allow", 0.01, 0.0, "flow=0.70", 0.70),
        ts=2000.0)

    report = store.quote_risk_report()
    store.close()

    # avg = (0.0 + 0.70) / 2 = 0.35
    assert report["avg_score"] == 0.35
    # intercepted avg = 0.70
    assert report["avg_score_intercepted"] == 0.70
    # markout sections present (no paired markouts in this test, so all None)
    assert "markout_30s" in report
    assert "markout_300s" in report


def test_quote_risk_report_pairs_markouts(tmp_path):
    """决策与 markouts 配对后可计算 intercepted/allow 组的 markout 统计。

    配对方向：markout → 决策（每条 markout 归最近 fill_ts 前的决策）。
    """
    from pmbot.risk import QuoteRiskDecision

    store = MetricsStore(str(tmp_path / "test.db"))
    # Two decisions at t=1000 (allow) and t=2000 (widen, intercepted)
    store.record_quote_risk_decision(
        "c", "shadow",
        QuoteRiskDecision("allow", "allow", 0.0, 0.0, "no_signal", 0.0),
        ts=1000.0)
    store.record_quote_risk_decision(
        "c", "shadow",
        QuoteRiskDecision("widen", "allow", 0.01, 0.0, "flow=0.70", 0.70),
        ts=2000.0)

    # Markouts: fill_ts determines which decision they pair to.
    # fill_ts=1001 pairs to decision ts=1000 (allow).
    # fill_ts=2001 pairs to decision ts=2000 (intercepted).
    with store._lock:
        store._conn.execute(
            "INSERT INTO markouts (ts, fill_ts, cid, market, horizon, markout) "
            "VALUES (?,?,?,?,?,?)",
            (1031.0, 1001.0, "c", "M", 30.0, -0.02))
        store._conn.execute(
            "INSERT INTO markouts (ts, fill_ts, cid, market, horizon, markout) "
            "VALUES (?,?,?,?,?,?)",
            (2050.0, 2001.0, "c", "M", 30.0, -0.04))
        # Also a 300s markout only after the intercepted fill
        store._conn.execute(
            "INSERT INTO markouts (ts, fill_ts, cid, market, horizon, markout) "
            "VALUES (?,?,?,?,?,?)",
            (2301.0, 2001.0, "c", "M", 300.0, -0.08))
        store._conn.commit()

    report = store.quote_risk_report(since_ts=500.0, until_ts=3000.0)
    store.close()

    ms30 = report["markout_30s"]
    assert ms30["intercepted_paired_samples"] == 1
    assert ms30["allow_paired_samples"] == 1
    assert ms30["intercepted_avg_cents"] == -4.0
    assert ms30["allow_avg_cents"] == -2.0
    assert ms30["intercepted_neg_hit_rate"] == 1.0  # both negative
    assert ms30["allow_neg_rate"] == 1.0

    ms300 = report["markout_300s"]
    assert ms300["intercepted_paired_samples"] == 1
    assert ms300["allow_paired_samples"] == 0


def test_quote_risk_report_single_markout_not_double_counted(tmp_path):
    """一条 markout 只归入一条决策，不会被多条决策同时统计。

    最小复现：t=1000 决策=allow，t=1010 决策=pull。
    只有一条 fill_ts=1005 的 markout（介于两者之间）。
    旧代码会同时计入 allow 和 intercepted；修复后只计入 allow
    （fill_ts=1005 最近的 ≤ fill_ts 决策是 t=1000）。
    """
    from pmbot.risk import QuoteRiskDecision

    store = MetricsStore(str(tmp_path / "test.db"))
    store.record_quote_risk_decision(
        "c", "shadow",
        QuoteRiskDecision("allow", "allow", 0.0, 0.0, "no_signal", 0.0),
        ts=1000.0)
    store.record_quote_risk_decision(
        "c", "shadow",
        QuoteRiskDecision("pull", "allow", 0.0, 0.0, "flow=0.90", 0.90),
        ts=1010.0)

    with store._lock:
        store._conn.execute(
            "INSERT INTO markouts (ts, fill_ts, cid, market, horizon, markout) "
            "VALUES (?,?,?,?,?,?)",
            (1030.0, 1005.0, "c", "M", 30.0, -0.02))
        store._conn.commit()

    report = store.quote_risk_report()
    store.close()

    ms30 = report["markout_30s"]
    # Only the allow decision should capture this markout
    assert ms30["allow_paired_samples"] == 1
    assert ms30["intercepted_paired_samples"] == 0
    assert ms30["allow_avg_cents"] == -2.0


# ── P1 episode lifecycle tests ──


def test_open_recovery_episode_creates_one_row(tmp_path):
    """First non-zero unpaired creates a single episode row."""
    store = MetricsStore(str(tmp_path / "test.db"))
    store.open_recovery_episode(
        cid="cid1", started_ts=1000.0, initial_unpaired=10.0,
        peak_abs_exposure_usd=5.0, stage="passive",
    )
    rows = store._conn.execute(
        "SELECT cid, started_ts, initial_unpaired, peak_abs_exposure_usd, "
        "stage, is_closed FROM recovery_episodes WHERE cid='cid1'"
    ).fetchall()
    store.close()
    assert len(rows) == 1
    assert rows[0] == ("cid1", 1000.0, 10.0, 5.0, "passive", 0)


def test_restart_recovers_same_episode(tmp_path):
    """After a simulated restart, the same episode is recovered."""
    store1 = MetricsStore(str(tmp_path / "test.db"))
    store1.open_recovery_episode(
        cid="cid1", started_ts=1000.0, initial_unpaired=15.0,
        peak_abs_exposure_usd=7.5, stage="passive",
    )
    store1.close()

    store2 = MetricsStore(str(tmp_path / "test.db"))
    ep = store2.get_open_episode("cid1")
    store2.close()
    assert ep is not None
    assert ep["initial_unpaired"] == 15.0
    assert ep["stage"] == "passive"


def test_update_recovery_episode_increases_peak_exposure(tmp_path):
    """Peak exposure must ratchet up, never down."""
    store = MetricsStore(str(tmp_path / "test.db"))
    store.open_recovery_episode(
        cid="cid1", started_ts=1000.0, initial_unpaired=10.0,
        peak_abs_exposure_usd=5.0, stage="passive",
    )
    store.update_recovery_episode(
        cid="cid1", peak_abs_exposure_usd=8.0, stage="escalated",
    )
    store.update_recovery_episode(
        cid="cid1", peak_abs_exposure_usd=3.0, stage="escalated",  # lower — ignored
    )
    ep = store.get_open_episode("cid1")
    store.close()
    assert ep["peak_abs_exposure_usd"] == 8.0  # retained the higher value
    assert ep["stage"] == "escalated"


def test_close_recovery_episode_sets_terminal_fields(tmp_path):
    """Closing records the outcome without deleting the row."""
    store = MetricsStore(str(tmp_path / "test.db"))
    store.open_recovery_episode(
        cid="cid1", started_ts=1000.0, initial_unpaired=10.0,
        peak_abs_exposure_usd=5.0, stage="escalated",
    )
    store.close_recovery_episode(
        cid="cid1", closed_ts=1200.0, chosen_path="buy_complement",
        expected_loss_usd=0.35, actual_loss_usd=0.40, reason="hedge_filled",
    )
    ep = store.get_open_episode("cid1")
    store.close()
    assert ep is None  # no longer open
    row = MetricsStore(str(tmp_path / "test.db"))._conn.execute(
        "SELECT is_closed, closed_ts, chosen_path, expected_loss_usd, "
        "actual_loss_usd, closed_reason FROM recovery_episodes WHERE cid='cid1'"
    ).fetchone()
    assert row == (1, 1200.0, "buy_complement", 0.35, 0.40, "hedge_filled")


def test_only_one_open_episode_per_cid(tmp_path):
    """A second call to open_recovery_episode for the same CID must
    update the existing row rather than creating a duplicate."""
    store = MetricsStore(str(tmp_path / "test.db"))
    store.open_recovery_episode(
        cid="cid1", started_ts=1000.0, initial_unpaired=10.0,
        peak_abs_exposure_usd=5.0, stage="passive",
    )
    store.open_recovery_episode(
        cid="cid1", started_ts=1100.0, initial_unpaired=12.0,
        peak_abs_exposure_usd=6.0, stage="passive",
    )
    cnt = store._conn.execute(
        "SELECT COUNT(*) FROM recovery_episodes WHERE cid='cid1' AND is_closed=0"
    ).fetchone()[0]
    store.close()
    assert cnt == 1


def test_aggregate_recovery_episodes_read_only(tmp_path):
    """The aggregate query must return episode stats without side effects."""
    store = MetricsStore(str(tmp_path / "test.db"))
    store.open_recovery_episode(
        cid="cid1", started_ts=1000.0, initial_unpaired=10.0,
        peak_abs_exposure_usd=5.0, stage="terminal",
    )
    store.close_recovery_episode(
        cid="cid1", closed_ts=1300.0, chosen_path="buy_complement",
        expected_loss_usd=0.20, actual_loss_usd=0.25, reason="hedge_filled",
    )
    store.open_recovery_episode(
        cid="cid2", started_ts=1400.0, initial_unpaired=-8.0,
        peak_abs_exposure_usd=4.0, stage="escalated",
    )
    summary = store.recovery_episode_summary()
    store.close()
    assert summary["total_episodes"] == 2
    assert summary["open_episodes"] == 1
    assert summary["closed_episodes"] == 1


def test_get_open_episode_none_for_flat_market(tmp_path):
    """A market with no open episode returns None."""
    store = MetricsStore(str(tmp_path / "test.db"))
    assert store.get_open_episode("never_opened") is None
    store.close()


# ── Task 4: historical replay / episode listing ──


def test_list_recovery_episodes_returns_all_entries(tmp_path):
    """list_recovery_episodes must return all episodes sorted by started_ts desc."""
    store = MetricsStore(str(tmp_path / "test.db"))
    store.open_recovery_episode(
        cid="cid-a", started_ts=1000.0, initial_unpaired=10.0,
        peak_abs_exposure_usd=5.0, stage="passive",
    )
    store.close_recovery_episode(
        cid="cid-a", closed_ts=1200.0, chosen_path="buy_complement",
        expected_loss_usd=0.35, reason="filled",
    )
    store.open_recovery_episode(
        cid="cid-b", started_ts=1100.0, initial_unpaired=-8.0,
        peak_abs_exposure_usd=4.0, stage="escalated",
    )
    store.update_recovery_episode(
        cid="cid-b", peak_abs_exposure_usd=6.0, stage="terminal",
    )

    episodes = store.list_recovery_episodes()
    store.close()

    assert len(episodes) == 2
    # Default sort: newest first (started_ts DESC)
    assert episodes[0]["cid"] == "cid-b"
    assert episodes[0]["stage"] == "terminal"
    assert episodes[0]["peak_abs_exposure_usd"] == 6.0
    assert episodes[0]["is_closed"] == 0
    assert episodes[1]["cid"] == "cid-a"
    assert episodes[1]["is_closed"] == 1
    assert episodes[1]["chosen_path"] == "buy_complement"
    assert abs(episodes[1]["expected_loss_usd"] - 0.35) < 1e-9
    # Duration for closed episodes
    assert abs(episodes[1]["duration_secs"] - 200.0) < 1e-9


def test_list_recovery_episodes_supports_limit_and_offset(tmp_path):
    """list_recovery_episodes must support limit and offset."""
    store = MetricsStore(str(tmp_path / "test.db"))
    for i, cid in enumerate(("c1", "c2", "c3")):
        store.open_recovery_episode(
            cid=cid, started_ts=float(1000 + i), initial_unpaired=float(10 + i),
            peak_abs_exposure_usd=5.0, stage="passive",
        )

    all_eps = store.list_recovery_episodes()
    assert len(all_eps) == 3

    limited = store.list_recovery_episodes(limit=2)
    assert len(limited) == 2

    offset_only = store.list_recovery_episodes(limit=2, offset=1)
    assert len(offset_only) == 2
    # c2 should be first (offset=1 skips c3, newest first)
    assert offset_only[0]["cid"] == "c2"
    assert offset_only[1]["cid"] == "c1"

    store.close()


def test_list_recovery_episodes_filters_by_status(tmp_path):
    """Filters open_only and closed_only must work."""
    store = MetricsStore(str(tmp_path / "test.db"))
    store.open_recovery_episode(
        cid="c-open", started_ts=1000.0, initial_unpaired=10.0,
        peak_abs_exposure_usd=5.0, stage="passive",
    )
    store.open_recovery_episode(
        cid="c-closed", started_ts=900.0, initial_unpaired=8.0,
        peak_abs_exposure_usd=4.0, stage="passive",
    )
    store.close_recovery_episode(
        cid="c-closed", closed_ts=1100.0, reason="filled",
    )

    # open_only
    opens = store.list_recovery_episodes(open_only=True)
    assert len(opens) == 1
    assert opens[0]["cid"] == "c-open"

    # closed_only
    closed = store.list_recovery_episodes(closed_only=True)
    assert len(closed) == 1
    assert closed[0]["cid"] == "c-closed"

    store.close()


def test_replay_old_recovery_events_folds_into_episodes(tmp_path):
    """replay_old_recovery_events must group raw recovery_events by CID
    and produce episode-like dicts with duration, peak unpaired, path counts."""
    store = MetricsStore(str(tmp_path / "test.db"))
    # Simulate old-style events for one market
    base_ts = 1000.0
    for i in range(5):
        store.record_recovery_event(
            "cid-x", "quote_placed", 10.0,
            recovery_path="forced_hedge", proposed_price=0.52,
            cost_basis=0.45,
        )
    store.record_recovery_event(
        "cid-x", "forced_hedge_deferred", 10.0,
        reason="over_hard_cap", recovery_path="forced_hedge",
    )
    store.record_recovery_event(
        "cid-x", "forced_hedge_filled", 0.0,
        recovery_path="forced_hedge", quote_price=0.52,
        cost_basis=0.45, expected_pair_pnl=-0.05,
    )
    # Second market
    store.record_recovery_event(
        "cid-y", "quote_placed", -8.0,
        recovery_path="forced_hedge", proposed_price=0.48,
        cost_basis=0.55,
    )
    store.record_recovery_event(
        "cid-y", "forced_hedge_deferred", -8.0,
        reason="book_unavailable_or_wide", recovery_path="forced_hedge",
    )

    replay = store.replay_old_recovery_events()
    store.close()

    assert len(replay) == 2
    cid_x = next(r for r in replay if r["cid"] == "cid-x")
    cid_y = next(r for r in replay if r["cid"] == "cid-y")

    assert cid_x["event_count"] == 7
    assert cid_x["filled"] is True  # forced_hedge_filled present
    assert cid_x["quote_placed_count"] == 5
    assert cid_x["max_abs_unpaired"] > 0
    assert cid_x["duration_secs"] >= 0

    assert cid_y["event_count"] == 2
    assert cid_y["filled"] is False
    assert cid_y["quote_placed_count"] == 1
    assert cid_y["max_abs_unpaired"] > 0


def test_replay_old_recovery_events_marks_insufficient_evidence(tmp_path):
    """When there is no quote_placed event with proposed_price, the replay
    must mark insufficient_evidence = True."""
    store = MetricsStore(str(tmp_path / "test.db"))
    # Only deferred events, no proposed_prices
    store.record_recovery_event(
        "cid-z", "forced_hedge_deferred", 12.0,
        reason="over_hard_cap", recovery_path="forced_hedge",
        # no proposed_price
    )
    store.record_recovery_event(
        "cid-z", "forced_hedge_deferred", 12.0,
        reason="book_unavailable_or_wide", recovery_path="forced_hedge",
    )

    replay = store.replay_old_recovery_events()
    store.close()

    assert len(replay) == 1
    assert replay[0]["insufficient_evidence"] is True
    assert replay[0]["event_count"] == 2


def test_replay_old_recovery_events_empty_db(tmp_path):
    """Empty database returns empty list."""
    store = MetricsStore(str(tmp_path / "test.db"))
    replay = store.replay_old_recovery_events()
    store.close()
    assert replay == []


def test_replay_splits_same_cid_by_gap(tmp_path):
    """Same-CID events > gap_secs apart must produce separate episodes."""
    store = MetricsStore(str(tmp_path / "test.db"))

    # Episode 1: 3 events at t=1000..1200 (within gap)
    with store._lock:
        for t in (1000.0, 1100.0, 1200.0):
            store._conn.execute(
                "INSERT INTO recovery_events (cid, ts, event, unpaired,"
                " recovery_path, proposed_price, cost_basis) "
                "VALUES (?,?,?,?,?,?,?)",
                ("cid-a", t, "quote_placed", 10.0, "forced_hedge",
                 0.52, 0.45),
            )
        # Episode 2: 2 events at t=5000..5100 (gap > 30 min from ep1)
        for t in (5000.0, 5100.0):
            store._conn.execute(
                "INSERT INTO recovery_events (cid, ts, event, unpaired,"
                " recovery_path, proposed_price, cost_basis) "
                "VALUES (?,?,?,?,?,?,?)",
                ("cid-a", t, "quote_placed", 8.0, "forced_hedge",
                 0.54, 0.46),
            )
        store._conn.commit()

    replay = store.replay_old_recovery_events(gap_secs=1800.0)
    store.close()

    # Same CID but separated by a long quiet period → 2 episodes
    assert len(replay) == 2
    assert replay[0]["event_count"] == 3
    assert replay[0]["max_abs_unpaired"] == 10.0
    assert replay[1]["event_count"] == 2
    assert replay[1]["max_abs_unpaired"] == 8.0
    # Both episodes lack dual prices → insufficient_evidence
    assert replay[0]["insufficient_evidence"]
    assert replay[1]["insufficient_evidence"]


def test_replay_dual_path_compare_reruns_decision_engine(tmp_path):
    """When complement_ask + original_bid + market_hints are present,
    _finalize_replay_episode must re-run choose_recovery_action and
    return a comparison dict."""
    from unittest.mock import MagicMock
    from pmbot.recovery import choose_recovery_action

    store = MetricsStore(str(tmp_path / "test.db"))
    market = MagicMock()
    market.fee_bps = 200
    market.fee_exponent = 0.5
    market.tick = 0.01
    market.condition_id = "cid-compare"
    market.yes_token = "yes-t"
    market.no_token = "no-t"

    # Write an event with both complement_ask and original_bid
    with store._lock:
        store._conn.execute(
            "INSERT INTO recovery_events (cid, ts, event, unpaired,"
            " recovery_path, proposed_price, cost_basis,"
            " complement_ask, original_bid) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("cid-compare", 1000.0, "quote_placed", 10.0, "forced_hedge",
             0.52, 0.45, 0.58, 0.30),
        )
        store._conn.commit()

    replay = store.replay_old_recovery_events(
        gap_secs=1800.0,
        market_hints={"cid-compare": market},
    )
    store.close()

    assert len(replay) == 1
    assert replay[0]["comparison"] is not None
    comp = replay[0]["comparison"]
    assert comp["path"] in ("buy_complement", "sell_original", "manual_hold")
    # Buy_complement should be cheaper: basis=0.45, ask=0.58 → loss ~0.40
    # Sell: basis=0.45, bid=0.30 → loss ~1.60
    # So buy_complement is expected
    assert comp["path"] == "buy_complement"
    assert comp["expected_loss_usd"] is not None
    # With market hints, dual-price events should still have
    # insufficient_evidence=False (we have prices to compare)
    assert not replay[0]["insufficient_evidence"]
    # The comparison is present because dual prices + market hints were given.
    assert replay[0]["comparison"] is not None


def test_outcome_report_separates_realized_paired_unpaired_and_incomplete(tmp_path):
    """Completeness summary must count each state and expose missing reasons."""
    store = MetricsStore(str(tmp_path / "test.db"))
    ts = datetime(2026, 8, 1, 12, tzinfo=timezone.utc).timestamp()

    # Realized: balanced buy + merge
    store.record_fill({"ts": ts, "cid": "realized", "market": "R", "side": "YES",
                       "token": "ry", "price": 0.46, "size": 10})
    store.record_fill({"ts": ts, "cid": "realized", "market": "R", "side": "NO",
                       "token": "rn", "price": 0.52, "size": 10})
    store.record_merge("realized", 10, ts=ts)
    # Incomplete: unpaired no mid
    store.record_fill({"ts": ts, "cid": "incomplete", "market": "I", "side": "YES",
                       "token": "iy", "price": 0.60, "size": 15})

    report = store.outcome_report("2026-08-01")
    store.close()

    assert report["realized_count"] == 1
    assert report["incomplete_count"] == 1
    assert report["complete_ratio"] == 0.5
    assert "unpaired_no_persistent_mid" in report["missing_reasons"]
    assert report["account_reward_total_usd"] == 0.0


def test_outcome_report_excludes_account_reward_from_market_net(tmp_path):
    """Account-level rewards must appear in the report summary but never in a market outcome."""
    store = MetricsStore(str(tmp_path / "test.db"))
    ts = datetime(2026, 8, 1, 12, tzinfo=timezone.utc).timestamp()

    store.record_fill({"ts": ts, "cid": "m1", "market": "M1", "side": "YES",
                       "token": "y", "price": 0.50, "size": 10})
    store.record_fill({"ts": ts, "cid": "m1", "market": "M1", "side": "NO",
                       "token": "n", "price": 0.50, "size": 10})
    store.record_merge("m1", 10, ts=ts)
    store.record_realized_reward("2026-08-01", 5.25)

    report = store.outcome_report("2026-08-01")
    store.close()

    assert report["account_reward_total_usd"] == 5.25
    o = report["outcomes"][0]
    assert o.get("market_reward_usd") is None
    assert o["net_outcome_usd"] == o["trading_pnl_usd"]


def test_outcome_report_ranks_complete_by_net_outcome(tmp_path):
    """Complete markets are sorted by net_outcome_usd descending for ranking."""
    store = MetricsStore(str(tmp_path / "test.db"))
    ts = datetime(2026, 8, 1, 12, tzinfo=timezone.utc).timestamp()

    # Market A: pair cost 0.50+0.52=1.02 → loss $0.02
    store.record_fill({"ts": ts, "cid": "A", "market": "A", "side": "YES",
                       "token": "ay", "price": 0.50, "size": 10})
    store.record_fill({"ts": ts, "cid": "A", "market": "A", "side": "NO",
                       "token": "an", "price": 0.52, "size": 10})
    store.record_merge("A", 10, ts=ts)
    # Market B: pair cost 0.40+0.45=0.85 → gain $1.50
    store.record_fill({"ts": ts, "cid": "B", "market": "B", "side": "YES",
                       "token": "by", "price": 0.40, "size": 10})
    store.record_fill({"ts": ts, "cid": "B", "market": "B", "side": "NO",
                       "token": "bn", "price": 0.45, "size": 10})
    store.record_merge("B", 10, ts=ts)

    report = store.outcome_report("2026-08-01")
    store.close()

    assert report["realized_count"] == 2
    assert [o["cid"] for o in report["outcomes"]] == ["B", "A"]


def test_outcome_report_incomplete_not_in_rankings(tmp_path):
    """Incomplete markets are excluded from net-outcome ranking entirely."""
    store = MetricsStore(str(tmp_path / "test.db"))
    ts = datetime(2026, 8, 1, 12, tzinfo=timezone.utc).timestamp()

    store.record_fill({"ts": ts, "cid": "inc", "market": "Inc", "side": "YES",
                       "token": "iy", "price": 0.55, "size": 20})
    store.record_fill({"ts": ts, "cid": "comp", "market": "Comp", "side": "YES",
                       "token": "cy", "price": 0.50, "size": 2})
    store.record_fill({"ts": ts + 1, "cid": "comp", "market": "Comp", "side": "YES",
                       "token": "cy", "price": 0.52, "size": 2, "exit": 1})

    report = store.outcome_report("2026-08-01")
    store.close()

    assert report["realized_count"] == 1
    assert report["incomplete_count"] == 1
    assert len(report["outcomes"]) == 1
    assert report["outcomes"][0]["cid"] == "comp"
    assert len(report["incomplete"]) == 1
    assert report["incomplete"][0]["cid"] == "inc"
