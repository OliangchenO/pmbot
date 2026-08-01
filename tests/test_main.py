"""Tests for Bot event-driven quote pulls (main.py)."""

import asyncio
import logging
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from pmbot import main, strategy
from pmbot.books import Book, BookTracker
from pmbot.brokers import PaperBroker, Position
from pmbot.gamma import Market
from pmbot.main import Bot
from pmbot.strategy import Quote


BASE_CFG = {
    "mode": "paper",
    "capital_usd": 500,
    "guards": {
        "vol_window_secs": 60,
        "vol_max_move_cents": 3.0,
        "max_same_side_fills": 3,
        "same_side_window_minutes": 15,
        "market_cooldown_minutes": 45,
        "velocity_window_secs": 10,
        "velocity_max_trades": 8,
        "directional_consecutive": 5,
        "side_cooldown_minutes": 10,
        "flow_window_secs": 300,
        "flow_min_volume_shares": 200,
        "flow_widen_threshold": 0.6,
        "flow_pull_threshold": 0.85,
        "flow_widen_max_cents": 2.0,
        "markout_horizons_secs": [30, 300],
        "markout_window_minutes": 120,
        "markout_min_samples": 3,
        "markout_trip_cents": -1.5,
    },
}


def test_report_command_renders_requested_utc_date(tmp_path):
    cfg = dict(BASE_CFG)
    cfg["metrics"] = {"db_path": str(tmp_path / "metrics.db")}
    store = main._metrics_store(cfg)
    store.record_realized_reward("2026-07-30", 1.25)
    store.record_realized_reward("2026-07-31", 2.50)
    store.close()

    with main.console.capture() as capture:
        main.cmd_report(cfg, "2026-07-30")

    output = capture.get()
    assert "PnL report — 2026-07-30" in output
    assert "$+1.25" in output
    assert "$+2.50" not in output


def test_trades_command_keeps_full_market_question(tmp_path):
    cfg = dict(BASE_CFG)
    cfg["metrics"] = {"db_path": str(tmp_path / "metrics.db")}
    question = "Will the complete market question remain visible in PowerShell output? ENDMARKER"
    store = main._metrics_store(cfg)
    store.record_fill({
        "cid": "cid", "market": question, "side": "YES",
        "token": "yes", "price": 0.5, "size": 10,
    })
    store.close()

    with main.console.capture() as capture:
        main.cmd_trades(cfg, limit=50, hours=None, export_csv=None)

    assert "ENDMARKER" in capture.get()


def test_performance_command_keeps_full_market_question(tmp_path):
    cfg = dict(BASE_CFG)
    cfg["metrics"] = {"db_path": str(tmp_path / "metrics.db")}
    question = "Will the complete market question remain visible in PowerShell output? ENDMARKER"
    store = main._metrics_store(cfg)
    store.record_fill({
        "cid": "cid", "market": question, "side": "YES",
        "token": "yes", "price": 0.5, "size": 10,
    })
    store.close()

    with main.console.capture() as capture:
        main.cmd_performance(cfg, None)

    market_list = capture.get().split("Per-market performance", maxsplit=1)[0]
    assert "ENDMARKER" in market_list


def test_performance_command_separates_full_market_entries(tmp_path):
    cfg = dict(BASE_CFG)
    cfg["metrics"] = {"db_path": str(tmp_path / "metrics.db")}
    store = main._metrics_store(cfg)
    for cid, question in (("cid-1", "First market"), ("cid-2", "Second market")):
        store.record_fill({
            "cid": cid, "market": question, "side": "YES",
            "token": f"{cid}-yes", "price": 0.5, "size": 10,
        })
    store.close()

    with main.console.capture() as capture:
        main.cmd_performance(cfg, None)

    market_list = capture.get().split("Per-market performance", maxsplit=1)[0]
    assert market_list.count("─" * 40) == 1


def test_performance_table_separates_market_rows(tmp_path):
    cfg = dict(BASE_CFG)
    cfg["metrics"] = {"db_path": str(tmp_path / "metrics.db")}
    store = main._metrics_store(cfg)
    for cid, question in (("cid-1", "First market"), ("cid-2", "Second market")):
        store.record_fill({
            "cid": cid, "market": question, "side": "YES",
            "token": f"{cid}-yes", "price": 0.5, "size": 10,
        })
    store.close()

    with main.console.capture() as capture:
        main.cmd_performance(cfg, None)

    table_output = capture.get()
    assert table_output.count("├") >= 2


def test_performance_table_shows_full_chinese_recovery_evidence(tmp_path):
    cfg = dict(BASE_CFG)
    cfg["metrics"] = {"db_path": str(tmp_path / "metrics.db")}
    store = main._metrics_store(cfg)
    store.record_fill({
        "cid": "cid", "market": "Market", "side": "YES",
        "token": "yes", "price": 0.5, "size": 10,
    })
    store.record_recovery_event("cid", "skip", 10, reason="over_hard_cap")
    store.close()

    with main.console.capture() as capture:
        main.cmd_performance(cfg, None)

    compact = "".join(capture.get().split())
    for header in ("市场", "成交", "资金", "收益", "风险", "恢复 / 证据"):
        assert "".join(header.split()) in compact
    assert "1s/0q/0h" in compact


def test_build_live_notifier_uses_enabled_dingtalk_environment(monkeypatch):
    """Removing the live-alert configuration path must disable this behavior."""
    monkeypatch.setenv("DINGTALK_WEBHOOK_URL", "https://example.test/robot")
    monkeypatch.setenv("DINGTALK_SECRET", "SEC-test")
    cfg = {"notifications": {"dingtalk": {"enabled": True}}}

    notifier = main._build_live_notifier(cfg)

    assert notifier.webhook_url == "https://example.test/robot"
    assert notifier.secret == "SEC-test"


def test_build_live_notifier_requires_explicit_enable(monkeypatch):
    """A configured webhook alone must not make paper or unconfigured runs alert."""
    monkeypatch.setenv("DINGTALK_WEBHOOK_URL", "https://example.test/robot")

    assert main._build_live_notifier({}) is None


def _market() -> Market:
    return Market(
        question="Will it rain tomorrow?", condition_id="cid1",
        yes_token="y1", no_token="n1", min_size=10,
        max_spread_cents=3, daily_pool=50, liquidity=1000,
        volume_24h=500, tick=0.01, end_date=None, neg_risk=False,
    )


def _bot(tmp_path) -> Bot:
    cfg = dict(BASE_CFG)
    cfg["metrics"] = {"db_path": str(tmp_path / "metrics.db")}
    return Bot(cfg)


class _RecoveryBasisBroker:
    """Minimal broker boundary needed to exercise recovery quote economics."""

    def __init__(self, basis: float | None):
        self.basis = basis

    def unpaired_cost_basis(self, _market: Market) -> float | None:
        return self.basis


def test_inventory_recovery_skip_log_includes_reason_and_state(tmp_path, caplog):
    """A held market skipped before quote submission must expose its gate."""
    bot = _bot(tmp_path)
    market = _market()
    caplog.set_level(logging.WARNING, logger="pmbot")

    bot._log_inventory_recovery_skip(
        market, unpaired=15.0, reason="unknown_cost_basis", basis=None)

    assert "INVENTORY_RECOVERY_SKIPPED" in caplog.text
    assert "reason=unknown_cost_basis" in caplog.text
    assert "unpaired=15" in caplog.text
    bot.metrics.close()


def _setup(bot: Bot, tmp_path, market: Market) -> PaperBroker:
    tracker = BookTracker([market.yes_token, market.no_token])
    broker = PaperBroker(500.0, tracker, data_dir=str(tmp_path))
    bot.tracker = tracker
    bot.broker = broker
    bot.markets = [market]
    bot._token_market = {market.yes_token: market, market.no_token: market}
    return broker


def test_inventory_sample_records_current_unpaired_paper_position(tmp_path):
    bot = _bot(tmp_path)
    market = _market()
    broker = _setup(bot, tmp_path, market)
    broker._fill(market, Quote(market.yes_token, 0.47, 12.0), 12.0)

    bot._sample_inventory(time.time())
    report = bot.metrics.performance_report()
    bot.metrics.close()

    row = next(m for m in report["markets"] if m["cid"] == market.condition_id)
    assert row["inventory_status"] == "unpaired"
    assert row["unpaired_shares"] == 12.0
    assert row["unpaired_cost_basis"] == 0.47


def test_recovery_history_command_renders_decision_reason_and_inventory(tmp_path):
    """The operator-facing drill-down must explain why a hedge did not run."""
    cfg = dict(BASE_CFG)
    cfg["metrics"] = {"db_path": str(tmp_path / "metrics.db")}
    store = main._metrics_store(cfg)
    store.record_recovery_event(
        "cid1", "forced_hedge_deferred", 10, reason="over_hard_cap",
        quote_price=0.52, pair_cap=0.49, cost_basis=0.50,
        expected_pair_pnl=-0.02, ts=100.0)
    store.record_inventory_snapshot("cid1", "Question", unpaired_shares=10,
                                    cost_basis=0.50, exposure_usd=5.0,
                                    status="unpaired", ts=101.0)
    store.close()

    with main.console.capture() as capture:
        main.cmd_recovery_history(cfg, "cid1")

    output = capture.get()
    assert "forced_hedge_deferred" in output
    assert "over_hard_cap" in output
    assert "最新库存" in output
    assert "unpaired" in output


def test_performance_command_shows_legacy_and_shadow_candidates(tmp_path):
    """Operators must be able to compare P2.1 without reading SQLite directly."""
    cfg = dict(BASE_CFG)
    cfg["metrics"] = {"db_path": str(tmp_path / "metrics.db")}
    store = main._metrics_store(cfg)
    ts = main.datetime.now(main.timezone.utc).timestamp()
    legacy = _scored("legacy", 2.0)
    shadow = _scored("shadow", 1.0)
    shadow.net_shadow_score = 0.2
    legacy.net_shadow_score = 0.1
    store.record_net_shadow_snapshot([legacy, shadow], ts, {"top_n": 1})
    store.close()

    with main.console.capture() as capture:
        main.cmd_performance(cfg, None)

    output = capture.get()
    assert "Net shadow candidates" in output
    assert "legacy" in output
    assert "shadow" in output


def test_performance_command_prints_full_condition_id_for_recovery_lookup(tmp_path):
    """Operators need a copyable CID, not just a shortened market label."""
    cfg = dict(BASE_CFG)
    cfg["metrics"] = {"db_path": str(tmp_path / "metrics.db")}
    cid = "0x" + "a" * 64
    store = main._metrics_store(cfg)
    store.record_fill({"cid": cid, "market": "Question", "side": "YES",
                       "token": "yes", "price": 0.5, "size": 10})
    store.close()

    with main.console.capture() as capture:
        main.cmd_performance(cfg, None)

    assert cid in capture.get()

def test_guard_trip_pulls_quotes_immediately(tmp_path):
    async def scenario():
        bot = _bot(tmp_path)
        m = _market()
        broker = _setup(bot, tmp_path, m)
        broker.set_quotes(m, [Quote(m.yes_token, 0.45, 20.0),
                              Quote(m.no_token, 0.52, 20.0)])
        bot.guards.trip_market(m.condition_id, time.time(), "test", m.question)
        assert bot._pull_tasks  # pull scheduled without waiting for the loop
        await asyncio.gather(*list(bot._pull_tasks))
        assert broker.open_quotes(m) == []
        bot.metrics.close()
    asyncio.run(scenario())


def test_side_block_pulls_only_blocked_side(tmp_path):
    async def scenario():
        bot = _bot(tmp_path)
        m = _market()
        broker = _setup(bot, tmp_path, m)
        broker.set_quotes(m, [Quote(m.yes_token, 0.45, 20.0),
                              Quote(m.no_token, 0.52, 20.0)])
        now = time.time()
        for _ in range(bot.guards.dir_consec):
            bot.guards.record_trade(m, m.yes_token, "SELL", 1.0, now)
        assert bot._pull_tasks
        await asyncio.gather(*list(bot._pull_tasks))
        remaining = broker.open_quotes(m)
        assert [q.token_id for q in remaining] == [m.no_token]
        bot.metrics.close()
    asyncio.run(scenario())


def test_schedule_pull_without_running_loop_is_noop(tmp_path):
    bot = _bot(tmp_path)
    bot._schedule_market_pull("cid1")  # must not raise outside a loop
    assert not bot._pull_tasks
    bot.metrics.close()


def _scored(cid: str, score: float) -> Market:
    m = Market(
        question=f"market {cid}", condition_id=cid,
        yes_token=f"{cid}y", no_token=f"{cid}n", min_size=10,
        max_spread_cents=3, daily_pool=100, liquidity=5000,
        volume_24h=0, tick=0.01, end_date=None, neg_risk=False,
    )
    m.score = score
    return m


def test_sticky_keeps_held_market_over_marginally_better(tmp_path):
    bot = _bot(tmp_path)
    bot.cfg["scanner"] = {"top_n_markets": 1, "sticky_swap": True,
                          "swap_score_margin": 0.25}
    bot.markets = [_scored("held", 1.0)]
    # a newcomer scoring 1.1 does NOT clear 0.9 * 1.25 = 1.125 → held is kept.
    chosen = bot._select_markets([_scored("new", 1.1), _scored("held", 0.9)])
    assert [m.condition_id for m in chosen] == ["held"]
    bot.metrics.close()


def _seed_uptime(bot, cid: str, in_band: bool, minutes: int = 15) -> None:
    now_min = int(time.time() // 60)
    with bot.metrics._lock:
        for i in range(minutes):
            bot.metrics._conn.execute(
                "INSERT INTO uptime (minute_ts, cid, in_band) VALUES (?,?,?)",
                (now_min - i, cid, int(in_band)))
        bot.metrics._conn.commit()


def test_sticky_displaces_underperforming_held_on_large_margin(tmp_path):
    bot = _bot(tmp_path)
    bot.cfg["scanner"] = {"top_n_markets": 1, "sticky_swap": True,
                          "swap_score_margin": 0.25, "underperform_uptime_pct": 60}
    bot.markets = [_scored("held", 1.0)]
    _seed_uptime(bot, "held", in_band=False)  # 0% uptime → underperforming
    # 2.0 >= 1.0 * 1.25 AND held underperforming → the better market wins.
    chosen = bot._select_markets([_scored("new", 2.0), _scored("held", 1.0)])
    assert [m.condition_id for m in chosen] == ["new"]
    bot.metrics.close()


def test_sticky_protects_performing_held_even_from_much_better(tmp_path):
    bot = _bot(tmp_path)
    bot.cfg["scanner"] = {"top_n_markets": 1, "sticky_swap": True,
                          "swap_score_margin": 0.25, "underperform_uptime_pct": 60}
    bot.markets = [_scored("held", 1.0)]
    _seed_uptime(bot, "held", in_band=True)  # 100% uptime → farming well
    # Even a 10x-better candidate must NOT evict a market that is performing.
    chosen = bot._select_markets([_scored("new", 10.0), _scored("held", 1.0)])
    assert [m.condition_id for m in chosen] == ["held"]
    bot.metrics.close()


def test_sticky_protects_freshly_entered_held_with_thin_history(tmp_path):
    bot = _bot(tmp_path)
    bot.cfg["scanner"] = {"top_n_markets": 1, "sticky_swap": True,
                          "swap_score_margin": 0.25, "underperform_uptime_pct": 60}
    bot.markets = [_scored("held", 1.0)]
    _seed_uptime(bot, "held", in_band=False, minutes=3)  # below min_samples
    # Too little history to judge → treated as performing → protected.
    chosen = bot._select_markets([_scored("new", 10.0), _scored("held", 1.0)])
    assert [m.condition_id for m in chosen] == ["held"]
    bot.metrics.close()


def test_sticky_drops_ineligible_held_and_backfills(tmp_path):
    bot = _bot(tmp_path)
    bot.cfg["scanner"] = {"top_n_markets": 1, "sticky_swap": True,
                          "swap_score_margin": 0.25}
    bot.markets = [_scored("held", 1.0)]
    # held no longer appears in the ranked (ineligible) → slot backfills.
    chosen = bot._select_markets([_scored("other", 0.5)])
    assert [m.condition_id for m in chosen] == ["other"]
    bot.metrics.close()


def test_rescan_is_sticky_and_swaps_incrementally(tmp_path, monkeypatch):
    """End-to-end: a reshuffled re-rank must not churn the set, and a genuine
    swap must reuse the tracker (resubscribe) instead of stop()/start()."""
    from pmbot import gamma as gamma_mod
    from pmbot.books import BookTracker

    counts = {"resub": 0, "stop": 0, "start": 0}

    async def fake_start(self):
        counts["start"] += 1

    async def fake_stop(self):
        counts["stop"] += 1

    async def fake_resub(self, token_ids, carry=None):
        counts["resub"] += 1
        self.books = {t: self.books.get(t) or __import__(
            "pmbot.books", fromlist=["Book"]).Book(t) for t in token_ids}

    monkeypatch.setattr(BookTracker, "start", fake_start)
    monkeypatch.setattr(BookTracker, "stop", fake_stop)
    monkeypatch.setattr(BookTracker, "resubscribe", fake_resub)

    ranked_holder = {"v": []}
    monkeypatch.setattr(gamma_mod, "scan",
                        lambda cfg, exclude=None, full=False: list(ranked_holder["v"]))

    async def scenario():
        bot = _bot(tmp_path)
        bot.cfg = dict(bot.cfg)
        bot.cfg["paper"] = {}
        bot.cfg["risk"] = {}
        bot.cfg["quoting"] = {"max_capital_per_market": 50}
        bot.cfg["scanner"] = {
            "top_n_markets": 2, "sticky_swap": True, "swap_score_margin": 0.25,
            "underperform_uptime_pct": 60, "underperform_lookback_minutes": 30,
            "refresh_minutes": 30,
        }

        a, b, c = _scored("A", 3.0), _scored("B", 2.0), _scored("C", 1.0)
        ranked_holder["v"] = [a, b, c]
        await bot._rescan(initial=True)
        assert {m.condition_id for m in bot.markets} == {"A", "B"}
        assert counts["start"] == 1 and counts["resub"] == 0

        # Re-rank reshuffles scores but the same cids stay best → NO churn.
        ranked_holder["v"] = [_scored("B", 3.0), _scored("A", 2.0), _scored("C", 1.0)]
        await bot._rescan()
        assert {m.condition_id for m in bot.markets} == {"A", "B"}
        assert counts["resub"] == 0 and counts["stop"] == 0  # nothing torn down

        # A held market (A) drops out of eligibility → real swap to C.
        ranked_holder["v"] = [_scored("B", 3.0), _scored("C", 1.0)]
        await bot._rescan()
        assert {m.condition_id for m in bot.markets} == {"B", "C"}
        assert counts["resub"] == 1   # incremental resubscribe used…
        assert counts["stop"] == 0    # …and the tracker was never torn down
        bot.metrics.close()

    asyncio.run(scenario())


def test_rescan_records_shadow_candidates_without_changing_legacy_selection(tmp_path, monkeypatch):
    """Passive P2.1 instrumentation must not make the shadow winner tradable."""
    from pmbot import gamma as gamma_mod
    from pmbot.books import BookTracker

    async def fake_start(self):
        return None

    monkeypatch.setattr(BookTracker, "start", fake_start)
    legacy_winner, shadow_winner = _scored("legacy", 2.0), _scored("shadow", 1.0)
    shadow_winner.daily_pool = 300
    monkeypatch.setattr(gamma_mod, "scan",
                        lambda cfg, exclude=None, full=False: [legacy_winner, shadow_winner])

    async def scenario():
        bot = _bot(tmp_path)
        bot.cfg = dict(bot.cfg)
        bot.cfg["paper"] = {}
        bot.cfg["risk"] = {}
        bot.cfg["quoting"] = {"max_capital_per_market": 50}
        bot.cfg["scanner"] = {"top_n_markets": 1, "sticky_swap": False,
                              "net_shadow": {"reward_realization_prior": 1.0,
                                             "uptime_prior": 1.0,
                                             "markout_cost_per_hour_prior": 0.0,
                                             "recovery_cost_per_hour_prior": 0.0}}
        await bot._rescan(initial=True)
        report = bot.metrics.net_shadow_report(
            main.datetime.now(main.timezone.utc).strftime("%Y-%m-%d"))
        assert [m.condition_id for m in bot.markets] == ["legacy"]
        assert [row["cid"] for row in report["legacy_top"]] == ["legacy"]
        bot.metrics.close()

    asyncio.run(scenario())


def test_rescan_keeps_unpaired_inventory_market_until_flat(tmp_path, monkeypatch):
    """扫描器不得换出仍有未配对库存的市场。"""
    from pmbot import gamma as gamma_mod
    from pmbot.books import BookTracker

    async def fake_start(self):
        return None

    async def fake_resubscribe(self, token_ids, carry=None):
        self.books = {t: self.books.get(t) or __import__(
            "pmbot.books", fromlist=["Book"]).Book(t) for t in token_ids}

    monkeypatch.setattr(BookTracker, "start", fake_start)
    monkeypatch.setattr(BookTracker, "resubscribe", fake_resubscribe)
    ranked = {"value": []}
    monkeypatch.setattr(gamma_mod, "scan",
                        lambda cfg, exclude=None, full=False: list(ranked["value"]))

    async def scenario():
        bot = _bot(tmp_path)
        bot.cfg = dict(bot.cfg)
        bot.cfg["paper"] = {}
        bot.cfg["risk"] = {}
        bot.cfg["quoting"] = {"max_capital_per_market": 50}
        bot.cfg["scanner"] = {
            "top_n_markets": 2, "sticky_swap": True, "swap_score_margin": 0.25,
            "underperform_uptime_pct": 60, "underperform_lookback_minutes": 30,
            "refresh_minutes": 30,
        }
        held, flat, fresh = _scored("held", 3.0), _scored("flat", 2.0), _scored("fresh", 1.0)
        held.min_size = 10.0
        ranked["value"] = [held, flat, fresh]
        await bot._rescan(initial=True)
        # A manageable 5-share NO position is below rewardsMinSize (10), so
        # it stays managed without consuming either of the two scan slots.
        bot.broker.state.positions[held.condition_id] = Position(no_shares=5.0)

        ranked["value"] = [flat, fresh]
        await bot._rescan()
        assert {m.condition_id for m in bot.markets} == {"held", "flat", "fresh"}

        # At exactly rewardsMinSize the position consumes one scan slot.
        bot.broker.state.positions[held.condition_id] = Position(no_shares=10.0)
        await bot._rescan()
        assert {m.condition_id for m in bot.markets} == {"held", "flat"}

        bot.broker.state.positions[held.condition_id] = Position()
        await bot._rescan()
        assert {m.condition_id for m in bot.markets} == {"flat", "fresh"}
        bot.metrics.close()

    asyncio.run(scenario())


def test_inventory_quote_only_buys_complement_and_caps_size(tmp_path):
    """未配对 NO 时只能买入不超过缺口的 YES。"""
    bot = _bot(tmp_path)
    bot.broker = _RecoveryBasisBroker(0.45)
    market = _market()
    desired = [Quote(market.yes_token, 0.47, 30.0),
               Quote(market.no_token, 0.50, 30.0)]

    quotes = bot._inventory_recovery_quotes(market, desired, unpaired=-20.0)

    assert [(q.token_id, q.size) for q in quotes] == [(market.yes_token, 20.0)]
    bot.metrics.close()


def test_unpaired_no_keeps_capped_yes_recovery_bid_when_yes_side_is_blocked(tmp_path):
    """普通单边流量防护不得撤掉配平裸 NO 的 YES 买单。"""
    bot = _bot(tmp_path)
    bot.broker = _RecoveryBasisBroker(0.62)
    market = _market()
    bot.guards.allow_side = MagicMock(return_value=False)

    recovery = bot._inventory_recovery_quotes(
        market, [Quote(market.yes_token, 0.38, 20.0)], unpaired=-9.67)
    quotes = bot._filter_quotes_for_side_guard(recovery, unpaired=-9.67, now=time.time())

    assert quotes == [Quote(market.yes_token, 0.38, 9.67)]
    bot.metrics.close()


def test_inventory_recovery_quote_log_includes_pricing_and_book_snapshot(caplog):
    """A recovery bid must leave enough evidence to reproduce its price."""
    market = _market()
    yes_book = Book(market.yes_token)
    yes_book.bids = {0.48: 80.0}
    yes_book.asks = {0.52: 120.0}
    no_book = Book(market.no_token)
    no_book.bids = {0.47: 90.0}
    no_book.asks = {0.53: 70.0}
    pricing = {
        "yes_microprice": 0.496, "fair": 0.491, "base_offset": 0.0105,
        "adaptive_offset": 0.003, "offset": 0.0135,
        "skew": -0.0027, "fade_yes": 0.01, "fade_no": 0.02,
        "flow_imbalance": -0.5, "flow_drift": -0.005,
        "yes_bid_quote": 0.47, "no_bid_quote": 0.47,
    }

    with caplog.at_level(logging.INFO, logger="pmbot"):
        Bot._log_inventory_recovery_quote(
            market, unpaired=-20.0, quote=Quote(market.yes_token, 0.47, 20.0),
            yes_book=yes_book, no_book=no_book, pricing=pricing,
        )

    message = caplog.messages[-1]
    assert "INVENTORY_RECOVERY_QUOTE" in message
    assert "held=NO 20" in message
    assert "quote=BUY YES 20 @ 0.470" in message
    assert "yes_book=0.480x80/0.520x120" in message
    assert "no_book=0.470x90/0.530x70" in message
    assert "fair=0.491" in message
    assert "offset=0.0135 skew=-0.0027" in message


def test_inventory_quote_only_buys_no_for_excess_yes_and_keeps_flat_quotes(tmp_path):
    """未配对 YES 只补 NO；低于锁定阈值时不改变正常双边报价。"""
    bot = _bot(tmp_path)
    bot.broker = _RecoveryBasisBroker(0.45)
    market = _market()
    desired = [Quote(market.yes_token, 0.47, 30.0),
               Quote(market.no_token, 0.50, 30.0)]

    excess_yes = bot._inventory_recovery_quotes(market, desired, unpaired=12.0)
    flat = bot._inventory_recovery_quotes(market, desired, unpaired=0.0)

    assert [(q.token_id, q.size) for q in excess_yes] == [(market.no_token, 12.0)]
    assert [(q.token_id, q.size) for q in flat] == [
        (market.yes_token, 30.0), (market.no_token, 30.0)]
    bot.metrics.close()


def test_unselected_held_market_keeps_only_complement_bid(tmp_path):
    """A held market outside the scanner must never resume two-sided quoting."""
    bot = _bot(tmp_path)
    bot.broker = _RecoveryBasisBroker(0.30)
    market = _market()
    desired = [Quote(market.yes_token, 0.35, 20.0),
               Quote(market.no_token, 0.64, 20.0)]

    assert [(q.token_id, q.size) for q in bot._held_market_recovery_quotes(
        market, desired, unpaired=11.0)] == [(market.no_token, 11.0)]
    assert bot._held_market_recovery_quotes(market, desired, unpaired=0.0) == []
    bot.metrics.close()


def test_escalated_recovery_generates_center_quote_without_normal_strategy_quote(tmp_path):
    """超时恢复必须在区间外市场独立生成等量反向补单。"""
    bot = _bot(tmp_path)
    bot.cfg["quoting"] = {
        "offset_frac_of_max_spread": 0.35,
        "skew_strength": 0.6,
        "flow_drift_max_cents": 1.0,
        "adaptive_markout_gain": 1.0,
        "adaptive_tighten_max_cents": 0.5,
        "adaptive_widen_max_cents": 2.0,
    }
    bot.cfg["risk"] = {"max_inventory_usd_per_market": 30.0}
    market = _market()
    tracker = BookTracker([market.yes_token, market.no_token])
    yes_book = tracker.books[market.yes_token]
    yes_book.bids = {0.10: 100.0}
    yes_book.asks = {0.12: 100.0}
    bot.tracker = tracker

    quotes = bot._escalated_recovery_quotes(
        market, [], unpaired=-20.0, yes_book=yes_book,
        exposure_usd=-2.5, max_inventory_usd=30.0,
    )

    assert [(q.token_id, q.price, q.size) for q in quotes] == [
        (market.yes_token, 0.10, 20.0),
    ]
    bot.metrics.close()


def test_forced_hedge_requires_escalation_and_pair_cost_cap(tmp_path):
    """An unselected small position waits; a costly pair is never crossed."""
    bot = _bot(tmp_path)
    market = _market()
    now = 1_000.0

    assert not bot._forced_hedge_allowed(
        market, urgent=False, exposure_usd=1.0, threshold_usd=15.0,
        risk_since=now, now=now + 30, wait_secs=90,
        basis=0.398, ask=0.648,
    )
    assert bot._forced_hedge_allowed(
        market, urgent=False, exposure_usd=1.0, threshold_usd=15.0,
        risk_since=now, now=now + 90, wait_secs=90,
        basis=0.398, ask=0.60,
    )
    assert not bot._forced_hedge_allowed(
        market, urgent=True, exposure_usd=20.0, threshold_usd=15.0,
        risk_since=now, now=now, wait_secs=90,
        basis=0.398, ask=0.648,
    )
    assert not bot._forced_hedge_allowed(
        market, urgent=True, exposure_usd=20.0, threshold_usd=15.0,
        risk_since=now, now=now, wait_secs=90,
        basis=None, ask=0.60,
    )
    assert bot._forced_hedge_max_price(market, 0.398) == pytest.approx(0.60)
    bot.metrics.close()


def test_held_market_tokens_are_subscribed_for_inventory_management(tmp_path):
    async def scenario():
        bot = _bot(tmp_path)
        bot.paper = False
        market = _market()
        bot.broker = MagicMock()
        bot.broker.held_markets.return_value = [market]
        bot.broker.position_tokens.return_value = [market.yes_token, market.no_token]
        bot.tracker = BookTracker([])
        calls = []

        async def resubscribe(token_ids, carry=None):
            calls.append(token_ids)

        bot.tracker.resubscribe = resubscribe
        await bot._ensure_held_market_books()

        assert calls == [[market.yes_token, market.no_token]]
        assert bot._token_market == {market.yes_token: market, market.no_token: market}
        bot.metrics.close()
    asyncio.run(scenario())


def test_live_startup_manages_inventory_when_scan_finds_no_market(tmp_path, monkeypatch):
    """A scan drought must not bypass inventory checks for an existing position."""
    class StopRun(Exception):
        pass

    async def stop_after_first_retry(_seconds):
        raise StopRun

    async def scenario():
        bot = _bot(tmp_path)
        bot.paper = False
        bot._bootstrap_live_broker = AsyncMock()
        bot._rescan = AsyncMock()
        bot._manage_inventory = AsyncMock()
        bot._ensure_held_market_books = AsyncMock()
        bot.broker = MagicMock()
        monkeypatch.setattr(main.asyncio, "sleep", stop_after_first_retry)

        with pytest.raises(StopRun):
            await bot.run()

        bot._bootstrap_live_broker.assert_awaited_once()
        assert bot.broker.refresh_state.called
        bot._ensure_held_market_books.assert_awaited_once()
        bot._manage_inventory.assert_awaited_once()
        bot.metrics.close()
    asyncio.run(scenario())


def test_cooldown_recovery_only_keeps_complement_for_meaningful_inventory(tmp_path):
    """冷却期只能保留降低裸仓的互补买单，平仓市场仍完全撤单。"""
    bot = _bot(tmp_path)
    bot.broker = _RecoveryBasisBroker(0.45)
    market = _market()
    desired = [Quote(market.yes_token, 0.47, 30.0),
               Quote(market.no_token, 0.50, 30.0)]

    excess_yes = bot._cooldown_recovery_quotes(market, desired, unpaired=12.0)
    excess_no = bot._cooldown_recovery_quotes(market, desired, unpaired=-12.0)
    flat = bot._cooldown_recovery_quotes(market, desired, unpaired=0.0)

    assert [(q.token_id, q.size) for q in excess_yes] == [(market.no_token, 12.0)]
    assert [(q.token_id, q.size) for q in excess_no] == [(market.yes_token, 12.0)]
    assert flat == []
    bot.metrics.close()


def test_inventory_recovery_clamps_quote_above_pair_cost_cap(tmp_path):
    """A changed theoretical bid must retain a safe passive complement price."""
    bot = _bot(tmp_path)
    bot.broker = _RecoveryBasisBroker(0.669)
    market = _market()

    allowed = bot._inventory_recovery_quotes(
        market, [Quote(market.yes_token, 0.329, 50.0)], unpaired=-50.0)
    capped = bot._inventory_recovery_quotes(
        market, [Quote(market.yes_token, 0.340, 50.0)], unpaired=-50.0)

    assert allowed == [Quote(market.yes_token, 0.329, 50.0)]
    assert capped == [Quote(market.yes_token, 0.330, 50.0)]
    bot.metrics.close()


def test_soft_recovery_window_records_tolerance_without_raising_hard_cap(tmp_path):
    """A soft-loss setting must not silently turn a passive recovery bid into a loss."""
    bot = _bot(tmp_path)
    broker = _RecoveryBasisBroker(0.669)
    broker.last_fill_ts = lambda _cid: 100.0
    bot.broker = broker
    bot.cfg["risk"] = {
        "recovery_soft_window_minutes": 30,
        "recovery_max_loss_cents": 1.5,
    }
    market = _market()

    quotes = bot._inventory_recovery_quotes(
        market, [Quote(market.yes_token, 0.340, 50.0)], unpaired=-50.0, now=120.0)

    assert quotes == [Quote(market.yes_token, 0.330, 50.0)]
    bot.metrics.close()


def test_inventory_recovery_raises_quote_cap_to_existing_unpaired_shares(tmp_path):
    """A tier drop must not suppress the passive complement for held inventory."""
    bot = _bot(tmp_path)
    bot.broker = _RecoveryBasisBroker(0.65)
    bot._scale = 0.5
    bot.cfg["scanner"] = {"mid_range": [0.25, 0.75]}
    bot.cfg["quoting"] = {
        "offset_frac_of_max_spread": 0.35,
        "size_mult_of_min": 1.0,
        "max_capital_per_market": 10,
        "skew_strength": 0.6,
    }
    market = _market()
    market.min_size = 100
    yes_book = Book(market.yes_token)
    yes_book.bids = {0.64: 100.0}
    yes_book.asks = {0.65: 100.0}

    normal = strategy.compute_quotes(
        market, yes_book, 0.0, bot.cfg, 30.0, scale=bot._scale)
    recovery_cfg = bot._quote_cfg_for_inventory_recovery(unpaired=15.0)
    recovery = strategy.compute_quotes(
        market, yes_book, 0.0, recovery_cfg, 30.0, scale=bot._scale,
        min_quote_size=15.0)
    quotes = bot._inventory_recovery_quotes(market, recovery, unpaired=15.0)

    assert normal == []
    assert bot.cfg["quoting"]["max_capital_per_market"] == 10
    assert [(q.token_id, q.size) for q in quotes] == [(market.no_token, 15.0)]
    assert quotes[0].price <= 0.34
    bot.metrics.close()


def test_inventory_recovery_uses_current_clob_minimum_order_size(tmp_path):
    bot = _bot(tmp_path)
    bot.broker = _RecoveryBasisBroker(0.65)
    market = _market()
    tracker = BookTracker([market.yes_token, market.no_token])
    tracker.books[market.no_token].min_order_size = 100.0
    bot.tracker = tracker

    quotes = bot._inventory_recovery_quotes(
        market, [Quote(market.no_token, 0.34, 100.0)], unpaired=15.0)

    assert quotes == []
    bot.metrics.close()


def test_cooldown_recovery_clamps_quote_above_pair_cost_cap(tmp_path):
    """Cooldown recovery must retain a complement bid at the economic ceiling."""
    bot = _bot(tmp_path)
    bot.broker = _RecoveryBasisBroker(0.669)
    market = _market()

    quotes = bot._cooldown_recovery_quotes(
        market, [Quote(market.yes_token, 0.372, 50.0)], unpaired=-50.0)

    assert quotes == [Quote(market.yes_token, 0.330, 50.0)]
    bot.metrics.close()


def test_inventory_recovery_requires_known_cost_basis(tmp_path):
    """Unknown inventory basis must fail closed instead of guessing a safe bid."""
    bot = _bot(tmp_path)
    bot.broker = _RecoveryBasisBroker(None)
    market = _market()

    quotes = bot._inventory_recovery_quotes(
        market, [Quote(market.yes_token, 0.329, 50.0)], unpaired=-50.0)

    assert quotes == []
    bot.metrics.close()


def test_paper_inventory_recovery_uses_same_pair_cost_cap(tmp_path):
    """Paper runs must exercise the live recovery ceiling with real inventory."""
    bot = _bot(tmp_path)
    market = _market()
    broker = _setup(bot, tmp_path, market)
    broker._fill(market, Quote(market.no_token, 0.669, 50.0), 50.0)

    allowed = bot._inventory_recovery_quotes(
        market, [Quote(market.yes_token, 0.329, 50.0)],
        unpaired=broker.unpaired_shares(market))
    capped = bot._inventory_recovery_quotes(
        market, [Quote(market.yes_token, 0.340, 50.0)],
        unpaired=broker.unpaired_shares(market))

    assert allowed == [Quote(market.yes_token, 0.329, 50.0)]
    assert capped == [Quote(market.yes_token, 0.330, 50.0)]
    bot.metrics.close()


def test_sticky_disabled_returns_plain_top_n(tmp_path):
    bot = _bot(tmp_path)
    bot.cfg["scanner"] = {"top_n_markets": 2, "sticky_swap": False}
    bot.markets = [_scored("held", 1.0)]
    chosen = bot._select_markets(
        [_scored("a", 3), _scored("b", 2), _scored("c", 1)])
    assert [m.condition_id for m in chosen] == ["a", "b"]
    bot.metrics.close()


def test_configure_logging_writes_utf8_date_named_file_without_renaming_active_log(
        tmp_path, monkeypatch):
    """Daily files avoid Windows rename failures when another bot is writing."""
    monkeypatch.chdir(tmp_path)
    root = main.configure_logging()
    root.info("持久化测试消息")
    listener = main._LOG_LISTENER
    assert listener is not None
    file_handler = next(h for h in listener.handlers
                        if isinstance(h, main.DailyFileHandler))
    main.stop_logging()

    log_file = tmp_path / "logs" / f"pmbot.{main.datetime.now(main.BEIJING_TZ):%Y-%m-%d}.log"
    assert "持久化测试消息" in log_file.read_text(encoding="utf-8")
    assert file_handler.baseFilename == str(log_file)
    assert not (tmp_path / "logs" / "pmbot.log").exists()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    logging.shutdown()


def test_configure_logging_does_not_wait_for_slow_file_io(tmp_path, monkeypatch):
    """Trading-path logging must enqueue instead of waiting for disk writes."""
    import threading
    import time

    write_started = threading.Event()
    release_writer = threading.Event()

    class SlowFileHandler(logging.Handler):
        def __init__(self, *_args, **_kwargs):
            super().__init__()

        def emit(self, _record):
            write_started.set()
            release_writer.wait(timeout=1)

    monkeypatch.setattr(main, "DailyFileHandler", SlowFileHandler)
    root = main.configure_logging(tmp_path / "logs")
    timer = threading.Timer(0.15, release_writer.set)
    timer.start()
    started = time.perf_counter()
    root.info("order lifecycle event")
    elapsed = time.perf_counter() - started
    assert write_started.wait(timeout=1)
    timer.join()
    main.stop_logging()

    assert elapsed < 0.05


def test_runtime_log_formatter_uses_beijing_time():
    record = logging.LogRecord("pmbot", logging.INFO, "", 0, "message", (), None)
    record.created = 0.0
    formatter = main.BeijingFormatter("%(asctime)s", datefmt="%Y-%m-%d %H:%M:%S")

    assert formatter.format(record) == "1970-01-01 08:00:00"
