"""Lifecycle regressions for persisted reward-exit batch facts."""

import asyncio
import copy

from pmbot.books import BookTracker
from pmbot.gamma import Market
from pmbot import main
from pmbot.main import Bot


def _market() -> Market:
    return Market(
        question="Reward exit market", condition_id="cid-1", yes_token="yes-1",
        no_token="no-1", min_size=10, max_spread_cents=3, daily_pool=50,
        liquidity=1000, volume_24h=500, tick=0.01, end_date=None,
        neg_risk=False,
    )


def _bot(tmp_path) -> tuple[Bot, Market]:
    cfg = copy.deepcopy(main.load_config("config.debug.yaml"))
    cfg["metrics"] = {"db_path": str(tmp_path / "metrics.db")}
    cfg["risk"]["reward_exit_batch_mode"] = "active"
    bot = Bot(cfg)
    market = _market()
    bot.tracker = BookTracker([market.yes_token, market.no_token])
    bot.markets = [market]
    bot._token_market = {market.yes_token: market, market.no_token: market}
    return bot, market


def _open_take_batch(bot: Bot, market: Market, batch_id: str = "batch-1") -> None:
    bot.metrics.open_reward_exit_batch(
        batch_id=batch_id, cid=market.condition_id, origin_order_id="origin-order",
        origin_fill_id="origin-fill", origin_token_id=market.yes_token,
        complement_token_id=market.no_token, origin_size=10.0,
        origin_notional_usd=5.0, origin_fee_usd=0.0, take_target_size=20.0,
        created_ts=1.0,
    )


class _FillBroker:
    def __init__(self, fills):
        self.fills_log = fills


def test_take_credit_persists_only_own_batch_fill_once_and_ignores_forced_hedge(tmp_path):
    """Direct credit in _advance_take_pending is the authoritative writer;
    _credit_take_fills reads persisted fills and updates batch totals."""
    async def scenario():
        bot, market = _bot(tmp_path)
        _open_take_batch(bot, market)
        own = {
            "id": "take-fill-1", "order_id": "take-order", "batch_id": "batch-1",
            "intent": "batch_take", "path": "reward_exit_take", "taker": True,
            "cid": market.condition_id, "token": market.no_token, "side": "BUY",
            "price": 0.51, "size": 6.0, "fee": 0.03, "ts": 10.0,
        }
        # Direct credit pre-populates the reward_exit_fills table — this is
        # what _advance_take_pending() does after FAK execution.
        bot.metrics.record_reward_exit_fill(
            fill_id="take-fill-1", batch_id="batch-1",
            order_id="take-order", intent="batch_take",
            cid=market.condition_id, token_id=market.no_token,
            side="BUY", price=0.51, size=6.0, fee_usd=0.03, ts=10.0,
        )
        # fills_log is present but _credit_take_fills no longer writes from it
        # for batch_take fills — it reads already-persisted fills.
        bot.broker = _FillBroker([own, dict(own), {
            "id": "forced-fill", "order_id": "forced-order", "intent": "forced_hedge",
            "path": "forced_hedge", "taker": True, "cid": market.condition_id,
            "token": market.no_token, "side": "BUY", "price": 0.99,
            "size": 100.0, "fee": 1.0, "ts": 11.0,
        }])

        await bot._credit_take_fills(12.0)
        await bot._credit_take_fills(13.0)

        batch = bot.metrics.get_reward_exit_batch("batch-1")
        assert batch["take_filled_size"] == 6.0
        assert batch["take_notional_usd"] == 3.06
        assert batch["take_fee_usd"] == 0.03
        assert [fill["fill_id"] for fill in bot.metrics.list_reward_exit_fills(
            "batch-1", "batch_take")] == ["take-fill-1"]
        bot.metrics.close()

    asyncio.run(scenario())


def test_take_submission_uses_remaining_size_and_batch_audit_context(tmp_path):
    """Dropping the batch context or using 2q again would create an unowned overfill."""
    class _TakeBroker:
        fills_log = []

        def __init__(self):
            self.calls = []

        def taker_buy(self, market, token_id, size, max_price, audit_context=None):
            self.calls.append((market, token_id, size, max_price, audit_context))
            return 0.0

    async def scenario():
        bot, market = _bot(tmp_path)
        _open_take_batch(bot, market)
        bot.metrics.update_reward_exit_batch(batch_id="batch-1", take_filled_size=6.0)
        bot.tracker.books[market.no_token].snapshot([], [{"price": "0.51", "size": "100"}])
        broker = _TakeBroker()
        bot.broker = broker

        await bot._advance_take_pending(
            market.condition_id, bot.metrics.get_reward_exit_batch("batch-1"), 10.0)

        assert broker.calls[0][2] == 14.0
        assert broker.calls[0][4]["path"] == "reward_exit_take"
        assert broker.calls[0][4]["intent"] == "batch_take"
        assert broker.calls[0][4]["batch_id"] == "batch-1"
        assert broker.calls[0][4]["best_ask"] == 0.51
        bot.metrics.close()

    asyncio.run(scenario())
