"""Tests for Bot event-driven quote pulls (main.py)."""

import asyncio
import logging
import time
from logging.handlers import TimedRotatingFileHandler
from unittest.mock import AsyncMock, MagicMock

import pytest

from pmbot import main
from pmbot.books import BookTracker
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


def _setup(bot: Bot, tmp_path, market: Market) -> PaperBroker:
    tracker = BookTracker([market.yes_token, market.no_token])
    broker = PaperBroker(500.0, tracker, data_dir=str(tmp_path))
    bot.tracker = tracker
    bot.broker = broker
    bot.markets = [market]
    bot._token_market = {market.yes_token: market, market.no_token: market}
    return broker


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
        ranked["value"] = [held, flat, fresh]
        await bot._rescan(initial=True)
        bot.broker.state.positions[held.condition_id] = Position(no_shares=20.0)

        ranked["value"] = [flat, fresh]
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
    market = _market()
    desired = [Quote(market.yes_token, 0.47, 30.0),
               Quote(market.no_token, 0.50, 30.0)]

    quotes = bot._inventory_recovery_quotes(market, desired, unpaired=-20.0)

    assert [(q.token_id, q.size) for q in quotes] == [(market.yes_token, 20.0)]
    bot.metrics.close()


def test_inventory_quote_only_buys_no_for_excess_yes_and_keeps_flat_quotes(tmp_path):
    """未配对 YES 只补 NO；低于锁定阈值时不改变正常双边报价。"""
    bot = _bot(tmp_path)
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
    market = _market()
    desired = [Quote(market.yes_token, 0.35, 20.0),
               Quote(market.no_token, 0.64, 20.0)]

    assert [(q.token_id, q.size) for q in bot._held_market_recovery_quotes(
        market, desired, unpaired=11.0)] == [(market.no_token, 11.0)]
    assert bot._held_market_recovery_quotes(market, desired, unpaired=0.0) == []
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


def test_sticky_disabled_returns_plain_top_n(tmp_path):
    bot = _bot(tmp_path)
    bot.cfg["scanner"] = {"top_n_markets": 2, "sticky_swap": False}
    bot.markets = [_scored("held", 1.0)]
    chosen = bot._select_markets(
        [_scored("a", 3), _scored("b", 2), _scored("c", 1)])
    assert [m.condition_id for m in chosen] == ["a", "b"]
    bot.metrics.close()


def test_configure_logging_writes_utf8_daily_rotating_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = main.configure_logging()
    root.info("持久化测试消息")
    listener = main._LOG_LISTENER
    assert listener is not None
    file_handler = next(h for h in listener.handlers
                        if isinstance(h, TimedRotatingFileHandler))
    main.stop_logging()

    log_file = tmp_path / "logs" / "pmbot.log"
    assert "持久化测试消息" in log_file.read_text(encoding="utf-8")
    assert file_handler.when == "MIDNIGHT"
    assert file_handler.backupCount == 0
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

    monkeypatch.setattr(main, "TimedRotatingFileHandler", SlowFileHandler)
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
