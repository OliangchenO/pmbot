"""Lifecycle tests: a price decision is not a completed inventory exit."""

from __future__ import annotations

import asyncio
import copy
import time

from pmbot.books import BookTracker
from pmbot.brokers import PaperBroker
from pmbot.gamma import Market
from pmbot.main import Bot, load_config


class ZeroFillPaperBroker(PaperBroker):
    def taker_buy(self, market, token_id, size, max_price):
        return 0.0


def _market() -> Market:
    return Market(
        question="Will it rain tomorrow?", condition_id="cid-1",
        yes_token="yes", no_token="no", min_size=5.0,
        max_spread_cents=3.0, daily_pool=50.0, liquidity=1_000.0,
        volume_24h=500.0, tick=0.01, end_date=None, neg_risk=False,
    )


def _bot(tmp_path, broker_type=PaperBroker) -> tuple[Bot, Market]:
    cfg = copy.deepcopy(load_config("config.debug.yaml"))
    cfg["mode"] = "paper"
    cfg["metrics"] = {"db_path": str(tmp_path / "metrics.db")}
    cfg["exit_strategy"] = {
        "enabled": True,
        "mode": "active",
        "min_shares": 5.0,
        "same_side_target": 0.01,
        "complement_target": 0.01,
        "complement_taker_enabled": True,
        "progressive": {"enabled": False},
    }
    bot = Bot(cfg)
    market = _market()
    bot.tracker = BookTracker([market.yes_token, market.no_token])
    bot.broker = broker_type(500.0, bot.tracker, data_dir=str(tmp_path))
    bot._over_since[market.condition_id] = time.time() - 60.0
    return bot, market


def test_same_side_exit_blocks_forced_hedge_in_the_same_cycle(tmp_path):
    bot, market = _bot(tmp_path)
    bot.tracker.books[market.yes_token].snapshot(
        [{"price": "0.57", "size": "10"}], [{"price": "0.58", "size": "10"}], "5")

    result = asyncio.run(bot._exit_strategy_check(
        market, market.condition_id, 10.0, 0.55, time.time(), False,
    ))

    assert result == "same_side_exit_open"
    assert bot.broker.exit_quote(market).price == 0.56
    assert bot._exit_state[market.condition_id]["status"] == "SAME_SIDE_EXIT_OPEN"


def test_zero_fill_complement_keeps_inventory_age_and_reports_zero_fill(tmp_path):
    bot, market = _bot(tmp_path, ZeroFillPaperBroker)
    started = bot._over_since[market.condition_id]
    bot.tracker.books[market.no_token].snapshot(
        [{"price": "0.42", "size": "10"}], [{"price": "0.43", "size": "10"}], "5")

    result = asyncio.run(bot._exit_strategy_check(
        market, market.condition_id, 10.0, 0.55, time.time(), False,
    ))

    assert result == "zero_fill"
    assert bot._over_since[market.condition_id] == started
    assert bot._exit_state[market.condition_id]["status"] == "IDLE"
