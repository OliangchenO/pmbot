# Reward Exit Batch Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 修复奖励成交双倍 take/批次 SELL 的成交归因、幂等、数量控制、重启恢复和模式门控问题，使每个批次只消费属于自己的已确认成交事实。

**Architecture:** 在现有 `reward_exit_batches` 汇总表旁新增批次成交事实和批次订单事实表；broker 通过 `audit_context` 写入 `batch_id`/`intent`，Bot 只从持久化事实表推进状态。take 使用数量受限的 FAK，SELL 使用可恢复的批次订单记录；shadow 只观测不锁定、不执行。

**Tech Stack:** Python 3、asyncio、SQLite、pytest、现有 PaperBroker/LiveBroker。

## Global Constraints

- 普通奖励成交创建独立批次；take 目标固定为 `2q`，不使用经济价格上限、pair-cap 或亏损预算。
- take 和 SELL 的每条成交以交易所 `fill_id`（paper 使用稳定模拟 ID）幂等落库，并绑定 `batch_id`、`order_id`、`intent`。
- 只有 `intent=normal_reward` 的 maker BUY fill 可以创建批次；`batch_take`、`batch_exit`、forced hedge 和来源不明 fill 不得创建批次。
- take 未达到 `2q` 不得计算配对亏损或挂 SELL；部分成交只提交剩余数量。
- 批次期间阻断普通报价、旧 recovery、Phase 2 和 forced hedge；所有批次关闭后只解除锁定，必须下一次新的 Top-N 扫描重新选中才恢复普通奖励报价。
- `shadow` 不调用真实撤单、take 或 SELL，也不改变普通报价/recovery 行为；`active` 才执行批次订单和市场锁定。
- 不启动、重启或修改 live 进程；只做本地代码和测试验证。

---

### Task 1: 持久化批次成交与订单事实

**Files:**
- Modify: `pmbot/metrics.py`（SQLite schema 与 MetricsStore 生命周期 API）
- Test: `tests/test_metrics.py` 或新增 `tests/test_reward_exit_lifecycle.py`

**Interfaces:**
- `record_reward_exit_fill(*, fill_id, batch_id, order_id, intent, cid, token_id, side, price, size, fee_usd, ts) -> bool`
- `list_reward_exit_fills(batch_id, intent=None) -> list[dict]`
- `record_reward_exit_order(*, order_id, batch_id, intent, cid, token_id, side, price, size, expiration, status) -> bool`
- `get_reward_exit_order(batch_id) -> dict | None`
- `update_reward_exit_order(order_id, **fields) -> None`

- [ ] **Step 1: Write failing tests** for unique fill insertion, batch filtering, order persistence, and idempotent duplicate insertion.
- [ ] **Step 2: Run focused tests** and confirm they fail because the tables/API do not exist.
- [ ] **Step 3: Add SQLite tables and minimal CRUD** with unique `fill_id`, unique `order_id`, and transactional updates.
- [ ] **Step 4: Run focused tests** and confirm all lifecycle persistence tests pass.

### Task 2: Broker 归因、精确数量与 paper/live fill IDs

**Files:**
- Modify: `pmbot/brokers.py`
- Modify: `pmbot/userfeed.py` only if fee/fill identifiers need propagation
- Test: `tests/test_brokers.py`, `tests/test_userfeed.py`

**Interfaces:**
- `PaperBroker.taker_buy(..., audit_context: dict | None = None) -> float`
- `LiveBroker.taker_buy(..., audit_context: dict | None = None) -> float`
- `place_reward_exit(market, batch_id, quote, audit_context) -> RestingOrder | PaperOrder | None`
- `cancel_reward_exit(batch_id) -> bool`
- fill entries carry `fill_id`, `order_id`, `batch_id`, `intent`, `fee` when known.

- [ ] **Step 1: Write failing tests** proving batch take carries `intent=batch_take` and `batch_id`, forced hedge remains `forced_hedge`, paper fills have stable IDs, and a cheap ask cannot buy more than requested shares.
- [ ] **Step 2: Run the broker tests** and confirm the current implementation labels batch take as forced hedge, lacks IDs, or overfills.
- [ ] **Step 3: Implement broker context propagation**; do not register reward-exit takes in the single forced-hedge overlay; use an exact-size FAK representation supported by the installed CLOB client. Add per-batch PaperBroker exit orders instead of reusing the singleton recovery exit.
- [ ] **Step 4: Persist/forward actual fee and exchange fill identifiers** without changing existing normal/recovery semantics.
- [ ] **Step 5: Run focused broker/userfeed tests** and confirm pass.

### Task 3: Batch controller idempotency, FIFO, SELL recovery

**Files:**
- Modify: `pmbot/main.py`
- Modify: `pmbot/metrics.py` as required by Task 1
- Test: `tests/test_main.py`, `tests/test_reward_exit_lifecycle.py`

**Interfaces:**
- `_credit_take_fills()` consumes only persisted `intent=batch_take` fills bound to the batch and marks each fill applied once.
- `_seal_and_set_sell_target()` loads the complete persisted take-fill history for the batch before FIFO splitting.
- `_advance_sell_pending()` consumes only persisted `intent=batch_exit` fills for the batch and updates the persisted order before posting replacements.

- [ ] **Step 1: Write failing integration tests** for repeated ticks, unrelated forced hedge, partial take across ticks, partial SELL across ticks, multiple batches on one CID, and restart with an existing SELL order.
- [ ] **Step 2: Run tests and verify each reproduces the review failure.**
- [ ] **Step 3: Replace in-memory fill scans with transactional persisted fill ingestion and applied markers.**
- [ ] **Step 4: Bind every take/SELL submission to a batch order record; on startup/reconcile rehydrate the order map from SQLite and exchange open orders before posting.**
- [ ] **Step 5: Make sealing use all confirmed take fills and make SELL updates idempotent; keep cancel-before-post for reduced remaining size.**
- [ ] **Step 6: Run focused main/lifecycle tests and confirm pass.

### Task 4: Mode and Top-N gates

**Files:**
- Modify: `pmbot/main.py`
- Test: `tests/test_main.py`, `tests/test_reward_exit_lifecycle.py`

**Interfaces:**
- `shadow` observes fills and computes decisions but does not add `_reward_exit_locked`, cancel quotes, execute take/SELL, or suppress recovery.
- `active` locks CID while any batch is open; after close it records a re-entry gate that only a subsequent successful scan can clear.

- [ ] **Step 1: Write failing tests** for shadow non-interference and closed-batch no-requote-before-new-Top-N-scan.
- [ ] **Step 2: Run tests and confirm current shadow/instant-unlock behavior fails.**
- [ ] **Step 3: Implement mode-aware locking and a persisted/in-memory `awaiting_top_n_rescan` gate cleared only by scan selection.**
- [ ] **Step 4: Run focused tests and the existing main suite.**

### Task 5: Full verification

- [ ] Run `tests/test_reward_exit.py`, lifecycle tests, `tests/test_main.py`, `tests/test_brokers.py`, `tests/test_metrics.py`.
- [ ] Run `python -m compileall -q pmbot`.
- [ ] Run `git diff --check`.
- [ ] Inspect `git diff` for unrelated changes; do not start any live process.
