"""Tests for metrics store."""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from pmbot.gamma import Market
from pmbot.metrics import MetricsStore


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
        soft_expected_pair_pnl=-0.038,
        ts=1_700_000_000.0,
    )
    row = store._conn.execute(
        "SELECT event,reason,unpaired,quote_price,pair_cap,proposed_price,"
        "cost_basis,fee_per_share,expected_pair_pnl,soft_expected_pair_pnl,ts FROM recovery_events "
        "WHERE cid='cid1'").fetchone()
    store.close()

    assert row == (
        "forced_hedge_deferred", "over_hard_cap", 12.0, 0.53, 0.50, 0.53,
        0.49, 0.003, -0.023, -0.038, 1_700_000_000.0,
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
