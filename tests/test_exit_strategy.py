"""Bot integration checks for the price-aware exit gate."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import yaml

import pytest

from pmbot.books import Book, BookTracker
from pmbot.brokers import PaperBroker
from pmbot.gamma import Market
from pmbot.main import Bot


BASE_CFG = {
    "mode": "paper", "capital_usd": 500,
    "metrics": {"db_path": ":memory:"},
    "quoting": {"requote_move_cents": 0.5},
    "guards": {
        "vol_window_secs": 60, "vol_max_move_cents": 3.0,
        "max_same_side_fills": 3, "same_side_window_minutes": 15,
        "market_cooldown_minutes": 45, "velocity_window_secs": 10,
        "velocity_max_trades": 8, "directional_consecutive": 5,
        "side_cooldown_minutes": 10, "flow_window_secs": 300,
        "flow_min_volume_shares": 200, "flow_widen_threshold": 0.6,
        "flow_pull_threshold": 0.85, "flow_widen_max_cents": 2.0,
        "markout_horizons_secs": [30, 300], "markout_window_minutes": 120,
        "markout_min_samples": 3, "markout_trip_cents": -1.5,
    },
}


def _market() -> Market:
    return Market("Will it rain tomorrow?", "cid1", "y1", "n1", 5,
                  3, 50, 1000, 500, 0.01, None, False)


def _cfg(**overrides) -> dict:
    cfg = {**BASE_CFG, "exit_strategy": {
        "enabled": True, "mode": "active", "min_shares": 5.0,
        "same_side_target": 0.01, "complement_target": 0.01,
        "complement_taker_enabled": True,
        "min_exit_price": 0.05, "max_exit_price": 0.95,
        "progressive": {"enabled": True, "phases": [
            {"max_hours": 2, "same_side_target": 0.01},
            {"max_hours": 8, "same_side_target": 0.005},
            {"max_hours": 24, "same_side_target": 0.0},
        ], "max_loss_cents": 1.0},
    }}
    cfg["exit_strategy"].update(overrides)
    return cfg


def _book(token: str, bids: dict[float, float], asks: dict[float, float]) -> Book:
    book = Book(token)
    book.bids, book.asks, book.min_order_size = bids, asks, 5.0
    return book


def test_live_config_keeps_price_exit_disabled_and_retains_exit_orders():
    cfg = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
    assert cfg["mode"] == "live"
    assert cfg["exit_strategy"]["enabled"] is False
    assert cfg["risk"]["exit_order_ttl_secs"] == 1800
    assert cfg["guards"]["markout_horizons_secs"] == [30, 300, 900, 3600]


@pytest.mark.parametrize(("hours", "expected"), [
    (1, 0.56), (3, 0.555), (10, 0.55), (25, 0.54),
])
def test_exit_target_progresses_by_inventory_age(hours, expected):
    bot = Bot(_cfg())
    bot._over_since["cid1"] = time.time() - hours * 3600
    assert bot._get_current_exit_target("cid1", 0.55, time.time()) == pytest.approx(expected)


@pytest.mark.parametrize("kwargs", [
    {"enabled": False},
    {"min_shares": 20.0},
])
def test_exit_gate_skips_when_disabled_or_below_minimum(kwargs):
    bot = Bot(_cfg(**kwargs))
    result = asyncio.run(bot._exit_strategy_check(
        _market(), "cid1", 10.0, 0.55, time.time(), False))
    assert result is None


def test_exit_gate_skips_urgent_or_unknown_basis():
    bot = Bot(_cfg())
    market = _market()
    assert asyncio.run(bot._exit_strategy_check(market, "cid1", 10.0, None, time.time(), False)) is None
    assert asyncio.run(bot._exit_strategy_check(market, "cid1", 10.0, 0.55, time.time(), True)) is None


def test_same_side_exit_uses_executable_profit_floor(tmp_path):
    bot = Bot(_cfg())
    market = _market()
    bot.tracker = BookTracker([market.yes_token, market.no_token])
    bot.tracker.books[market.yes_token] = _book("y1", {0.57: 10.0}, {0.58: 10.0})
    bot.broker = PaperBroker(500.0, bot.tracker, data_dir=str(tmp_path))
    bot._over_since["cid1"] = time.time() - 60

    result = asyncio.run(bot._exit_strategy_check(market, "cid1", 10.0, 0.55, time.time(), False))

    assert result == "same_side_exit_open"
    assert bot.broker.exit_quote(market).price == pytest.approx(0.56)
    assert bot._exit_state["cid1"]["status"] == "SAME_SIDE_EXIT_OPEN"


def test_same_side_exit_does_not_require_complement_book(tmp_path):
    bot = Bot(_cfg())
    market = _market()
    bot.tracker = BookTracker([market.yes_token, market.no_token])
    bot.tracker.books[market.yes_token] = _book("y1", {0.57: 10.0}, {0.58: 10.0})
    bot.tracker.books.pop(market.no_token)
    bot.broker = PaperBroker(500.0, bot.tracker, data_dir=str(tmp_path))

    assert asyncio.run(bot._exit_strategy_check(
        market, "cid1", 10.0, 0.55, time.time(), False)) == "same_side_exit_open"


def test_same_side_exit_needs_only_an_executable_bid(tmp_path):
    bot = Bot(_cfg())
    market = _market()
    bot.tracker = BookTracker([market.yes_token, market.no_token])
    bot.tracker.books[market.yes_token] = _book("y1", {0.57: 10.0}, {})
    bot.broker = PaperBroker(500.0, bot.tracker, data_dir=str(tmp_path))

    assert asyncio.run(bot._exit_strategy_check(
        market, "cid1", 10.0, 0.55, time.time(), False)) == "same_side_exit_open"
    assert bot.broker.exit_quote(market).price == pytest.approx(0.56)


def test_shadow_mode_records_decision_without_order_write(tmp_path):
    bot = Bot(_cfg(mode="shadow"))
    market = _market()
    bot.tracker = BookTracker([market.yes_token, market.no_token])
    bot.tracker.books[market.yes_token] = _book("y1", {0.57: 10.0}, {0.58: 10.0})
    bot.broker = PaperBroker(500.0, bot.tracker, data_dir=str(tmp_path))

    assert asyncio.run(bot._exit_strategy_check(
        market, "cid1", 10.0, 0.55, time.time(), False)) == "shadow"
    assert asyncio.run(bot._exit_strategy_check(
        market, "cid1", 10.0, 0.55, time.time(), False)) == "shadow"
    assert bot.broker.exit_quote(market) is None
    assert bot.metrics._conn.execute(
        "SELECT mode FROM exit_events WHERE event='decision'").fetchone() == ("shadow",)
    assert bot.metrics._conn.execute("SELECT COUNT(*) FROM exit_events").fetchone() == (1,)
