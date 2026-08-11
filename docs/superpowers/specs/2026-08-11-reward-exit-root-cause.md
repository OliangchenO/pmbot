# Reward Exit Batch 根因分析

> 调查时间：2026-08-11
> 分析范围：2026-08-10 全日志 + metrics.db + 完整代码审查

## 数据库状态

两个 batch 均为 `CLOSED`，但：
- `updated_ts == created_ts` → `open_reward_exit_batch()` 是唯一执行过的写操作
- `closed_ts = None` → `close_reward_exit_batch()` 从未执行
- `take_filled_size = 0.0` → `update_reward_exit_batch()` 从未执行

**结论：没有任何代码路径能产生此状态。`open_reward_exit_batch()` 创建时 status=TAKE_PENDING。要从 TAKE_PENDING 变为 CLOSED 必须经过 `update_reward_exit_batch(status="SELL_PENDING")` 或 `close_reward_exit_batch()`，两者都会修改 `updated_ts`。此状态唯一可能的来源是手动 SQL UPDATE。**

## 实际发现的问题

### Bug 1: FAK 过量成交（Overfill）

**根因**：`LiveBroker.taker_buy()` 使用 `amount = max_price * size` 作为 FAK 订单金额。CLOB 将此金额按 best_ask 买入最多份额，导致买入量远超请求量。

**证据**：
- batch1 take_target=7.32 股，每次 EXECUTED 显示 filled=14.80
- batch2 take_target=87.8 股，EXECUTED 显示 filled=170.49, 175.12

**代码位置**：`brokers.py:1407`
```python
amount = round(effective_price * size, 2)
```
这里 `effective_price` 已用 best_ask 做了保守估算（line 1405），但 CLOB 执行时实际 ask 可能更低，导致更多成交。这是 FAK market order 的固有行为，无法完全避免。

**修复方向**：
- 在 `taker_buy()` 调用前，使用 `safe_price * remaining` 做更精确的 amount 计算（已实现）
- FAK 返回的 `filled` 应被正确 cap 到 `size`（已实现：line 1420 `min(_parse_fill_amount(resp, size), size)`）
- `_advance_take_pending()` 中 `new_total = filled + filled_now` 会正确触发 `_seal_and_set_sell_target`，但 DB 未反映此更新

### Bug 2: 无批次超时机制

**根因**：如果 TAKE_PENDING 批次因为余额不足、市场摘牌、book 丢失等原因永远无法完成 take，市场锁永不释放，CID 永久退出报价。

**证据**：
- 两个批次在 Aug 10 22:19 / 23:12 创建
- 至今超过 19 小时仍锁住市场
- 日志中从未出现 BATCH_SEALED、REWARD_EXIT_BATCH_CLOSED、MARKET_REWARD_EXIT_UNLOCKED

**修复方向**：
- 为 TAKE_PENDING 批次添加 `terminal_after_secs` 超时
- 超时后自动：写 MANUAL_HOLD 理由 → 清理锁 → UNLOCK CID
- 配置 key: `risk.reward_exit_terminal_after_secs`

### Bug 3: 重启后不再处理已有批次

**根因**：批次写入了 DB，但 `_advance_reward_exit_batches()` 只在 `_process_reward_fills()`（检测新成交）和批次推进中调用。如果批次从未完成 take（`take_filled_size == 0`），重启后 `_advance_take_pending()` 会重新尝试 take，**但前提是批次的 status 不为 CLOSED**。当前两个批次被手动设为 CLOSED 后被永久忽略。

**修复方向**：
- 启动时检测僵尸 TAKE_PENDING 批次（created_ts 早于重启时间且 still pending）
- 自动 close 僵尸批次并 unlock CID

### Bug 4: `_advance_take_pending` 中 new_total 变量作用域问题

**代码位置**：`main.py:2437,2445,2456`

```python
if filled_now > 0 and self.metrics is not None:
    new_total = filled + filled_now  # line 2437
    remaining_after = ...  # line 2445

if filled_now > 0:
    log.warning("... remaining_after=%.0f ..."  # line 2450 USES remaining_after!
                ..., remaining_after)
    if new_total >= target - 1e-9 and ...:  # line 2456 USES new_total!
```

`new_total` 和 `remaining_after` 在 `if filled_now > 0 and self.metrics is not None:` 块内定义，但在外层 `if filled_now > 0:` 块中使用。如果 `self.metrics` 因某种原因为 None（例如 metrics.close() 已在 shutdown 中调用），`filled_now > 0` 时这两变量未定义 → **NameError 崩溃**。

但实际上这种情况不会发生因为 `_broker_call` 返回前 metrics 仍在…但这是一个代码正确性 bug。

## 修复方案

### P0: 修复变量作用域 → 消除潜在崩溃
```python
new_total = filled  # init before the if-block
if filled_now > 0 and self.metrics is not None:
    new_total = filled + filled_now
    ...
```

### P0: 添加批次超时自动关闭
在 `_advance_reward_exit_batches()` 中：
- 检测 TAKE_PENDING 批次是否超过 `terminal_after_secs`
- 超时则调用 `close_reward_exit_batch(status="CLOSED", manual_reason="terminal_timeout")`
- 日志：BATCH_TERMINAL_TIMEOUT

### P1: 启动时清理僵尸批次
在 `Bot.__init__` 或首轮 `_run_reward_exit_batch_tick()` 中：
- 检测所有 status != 'CLOSED' 的批次
- 对其 CID 调用 `close_reward_exit_batch(status="CLOSED", manual_reason="stale_on_startup")`
- 触发 CID unlock

### P1: 恢复两个当前 CLOSED 批次 → 清理锁
手动：将两个批次的 CID 从 `data/banned_markets.json` 中移除（如果有），清理 `_reward_exit_locked`

## 验证

修复后手动测试：
1. 在 shadow 模式重新运行，验证 `_advance_reward_exit_batches()` 能正常推进批次
2. 检查 terminal timeout 是否正确触发
3. 检查启动清理是否正确关闭僵尸批次
