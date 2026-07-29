# 带敞口市场名额阈值 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 依据每个市场的 `rewardsMinSize` 决定未配对敞口市场是否占用 `top_n_markets` 名额。

**Architecture:** `Bot._locked_inventory_markets()` 保持带可管理未配对敞口市场的识别职责，并在选择阶段按 `Market.min_size` 将其划分为占位锁定与免费保留市场。`_select_markets()` 将占位锁定从普通容量扣除，免费保留附加到普通候选结果，且按 condition id 去重。

**Tech Stack:** Python 3, pytest, pmbot 的 Gamma 市场模型与 paper broker。

## Global Constraints

- `Market.min_size` 是 Gamma `rewardsMinSize` 的本地字段，单位为股。
- 可管理未配对敞口仍要求 `abs(unpaired_shares) >= MIN_TAKER_SHARES`（当前为 5）。
- `abs(unpaired_shares) < market.min_size` 不占名额；相等或更大时占一个名额。
- 不启动 live 交易进程。

---

### Task 1: 锁定市场容量的回归测试

**Files:**
- Modify: `tests/test_main.py:304-320`

**Interfaces:**
- Consumes: `Bot._rescan(initial=False)`, `Broker.unpaired_shares(Market)`, `Market.min_size`。
- Produces: 两个端到端扫描断言，定义免费保留与占位锁定的预期市场集合。

- [x] **Step 1: 写入失败测试**

在 `test_rescan_keeps_unpaired_inventory_market_until_flat` 中将 `held.min_size` 设为 `10`，并在首次重扫后设置 `Position(no_shares=5.0)`。第二次重扫应断言结果为 `{held, flat, fresh}`。接着将同一仓位改为 `Position(no_shares=10.0)`，第三次重扫应断言结果为 `{held, flat}`；最后归零后应断言 `{flat, fresh}`。

```python
held.min_size = 10.0
bot.broker.state.positions[held.condition_id] = Position(no_shares=5.0)
await bot._rescan()
assert {m.condition_id for m in bot.markets} == {"held", "flat", "fresh"}

bot.broker.state.positions[held.condition_id] = Position(no_shares=10.0)
await bot._rescan()
assert {m.condition_id for m in bot.markets} == {"held", "flat"}
```

- [x] **Step 2: 运行定向测试并确认失败**

Run: `pytest tests/test_main.py::test_rescan_keeps_unpaired_inventory_market_until_flat -q`

Expected: FAIL；当前实现始终将带仓市场视为占位锁定，因此 5 股敞口时只返回 `{held, flat}`。

- [x] **Step 3: 实现最小选择逻辑**

在 `pmbot/main.py` 将锁定市场按 `abs(self.broker.unpaired_shares(m)) >= m.min_size` 划分：达到奖励最小挂单量的市场仍作为 `locked` 扣减容量；低于该量的带仓市场作为 `free_locked`，不扣减容量但在普通候选选择后附加。对两类 market 以 `condition_id` 去重并保持免费带仓市场不进入普通候选名额。

```python
occupying = [m for m in locked if abs(self.broker.unpaired_shares(m)) >= m.min_size]
free_locked = [m for m in locked if m.condition_id not in {x.condition_id for x in occupying}]
slots = top_n - len(occupying)
# ...现有普通候选选择使用 slots...
return occupying + free_locked + chosen
```

- [x] **Step 4: 运行定向测试并确认通过**

Run: `pytest tests/test_main.py::test_rescan_keeps_unpaired_inventory_market_until_flat -q`

Expected: PASS。

- [x] **Step 5: 运行相关测试**

Run: `pytest tests/test_main.py -q`

Expected: PASS。实际结果：`tests/test_main.py` 的 29 项通过；全量 `tests/` 中另有
`tests/test_gamma.py::test_fetch_reward_markets_requests_reward_bearing_books` 的既有断言失败，
该测试期望已经不由 `fetch_reward_markets()` 发送的 `rewards_min_size` 参数，和本任务无关。

- [ ] **Step 6: 提交**

不执行提交：工作区已有用户未提交的日志处理改动，避免混入本任务。完成后由用户决定如何分组提交。
