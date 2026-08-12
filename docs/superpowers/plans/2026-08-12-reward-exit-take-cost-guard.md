# Reward-exit 双倍 take 成本保护 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 reward-exit 双倍 take 加入每对 8¢ 成本上限，并保证超限批次和 take 后残余仓持续受 batch lock 管理。

**Architecture:** 在纯状态机层加入 `TAKE_BLOCKED` 以及可测试的最大允许互补价格计算；编排层仅在当前 ask 可接受时提交 take。所有 batch 成本与 SELL 状态仍由持久化的交易所确认 fill 重建，提交订单不构成成交事实。

**Tech Stack:** Python 3, pytest, SQLite `MetricsStore`, 现有 `PaperBroker`/`LiveBroker` 与 `BookTracker`。

## Global Constraints

- 每笔 `normal_reward` 独立 take `2q`；不可跨 batch 净额化。
- 最大每对总成本为 `$1.08`，包括原始成交、互补 ask 与预计 taker 手续费。
- 超限不提交自动 taker 单，保留 CID lock；不改通用 P1 recovery 行为。
- `TAKE_BLOCKED` 与 `SELL_PENDING` 不因批次年龄或启动清理关闭。
- 实际 fill 的 `fill_id`、价格和手续费是成本与完成数量的唯一来源。
- 不提交 Git；不重启机器人、不取消或新建真实订单。

---

## 文件边界

- `pmbot/reward_exit.py`：batch 合法状态迁移，以及从原始成交/互补价格/手续费估计可接受性。
- `pmbot/main.py`：读取 `risk.reward_exit_max_pair_loss_cents`，在 `_advance_take_pending` 前做成本门控；维护 blocked batch lock 与持久 fill 重建。
- `config.yaml`：显式启用 8.0¢/对上限。
- `tests/test_reward_exit.py`：纯状态和成本计算测试。
- `tests/test_reward_exit_lifecycle.py`：PaperBroker 生命周期、blocked 重试与真实 fill seal 测试。
- `tests/test_main.py`：重启清理不会释放 `TAKE_BLOCKED` CID lock。

### Task 1: 纯状态机与成本门控

**Files:**
- Modify: `pmbot/reward_exit.py:17-29, 341-468`
- Test: `tests/test_reward_exit.py`

**Interfaces:**
- Produces `BatchStatus` 中的 `TAKE_BLOCKED`。
- Produces `max_take_price_for_pair(origin_price: float, market: Market, max_pair_loss_cents: float) -> float`，返回能使原始价、互补价和预计买入手续费合计不超过 `$1 + cents/100` 的最大互补价格。
- Produces `transition_to_take_blocked(batch: RewardExitBatch, reason: str, updated_ts: float) -> RewardExitBatch`，保留既有累计 take 字段并设置状态和机器可读原因。

- [ ] **Step 1: 写入失败测试：超限状态可持久保留并可恢复至 TAKE_PENDING**

```python
def test_take_blocked_transition_preserves_progress_and_allows_retry():
    batch = create_batch(..., origin_size=20, origin_price=0.59, created_ts=1.0)
    blocked = transition_to_take_blocked(
        batch, reason="take_cost_guard", updated_ts=2.0,
    )

    assert blocked.status == "TAKE_BLOCKED"
    assert blocked.manual_reason == "take_cost_guard"
    assert blocked.take_filled_size == 0.0
    assert valid_transition("TAKE_BLOCKED", "TAKE_PENDING")
```

- [ ] **Step 2: 运行失败测试**

Run: `rtk pytest tests/test_reward_exit.py -k take_blocked -q`

Expected: FAIL，因为状态和 transition helper 尚不存在。

- [ ] **Step 3: 写入失败测试：8¢ 上限拒绝 0.86，接受接近公平的 ask**

```python
def test_max_take_price_for_pair_enforces_eight_cent_loss_cap():
    market = _market_with_taker_fee(...)

    cap = max_take_price_for_pair(0.59, market, 8.0)

    assert 0.41 <= cap < 0.86
```

- [ ] **Step 4: 运行失败测试**

Run: `rtk pytest tests/test_reward_exit.py -k max_take_price -q`

Expected: FAIL，因为 `max_take_price_for_pair` 尚不存在。

- [ ] **Step 5: 实现最小状态机与价格计算**

```python
BatchStatus = Literal[
    "TAKE_PENDING", "TAKE_BLOCKED", "SELL_PENDING", "CLOSED", "MANUAL_HOLD",
]

def max_take_price_for_pair(origin_price, market, max_pair_loss_cents):
    ceiling = 1.0 + max_pair_loss_cents / 100.0
    price = ceiling - origin_price
    while price > 0 and origin_price + price + _buy_fee_per_share(market, price) > ceiling:
        price -= market.tick
    return max(0.0, math.floor(price / market.tick + 1e-12) * market.tick)
```

Add `TAKE_PENDING -> TAKE_BLOCKED` and `TAKE_BLOCKED -> TAKE_PENDING` to `_VALID_TRANSITIONS`; preserve every accumulated accounting field in `transition_to_take_blocked`.

- [ ] **Step 6: 运行纯状态机测试**

Run: `rtk pytest tests/test_reward_exit.py -q`

Expected: PASS。

### Task 2: 编排层的 take 门控与真实 fill 封存

**Files:**
- Modify: `pmbot/main.py:_run_reward_exit_batch_tick, _advance_reward_exit_batches, _advance_take_pending, _credit_take_fills`
- Modify: `config.yaml:risk`
- Test: `tests/test_reward_exit_lifecycle.py`

**Interfaces:**
- Consumes `reward_exit_max_pair_loss_cents`，缺省值 `8.0`。
- Consumes Task 1 的 `max_take_price_for_pair` 和 `TAKE_BLOCKED`。
- Produces `BATCH_TAKE_BLOCKED` 审计日志，包含 batch ID、原始均价、当前 ask、最大允许 ask 与原因。

- [ ] **Step 1: 写入失败生命周期测试：0.59 原始成交与 0.86 ask 不得下单且 batch 保持锁定**

```python
async def scenario():
    bot, market = _bot(tmp_path)
    _open_take_batch(bot, market, origin_price=0.59, origin_size=20)
    bot.tracker.books[market.no_token].snapshot([], [{"price": "0.86", "size": "100"}])

    await bot._advance_reward_exit_batches(100.0)

    assert bot.broker.taker_orders == []
    assert bot.metrics.get_reward_exit_batch("batch-1")["status"] == "TAKE_BLOCKED"
    assert market.condition_id in bot._reward_exit_locked
```

- [ ] **Step 2: 运行失败测试**

Run: `rtk pytest tests/test_reward_exit_lifecycle.py -k take_cost_guard_blocks -q`

Expected: FAIL，因为当前实现会提交双倍 take。

- [ ] **Step 3: 写入失败生命周期测试：价格回落后 blocked batch 可重新提交 take**

```python
async def scenario():
    bot, market = _bot(tmp_path)
    _blocked_take_batch(bot, market, origin_price=0.59, origin_size=20)
    bot.tracker.books[market.no_token].snapshot([], [{"price": "0.41", "size": "100"}])

    await bot._advance_reward_exit_batches(101.0)

    assert bot.metrics.get_reward_exit_batch("batch-1")["status"] == "TAKE_PENDING"
    assert len(bot.broker.taker_orders) == 1
```

- [ ] **Step 4: 运行失败测试**

Run: `rtk pytest tests/test_reward_exit_lifecycle.py -k take_cost_guard_retries -q`

Expected: FAIL，因为当前实现不会处理 `TAKE_BLOCKED`。

- [ ] **Step 5: 实现门控与重试**

在 `_advance_reward_exit_batches` 中把 `TAKE_BLOCKED` 与 `TAKE_PENDING` 都分派给 `_advance_take_pending`。在提交任何 take 前，从 order book 取得互补 `best_ask`，调用 `max_take_price_for_pair`；若 ask 缺失或超过 cap，则 `update_reward_exit_batch(status="TAKE_BLOCKED", manual_reason="take_cost_guard")` 并返回。价格合规时，将 blocked batch 改回 `TAKE_PENDING` 后再走现有下单分支。将 `risk.reward_exit_max_pair_loss_cents: 8.0` 写入 `config.yaml`。

- [ ] **Step 6: 写入失败生命周期测试：seal 使用真实 fill 而非提交限价**

```python
async def scenario():
    bot, market = _bot(tmp_path)
    _open_take_batch(bot, market, origin_price=0.59, origin_size=20)
    bot.broker.fills_log = [
        {"fill_id": "take-1", "batch_id": "batch-1", "intent": "batch_take",
         "price": 0.86, "size": 40.0, "fee": 0.24, "ts": 100.0},
    ]

    await bot._credit_take_fills(101.0)
    batch = bot.metrics.get_reward_exit_batch("batch-1")

    assert batch["take_notional_usd"] == pytest.approx(34.4)
    assert batch["take_fee_usd"] == pytest.approx(0.24)
```

- [ ] **Step 7: 运行失败测试**

Run: `rtk pytest tests/test_reward_exit_lifecycle.py -k credits_actual_take_fill -q`

Expected: FAIL，因为当前记录可能把提交价或订单意图计入 take 成本。

- [ ] **Step 8: 只用持久真实 fill 重建 take 并 seal**

在 `_credit_take_fills` 仅以 broker/userfeed 中带 `fill_id` 的 `batch_take` 记录写入 `reward_exit_fills`；用 `list_reward_exit_fills(batch_id, intent="batch_take")` 的价格、数量、费用求和更新 batch。达到 `2q` 后使用现有 FIFO split 与 `transition_to_seal_pending` 字段，写入 `paired_size=q`、`exit_initial_size=q`、`paired_loss_usd` 和 `status="SELL_PENDING"`；禁止用下单限价写入这些字段。

- [ ] **Step 9: 运行生命周期测试**

Run: `rtk pytest tests/test_reward_exit_lifecycle.py -q`

Expected: PASS。

### Task 3: 跨重启锁与回归验证

**Files:**
- Modify: `pmbot/main.py:1998-2028`
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes open batch statuses `TAKE_PENDING`, `TAKE_BLOCKED`, `SELL_PENDING`。
- Produces启动时只关闭真正陈旧且仍可安全放弃的 `TAKE_PENDING`；blocked 与 sell 状态保持 lock。

- [ ] **Step 1: 写入失败测试：陈旧 TAKE_BLOCKED 重启后不能关闭或解锁**

```python
def test_startup_keeps_stale_take_blocked_reward_exit_batch_locked(tmp_path):
    async def scenario():
        bot = _bot(tmp_path)
        batch = {"batch_id": "blocked", "cid": "cid", "status": "TAKE_BLOCKED", "created_ts": 0.0}
        metrics = MagicMock()
        metrics.get_open_reward_exit_batches.return_value = [batch]
        bot.metrics, bot.broker, bot.tracker = metrics, MagicMock(), MagicMock()
        bot._credit_take_fills = AsyncMock()
        bot._process_reward_fills = AsyncMock()
        bot._advance_reward_exit_batches = AsyncMock()

        await bot._run_reward_exit_batch_tick(901.0)

        metrics.close_reward_exit_batch.assert_not_called()
        assert "cid" in bot._reward_exit_locked
    asyncio.run(scenario())
```

- [ ] **Step 2: 运行失败测试**

Run: `rtk pytest tests/test_main.py -k stale_take_blocked -q`

Expected: FAIL，因为当前启动清理会把它当作未知的可关闭 batch，或未建立 lock。

- [ ] **Step 3: 收窄启动清理与同步条件**

保持清理谓词为严格的 `status == "TAKE_PENDING"`；明确 `TAKE_BLOCKED` 是保留 CID lock 的开放状态。不要修改任何 generic P1 recovery 分支。

- [ ] **Step 4: 运行通过测试与既有回归**

Run: `rtk pytest tests/test_main.py -k "stale_take_blocked or stale_sell_pending" -q`

Expected: PASS。

- [ ] **Step 5: 运行完整相关验证**

Run: `rtk pytest tests/test_reward_exit.py tests/test_reward_exit_lifecycle.py tests/test_main.py -q`

Expected: 全部 reward-exit 相关测试通过；若存在与本次无关的既有失败，逐项报告且不归因为本改动。

- [ ] **Step 6: 检查改动范围**

Run: `rtk git diff --check; rtk git diff -- pmbot/reward_exit.py pmbot/main.py config.yaml tests/test_reward_exit.py tests/test_reward_exit_lifecycle.py tests/test_main.py`

Expected: 仅包含上述成本门控、状态机、配置和回归测试改动；不提交 Git。
