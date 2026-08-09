"""Tests for P0 adverse-selection guard wiring in Bot quote loop."""

import asyncio
import copy
import sys
import time
from collections import deque
from unittest.mock import MagicMock

import pytest

# tomllib is 3.11+; mock it for Python 3.10 test runners.
if sys.version_info < (3, 11):
    import types
    sys.modules.setdefault("tomllib", types.ModuleType("tomllib"))
    import yaml as _yaml
    # patch tomllib.load so config loading still works
    sys.modules["tomllib"].load = staticmethod(_yaml.safe_load)  # type: ignore[attr-defined]

from pmbot import main, strategy
from pmbot.books import Book, BookTracker
from pmbot.brokers import PaperBroker, Position
from pmbot.gamma import Market
from pmbot.main import Bot
from pmbot.risk import QuoteRiskDecision
from pmbot.strategy import Quote


BASE_CFG = {
    "mode": "paper",
    "capital_usd": 500,
    "scanner": {
        "top_n_markets": 1,
        "sticky_swap": False,
        "refresh_minutes": 30,
    },
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
        "max_book_staleness_secs": 25,
        "fade_cents_per_fill": 0.5,
        "fade_window_minutes": 5,
        "fade_max_cents": 2.0,
        # P0 defaults
        "quote_risk_mode": "shadow",
        "quote_risk_widen_score": 0.60,
        "quote_risk_pull_score": 0.85,
        "quote_risk_resume_score": 0.45,
    },
    "quoting": {
        "offset_frac_of_max_spread": 0.35,
        "size_mult_of_min": 1.0,
        "max_capital_per_market": 50,
        "skew_strength": 0.6,
        "requote_move_cents": 0.4,
        "order_ttl_secs": 180,
        "refresh_overlap": False,
        "max_book_spread_mult_of_band": 3.0,
        "flow_drift_max_cents": 1.0,
        "adaptive_markout_gain": 1.0,
        "adaptive_tighten_max_cents": 0.5,
        "adaptive_widen_max_cents": 2.0,
        "size_turnover_penalty": 0.0,
        "size_markout_penalty": 0.0,
    },
    "risk": {
        "max_inventory_usd_per_market": 60,
        "max_total_inventory_usd": 250,
        "daily_loss_limit_usd": 25,
        "hard_kill_loss_usd": 50,
        "flatten_threshold_usd": 15,
        "flatten_after_secs": 90,
        "flatten_max_spread_cents": 4.0,
        "derisk_hours_before_end": 12,
        "derisk_widen_cents": 2.0,
        "exit_hours_before_end": 2,
        "recovery_soft_window_minutes": 5,
        "recovery_max_loss_cents": 2.0,
        "recovery_escalate_after_minutes": 30,
        "theme_max_inventory_usd": 0,
    },
}


def _market(cid="cid1") -> Market:
    from pmbot.gamma import Market as M
    return M(
        question="Will it rain tomorrow?", condition_id=cid,
        yes_token=f"{cid}yes", no_token=f"{cid}no", min_size=10,
        max_spread_cents=3, daily_pool=50, liquidity=1000,
        volume_24h=500, tick=0.01, end_date=None, neg_risk=False,
    )


class _FakeBroker:
    """Minimal broker for the bot only needs these attributes for recovery."""
    def __init__(self):
        self.held_markets = lambda: []
        self.fills_log = []
        self.open_quotes = lambda m: []
        self.unpaired_shares = lambda m: 0.0
        self.unpaired_cost_basis = None
        self.due_for_refresh = lambda m: False
        self.has_pending_hedge = lambda cid: False
        self.metrics = None

    def set_quotes(self, market, quotes, audit_context=None):
        pass

    def cancel_quotes(self):
        pass

    def cancel_all(self):
        pass

    def net_yes_exposure_usd(self, m=None):
        return 0.0

    def total_inventory_usd(self):
        return 0.0

    def equity(self):
        return 500.0

    def exit_quote(self, m):
        return None

    def set_exit(self, m, quote):
        pass

    def accrue_rewards(self, usd):
        pass

    def last_fill_ts(self, cid):
        return None


def _bot(tmp_path) -> Bot:
    # Build cfg from scratch to avoid filesystem dependency on config.debug.yaml
    cfg = copy.deepcopy(BASE_CFG)
    cfg["metrics"] = {"db_path": str(tmp_path / "metrics.db")}
    # Add controller disabled so it does not try to mutate config
    cfg["controller"] = {"enabled": False}
    # Add risk recovery settings that _quote_all reads
    cfg["risk"]["recovery_soft_window_minutes"] = 5
    cfg["risk"]["recovery_max_loss_cents"] = 2.0
    cfg["risk"]["recovery_escalate_after_minutes"] = 30
    cfg["risk"]["passive_exit"] = False
    cfg["risk"]["theme_max_inventory_usd"] = 0
    cfg["risk"]["exit_order_ttl_secs"] = 600
    bot = Bot(cfg)
    return bot


def _setup(bot: Bot, tmp_path, market: Market) -> PaperBroker:
    tracker = BookTracker([market.yes_token, market.no_token])
    broker = PaperBroker(500.0, tracker, data_dir=str(tmp_path))
    bot.tracker = tracker
    bot.broker = broker
    bot.markets = [market]
    bot._token_market = {market.yes_token: market, market.no_token: market}
    tracker.last_msg_ts = time.time()
    tracker.books[market.yes_token].snapshot(
        [{"price": "0.50", "size": "100"}], [{"price": "0.51", "size": "100"}],
    )
    tracker.books[market.no_token].snapshot(
        [{"price": "0.49", "size": "100"}], [{"price": "0.50", "size": "100"}],
    )
    return broker


# ── Shadow mode: records decisions but does NOT change quotes ──

def test_shadow_mode_does_not_change_quotes(tmp_path, monkeypatch):
    """shadow 只记录、不改变 quote。"""
    async def scenario():
        bot, market = _setup_quote_bot(tmp_path, monkeypatch)
        bot.guards.quote_risk_mode = "shadow"

        # Inject flow that would trigger a widen
        now = time.time()
        bot.guards._flow[market.condition_id] = deque([(now, 1.0)] * 190 + [(now, -1.0)] * 10)

        # The guards should produce a decision with pull/widen on NO side
        decision = bot.guards.quote_risk_decision(
            market, now, markout_avg=None, markout_samples=0)
        assert decision.no_action in ("widen", "pull")

        # Neutralise the old check_flow guard so it doesn't independently
        # cancel quotes — we want to isolate the P0 guard behaviour.
        monkeypatch.setattr(bot.guards, "check_flow", lambda _m, _t: (0.0, 0.0))

        # But shadow mode means Bot does NOT apply it to actual quotes
        sent = []

        async def record_set_quotes(_market, quotes, audit_context=None):
            sent.append((quotes, audit_context))

        monkeypatch.setattr(bot, "_set_quotes_locked", record_set_quotes)
        await bot._quote_all()

        # Should have posted the normal two-sided quotes unchanged
        assert len(sent) == 1
        quotes, _ = sent[0]
        assert len(quotes) == 2  # both sides still present
        bot.metrics.close()

    asyncio.run(scenario())


# ── Active mode: widens or pulls dangerous side ──

def test_active_mode_widens_dangerous_side(tmp_path, monkeypatch):
    """active 模式执行 widen 动作。"""
    async def scenario():
        bot, market = _setup_quote_bot(tmp_path, monkeypatch)
        bot.guards.quote_risk_mode = "active"

        # Inject flow that triggers a widen on NO side
        now = time.time()
        bot.guards._flow[market.condition_id] = deque([(now, 1.0)] * 170 + [(now, -1.0)] * 30)

        sent = []

        async def record_set_quotes(_market, quotes, audit_context=None):
            sent.append((list(quotes), audit_context))

        monkeypatch.setattr(bot, "_set_quotes_locked", record_set_quotes)
        await bot._quote_all()

        assert len(sent) == 1
        quotes, _ = sent[0]
        # Both sides quoted but NO side is widened
        yes_q = next((q for q in quotes if q.token_id == market.yes_token), None)
        no_q = next((q for q in quotes if q.token_id == market.no_token), None)
        assert yes_q is not None
        assert no_q is not None
        # NO bid should be wider (lower price) than the normal 0.49
        assert no_q.price < 0.50  # widened from normal ~0.50
        bot.metrics.close()

    asyncio.run(scenario())


def test_active_mode_pulls_dangerous_side(tmp_path, monkeypatch):
    """active 模式执行 pull 动作。"""
    async def scenario():
        bot, market = _setup_quote_bot(tmp_path, monkeypatch)
        bot.guards.quote_risk_mode = "active"

        # Inject flow that triggers a pull on NO side (0.95 imbalance)
        now = time.time()
        bot.guards._flow[market.condition_id] = deque([(now, 1.0)] * 195 + [(now, -1.0)] * 5)

        sent = []

        async def record_set_quotes(_market, quotes, audit_context=None):
            sent.append((list(quotes), audit_context))

        monkeypatch.setattr(bot, "_set_quotes_locked", record_set_quotes)
        await bot._quote_all()

        assert len(sent) == 1
        quotes, _ = sent[0]
        # Only YES side remains — NO side was pulled
        assert len(quotes) == 1
        assert quotes[0].token_id == market.yes_token
        bot.metrics.close()

    asyncio.run(scenario())


# ── Recovery orders bypass the decision engine ──

def test_recovery_quote_bypasses_decision_engine(tmp_path, monkeypatch):
    """恢复订单带 inventory_recovery path 时不经过决策器。"""
    async def scenario():
        bot, market = _setup_quote_bot(tmp_path, monkeypatch)
        bot.guards.quote_risk_mode = "active"

        # Inject flow that would trigger pull on NO side
        now = time.time()
        bot.guards._flow[market.condition_id] = deque([(now, 1.0)] * 195 + [(now, -1.0)] * 5)
        # But also inject unpaired inventory so the NO quote is a recovery order
        bot.broker.unpaired_shares = MagicMock(return_value=12.0)
        bot.broker.unpaired_cost_basis = MagicMock(return_value=0.45)

        # Override recovery path tracking — set it before quote_all
        # The market is still in selected set (only 1 market, top_n=1)
        bot.markets = [market]

        sent = []

        async def record_set_quotes(_market, quotes, audit_context=None):
            sent.append((list(quotes), audit_context))

        monkeypatch.setattr(bot, "_set_quotes_locked", record_set_quotes)
        await bot._quote_all()

        assert len(sent) == 1
        quotes, audit = sent[0]
        # Recovery order — only complement side (NO) is quoted
        assert len(quotes) == 1
        assert quotes[0].token_id == market.no_token
        # It should have the recovery_order flag in audit context
        assert audit[market.no_token].get("recovery_order") is True
        bot.metrics.close()

    asyncio.run(scenario())


# ── Mode off ──

def test_off_mode_leaves_quotes_unchanged(tmp_path, monkeypatch):
    """off 模式不改变任何 quote。"""
    async def scenario():
        bot, market = _setup_quote_bot(tmp_path, monkeypatch)
        bot.guards.quote_risk_mode = "off"

        # Inject strong flow — P0 guard returns allow in off mode,
        # but the old check_flow guard also sees it.  Neutralise it so
        # the test isolates the P0 decision path.
        now = time.time()
        bot.guards._flow[market.condition_id] = deque([(now, 1.0)] * 195 + [(now, -1.0)] * 5)
        monkeypatch.setattr(bot.guards, "check_flow", lambda _m, _t: (0.0, 0.0))

        sent = []

        async def record_set_quotes(_market, quotes, audit_context=None):
            sent.append((list(quotes), audit_context))

        monkeypatch.setattr(bot, "_set_quotes_locked", record_set_quotes)
        await bot._quote_all()

        assert len(sent) == 1
        quotes, _ = sent[0]
        assert len(quotes) == 2  # unchanged
        bot.metrics.close()

    asyncio.run(scenario())


# ── Helpers ──

def _setup_quote_bot(tmp_path, monkeypatch):
    """Build a bot with a real market and fake strategy returning two-sided quotes."""
    bot = _bot(tmp_path)
    market = _market()
    _setup(bot, tmp_path, market)
    from pmbot.risk import RiskManager
    bot.risk = RiskManager(bot.cfg, 500.0)
    # Fake strategy always returns two-sided quotes
    monkeypatch.setattr(
        strategy, "compute_quotes",
        lambda *_args, **_kwargs: [
            Quote(market.yes_token, 0.47, 10.0),
            Quote(market.no_token, 0.50, 10.0),
        ],
    )
    monkeypatch.setattr(bot, "_log_inventory_recovery_quote", lambda *_args, **_kwargs: None)
    return bot, market
