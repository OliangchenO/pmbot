"""Lifecycle regressions for persisted reward-exit batch facts."""

import asyncio
import copy

from pmbot.books import BookTracker
from pmbot.gamma import Market
from pmbot import main
from pmbot.brokers import RestingOrder
from pmbot.main import Bot
from pmbot.strategy import Quote


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


def _record_rescan(bot: Bot) -> list[tuple[bool, bool, set[str]]]:
    calls = []

    async def record(*, initial=False, rotate=False, exclude_cids=None):
        calls.append((initial, rotate, set(exclude_cids or ())))

    bot._rescan = record
    return calls


def _open_take_batch(bot: Bot, market: Market, batch_id: str = "batch-1") -> None:
    bot.metrics.open_reward_exit_batch(
        batch_id=batch_id, cid=market.condition_id, origin_order_id="origin-order",
        origin_fill_id="origin-fill", origin_token_id=market.yes_token,
        complement_token_id=market.no_token, origin_size=10.0,
        origin_notional_usd=5.0, origin_fee_usd=0.0, take_target_size=20.0,
        created_ts=1.0, market_name=market.question,
    )


def test_reward_exit_batch_persists_market_name(tmp_path):
    """批次列表必须保留创建时的市场名称，避免只能靠 CID 反查。"""
    bot, market = _bot(tmp_path)

    _open_take_batch(bot, market)

    assert bot.metrics.get_reward_exit_batch("batch-1")["market_name"] == "Reward exit market"
    bot.metrics.close()


class _FillBroker:
    def __init__(self, fills):
        self.fills_log = fills
        self.cancelled = []

    def cancel_quotes_for_market(self, market):
        self.cancelled.append(market.condition_id)
        return True


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


def test_take_cost_guard_blocks_expensive_complement_and_keeps_batch_open(tmp_path):
    """删除 take 成本门控会在 0.86 ask 时再次提交确定性亏损的买单。"""
    class _TakeBroker:
        fills_log = []

        def __init__(self):
            self.calls = []

        def taker_buy(self, market, token_id, size, max_price, audit_context=None):
            self.calls.append((market, token_id, size, max_price, audit_context))
            return 0.0

    async def scenario():
        bot, market = _bot(tmp_path)
        bot.cfg["risk"]["reward_exit_max_pair_loss_cents"] = 8.0
        _open_take_batch(bot, market)
        bot.tracker.books[market.no_token].snapshot([], [{"price": "0.86", "size": "100"}])
        broker = _TakeBroker()
        bot.broker = broker

        await bot._advance_take_pending(
            market.condition_id, bot.metrics.get_reward_exit_batch("batch-1"), 10.0)

        assert broker.calls == []
        batch = bot.metrics.get_reward_exit_batch("batch-1")
        assert batch["status"] == "TAKE_BLOCKED"
        assert batch["manual_reason"] == "take_cost_guard"
        bot.metrics.close()

    asyncio.run(scenario())


def test_take_cost_guard_retries_blocked_batch_after_ask_recovers(tmp_path):
    """遗漏 TAKE_BLOCKED 调度会使价格恢复后的批次永久锁死。"""
    class _TakeBroker:
        fills_log = []

        def __init__(self):
            self.calls = []

        def taker_buy(self, market, token_id, size, max_price, audit_context=None):
            self.calls.append((market, token_id, size, max_price, audit_context))
            return 0.0

    async def scenario():
        bot, market = _bot(tmp_path)
        bot.cfg["risk"]["reward_exit_max_pair_loss_cents"] = 8.0
        _open_take_batch(bot, market)
        bot.metrics.update_reward_exit_batch(
            batch_id="batch-1", status="TAKE_BLOCKED", manual_reason="take_cost_guard")
        bot.tracker.books[market.no_token].snapshot([], [{"price": "0.50", "size": "100"}])
        broker = _TakeBroker()
        bot.broker = broker

        await bot._advance_reward_exit_batches(10.0)

        assert len(broker.calls) == 1
        assert bot.metrics.get_reward_exit_batch("batch-1")["status"] == "TAKE_PENDING"
        bot.metrics.close()

    asyncio.run(scenario())


def test_take_submission_does_not_persist_estimated_price_as_a_fill(tmp_path):
    """将限价或盘口价伪造为成交事实会低估 live batch 的实际成本。"""
    class _TakeBroker:
        fills_log = []

        def taker_buy(self, market, token_id, size, max_price, audit_context=None):
            return 6.0

    async def scenario():
        bot, market = _bot(tmp_path)
        _open_take_batch(bot, market)
        bot.tracker.books[market.no_token].snapshot([], [{"price": "0.51", "size": "100"}])
        bot.broker = _TakeBroker()

        await bot._advance_take_pending(
            market.condition_id, bot.metrics.get_reward_exit_batch("batch-1"), 10.0)

        batch = bot.metrics.get_reward_exit_batch("batch-1")
        assert batch["take_filled_size"] == 0.0
        assert batch["take_notional_usd"] == 0.0
        assert bot.metrics.list_reward_exit_fills("batch-1", "batch_take") == []
        bot.metrics.close()

    asyncio.run(scenario())


def test_take_waits_for_real_fill_before_retrying_after_timeout(tmp_path):
    """删除 broker 的待归因检查会在真实成交流到达前重复提交 FAK。"""
    class _TakeBroker:
        fills_log = []

        def __init__(self):
            self.calls = []
            self.pending = False

        def taker_buy(self, market, token_id, size, max_price, audit_context=None):
            self.calls.append((market, token_id, size, max_price, audit_context))
            self.pending = True
            return 10.0

        def has_pending_batch_take(self, cid, batch_id):
            return self.pending

        def clear_pending_batch_take(self, cid, batch_id):
            self.pending = False

    async def scenario():
        bot, market = _bot(tmp_path)
        _open_take_batch(bot, market)
        bot.tracker.books[market.no_token].snapshot([], [{"price": "0.51", "size": "100"}])
        broker = _TakeBroker()
        bot.broker = broker

        await bot._advance_take_pending(
            market.condition_id, bot.metrics.get_reward_exit_batch("batch-1"), 10.0)
        await bot._advance_take_pending(
            market.condition_id, bot.metrics.get_reward_exit_batch("batch-1"), 100.0)

        assert len(broker.calls) == 1

        bot.metrics.record_reward_exit_fill(
            fill_id="actual-take-1", batch_id="batch-1", order_id="take-1",
            intent="batch_take", cid=market.condition_id, token_id=market.no_token,
            side="NO", price=0.51, size=10.0, fee_usd=0.0, ts=101.0,
        )
        await bot._credit_take_fills(101.0)

        assert bot.metrics.get_reward_exit_batch("batch-1")["take_filled_size"] == 10.0
        assert not broker.pending

        await bot._advance_take_pending(
            market.condition_id, bot.metrics.get_reward_exit_batch("batch-1"), 102.0)
        assert len(broker.calls) == 2
        assert broker.calls[-1][2] == 10.0
        bot.metrics.close()

    asyncio.run(scenario())


def test_durable_pending_take_blocks_retry_after_bot_restart(tmp_path):
    """删除持久化提交检查会让重启后的批次再次提交同一笔 take。"""
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
        bot.tracker.books[market.no_token].snapshot([], [{"price": "0.51", "size": "100"}])
        bot.metrics.record_pending_batch_take(
            batch_id="batch-1", cid=market.condition_id,
            token_id=market.no_token, price=0.58, submitted_ts=99.0,
        )
        broker = _TakeBroker()
        bot.broker = broker

        await bot._advance_take_pending(
            market.condition_id, bot.metrics.get_reward_exit_batch("batch-1"), 100.0)

        assert broker.calls == []
        bot.metrics.close()

    asyncio.run(scenario())


def test_fragmented_take_fill_keeps_submission_locked_until_reported_size(tmp_path):
    """若首段成交就解锁，余下同一 FAK 成交会与补单叠加而超买。"""
    class _TakeBroker:
        fills_log = []

        def __init__(self):
            self.calls = []

        def taker_buy(self, market, token_id, size, max_price, audit_context=None):
            self.calls.append((market, token_id, size, max_price, audit_context))
            return 0.0

        def clear_pending_batch_take(self, cid, batch_id):
            raise AssertionError("partial fill must not release pending take")

    async def scenario():
        bot, market = _bot(tmp_path)
        _open_take_batch(bot, market)
        bot.tracker.books[market.no_token].snapshot([], [{"price": "0.51", "size": "100"}])
        pending_id = bot.metrics.record_pending_batch_take(
            batch_id="batch-1", cid=market.condition_id,
            token_id=market.no_token, price=0.58, submitted_ts=99.0,
        )
        bot.metrics.update_reward_exit_order(pending_id, expiration=10.0)
        bot.metrics.record_reward_exit_fill(
            fill_id="take-part-1", batch_id="batch-1", order_id="take-1",
            intent="batch_take", cid=market.condition_id, token_id=market.no_token,
            side="NO", price=0.51, size=5.0, fee_usd=0.0, ts=100.0,
        )
        bot.broker = _TakeBroker()

        await bot._credit_take_fills(100.0)
        await bot._advance_take_pending(
            market.condition_id, bot.metrics.get_reward_exit_batch("batch-1"), 101.0)

        assert bot.metrics.get_pending_batch_take("batch-1")["status"] == "PENDING"
        assert bot.broker.calls == []
        bot.metrics.close()

    asyncio.run(scenario())


def test_completed_durable_take_releases_rehydrated_context_without_new_fill(tmp_path):
    """重启后汇总已更新时，不能因没有新增成交流而永久锁住 take。"""
    class _TakeBroker:
        fills_log = []

        def __init__(self):
            self.cleared = []

        def clear_pending_batch_take(self, cid, batch_id):
            self.cleared.append((cid, batch_id))

    async def scenario():
        bot, market = _bot(tmp_path)
        _open_take_batch(bot, market)
        pending_id = bot.metrics.record_pending_batch_take(
            batch_id="batch-1", cid=market.condition_id,
            token_id=market.no_token, price=0.58, submitted_ts=99.0,
        )
        bot.metrics.update_reward_exit_order(pending_id, expiration=5.0)
        bot.metrics.record_reward_exit_fill(
            fill_id="take-complete-before-restart", batch_id="batch-1", order_id="take-1",
            intent="batch_take", cid=market.condition_id, token_id=market.no_token,
            side="NO", price=0.51, size=5.0, fee_usd=0.0, ts=100.0,
        )
        bot.metrics.update_reward_exit_batch(batch_id="batch-1", take_filled_size=5.0)
        bot.broker = _TakeBroker()

        await bot._credit_take_fills(101.0)

        assert bot.metrics.get_pending_batch_take("batch-1") is None
        assert bot.broker.cleared == [(market.condition_id, "batch-1")]
        bot.metrics.close()

    asyncio.run(scenario())


def test_normal_reward_fill_replays_from_metrics_after_broker_restart(tmp_path):
    """A restart must not lose a normal maker fill before its batch is opened."""
    async def scenario():
        bot, market = _bot(tmp_path)
        _record_rescan(bot)
        bot.metrics.record_reward_exit_fill(
            fill_id="durable-origin", batch_id="", order_id="normal-order",
            intent="normal_reward", cid=market.condition_id,
            token_id=market.no_token, side="NO", price=0.44, size=40.0,
            fee_usd=0.0, ts=10.0,
        )
        bot.broker = _FillBroker([])

        await bot._process_reward_fills(11.0)

        batch = bot.metrics.get_reward_exit_batch("reward-exit-durable-origin")
        assert batch is not None
        assert batch["origin_size"] == 40.0
        assert batch["take_target_size"] == 80.0
        bot.metrics.close()

    asyncio.run(scenario())


def test_first_reward_fill_cancels_once_and_every_fill_opens_its_own_take(tmp_path):
    """One market cancel guards every same-market reward fill, not only the first."""
    async def scenario():
        bot, market = _bot(tmp_path)
        rescan_calls = _record_rescan(bot)
        broker = _FillBroker([
            {"fill_id": "fill-1", "order_id": "order-1", "intent": "normal_reward",
             "cid": market.condition_id, "token": market.yes_token, "side": "YES",
             "price": 0.44, "size": 33.0, "ts": 10.0},
            {"fill_id": "fill-2", "order_id": "order-2", "intent": "normal_reward",
             "cid": market.condition_id, "token": market.no_token, "side": "NO",
             "price": 0.45, "size": 40.0, "ts": 11.0},
        ])
        bot.broker = broker

        await bot._process_reward_fills(12.0)

        assert broker.cancelled == [market.condition_id]
        assert rescan_calls == [(False, True, {market.condition_id})]
        assert bot.metrics.get_reward_exit_batch("reward-exit-fill-1")["take_target_size"] == 66.0
        assert bot.metrics.get_reward_exit_batch("reward-exit-fill-2")["take_target_size"] == 80.0
        bot.metrics.close()

    asyncio.run(scenario())


def test_cancel_failure_does_not_block_confirmed_reward_take(tmp_path):
    """A cancel error is logged, but the confirmed reward fill still gets 2q take."""
    class _CancelFailBroker(_FillBroker):
        def cancel_quotes_for_market(self, market):
            raise RuntimeError("exchange timeout")

    async def scenario():
        bot, market = _bot(tmp_path)
        rescan_calls = []

        async def record_rescan(*, initial=False, rotate=False, exclude_cids=None):
            rescan_calls.append((initial, rotate, set(exclude_cids or ())))

        bot._rescan = record_rescan
        bot.broker = _CancelFailBroker([
            {"fill_id": "fill-1", "order_id": "order-1", "intent": "normal_reward",
             "cid": market.condition_id, "token": market.yes_token, "side": "YES",
             "price": 0.44, "size": 33.0, "ts": 10.0},
        ])

        await bot._process_reward_fills(12.0)

        assert bot.metrics.get_reward_exit_batch("reward-exit-fill-1")["take_target_size"] == 66.0
        assert rescan_calls == [(False, True, {market.condition_id})]
        bot.metrics.close()

    asyncio.run(scenario())


def test_failed_quote_cancel_retries_while_reward_batch_is_open(tmp_path):
    """Cancellation retries independently; the existing batch remains eligible for take."""
    class _RetryBroker(_FillBroker):
        def __init__(self, fills):
            super().__init__(fills)
            self.attempts = 0

        def cancel_quotes_for_market(self, market):
            self.attempts += 1
            return self.attempts >= 2

    async def scenario():
        bot, market = _bot(tmp_path)
        _record_rescan(bot)
        broker = _RetryBroker([
            {"fill_id": "fill-1", "order_id": "order-1", "intent": "normal_reward",
             "cid": market.condition_id, "token": market.yes_token, "side": "YES",
             "price": 0.44, "size": 33.0, "ts": 10.0},
        ])
        bot.broker = broker

        await bot._process_reward_fills(12.0)
        assert bot.metrics.get_reward_exit_batch("reward-exit-fill-1") is not None
        assert broker.attempts == 1

        await bot._retry_reward_exit_quote_cancels(13.0)
        assert broker.attempts == 2
        assert bot._reward_exit_cancel_retries == {}
        bot.metrics.close()

    asyncio.run(scenario())


def test_sell_refresh_waits_for_reconcile_to_remove_cancelled_order(tmp_path):
    """A successful cancel request must not free shares until reconciliation does."""
    class _ExitBroker:
        fills_log = []

        def __init__(self):
            old = RestingOrder("old-exit", Quote("no-1", 0.62, 10.0), 1.0, 1)
            self._reward_exit_orders = {"batch-1": old}
            self._exit_orders = {}
            self.cancelled = []
            self.placed = []
            self.reconciled = 0
            self.old_still_open = True

        def cancel_reward_exit(self, batch_id):
            self.cancelled.append(batch_id)
            self._reward_exit_orders.pop(batch_id, None)
            return True

        def reconcile_orders(self):
            self.reconciled += 1
            if self.old_still_open:
                self._exit_orders = {
                    "cid-1": RestingOrder(
                        "old-exit", Quote("no-1", 0.62, 10.0), 1.0, 1)
                }
            else:
                self._exit_orders = {}
            return True

        def place_reward_exit(self, market, batch_id, quote, audit_context):
            self.placed.append((market, batch_id, quote, audit_context))
            return RestingOrder("replacement", quote, 100.0, 1000)

    async def scenario():
        bot, market = _bot(tmp_path)
        _open_take_batch(bot, market)
        bot.metrics.update_reward_exit_batch(
            batch_id="batch-1", status="SELL_PENDING", take_filled_size=20.0,
            exit_initial_size=10.0, exit_target_price=0.62,
        )
        bot.tracker.books[market.no_token].snapshot(
            [{"price": "0.61", "size": "100"}], [])
        broker = _ExitBroker()
        bot.broker = broker

        await bot._advance_sell_pending(
            market.condition_id, bot.metrics.get_reward_exit_batch("batch-1"), 100.0)

        assert broker.cancelled == ["batch-1"]
        assert broker.reconciled == 0
        assert broker.placed == []

        await bot._advance_sell_pending(
            market.condition_id, bot.metrics.get_reward_exit_batch("batch-1"), 101.0)

        assert broker.reconciled == 1
        assert broker.placed == []

        broker.old_still_open = False
        await bot._advance_sell_pending(
            market.condition_id, bot.metrics.get_reward_exit_batch("batch-1"), 102.0)

        assert len(broker.placed) == 1
        assert bot.metrics.get_reward_exit_order("batch-1")["status"] == "OPEN"
        bot.metrics.close()

    asyncio.run(scenario())


def test_missing_persisted_batch_sell_reconciles_then_replaces(tmp_path):
    """An exchange-confirmed missing persisted exit must not lock the batch forever."""
    class _ExitBroker:
        fills_log = []

        def __init__(self):
            self._reward_exit_orders = {}
            self._exit_orders = {}
            self._open_orders = {}
            self.reconciled = 0
            self.placed = []

        def reconcile_orders(self):
            self.reconciled += 1
            return True

        def place_reward_exit(self, market, batch_id, quote, audit_context):
            self.placed.append((market, batch_id, quote, audit_context))
            return RestingOrder("replacement", quote, 100.0, 1000)

    async def scenario():
        bot, market = _bot(tmp_path)
        _open_take_batch(bot, market)
        bot.metrics.update_reward_exit_batch(
            batch_id="batch-1", status="SELL_PENDING", take_filled_size=20.0,
            exit_initial_size=10.0, exit_target_price=0.62,
        )
        bot.metrics.record_reward_exit_order(
            order_id="stale-exit", batch_id="batch-1", intent="batch_exit",
            cid=market.condition_id, token_id=market.no_token, side="SELL",
            price=0.62, size=10.0, expiration=200.0, status="OPEN",
        )
        bot.tracker.books[market.no_token].snapshot(
            [{"price": "0.61", "size": "100"}], [])
        broker = _ExitBroker()
        bot.broker = broker

        await bot._advance_sell_pending(
            market.condition_id, bot.metrics.get_reward_exit_batch("batch-1"), 100.0)

        assert broker.reconciled == 1
        assert len(broker.placed) == 1
        assert bot.metrics.get_reward_exit_order("batch-1")["order_id"] == "replacement"
        bot.metrics.close()

    asyncio.run(scenario())


def test_failed_batch_sell_reconciles_and_backs_off_before_retry(tmp_path):
    """A rejected SELL cannot be reposted on every reward-exit tick."""
    class _RejectingExitBroker:
        fills_log = []
        _reward_exit_orders = {}
        _exit_orders = {}
        _open_orders = {}

        def __init__(self):
            self.place_attempts = 0
            self.reconcile_attempts = 0

        def place_reward_exit(self, market, batch_id, quote, audit_context):
            self.place_attempts += 1
            return None

        def reconcile_orders(self):
            self.reconcile_attempts += 1
            return True

    async def scenario():
        bot, market = _bot(tmp_path)
        _open_take_batch(bot, market)
        bot.metrics.update_reward_exit_batch(
            batch_id="batch-1", status="SELL_PENDING", take_filled_size=20.0,
            exit_initial_size=10.0, exit_target_price=0.62,
        )
        bot.tracker.books[market.no_token].snapshot(
            [{"price": "0.61", "size": "100"}], [])
        broker = _RejectingExitBroker()
        bot.broker = broker

        batch = bot.metrics.get_reward_exit_batch("batch-1")
        await bot._advance_sell_pending(market.condition_id, batch, 100.0)
        await bot._advance_sell_pending(market.condition_id, batch, 101.0)

        assert broker.place_attempts == 1
        assert broker.reconcile_attempts == 1
        bot.metrics.close()

    asyncio.run(scenario())
