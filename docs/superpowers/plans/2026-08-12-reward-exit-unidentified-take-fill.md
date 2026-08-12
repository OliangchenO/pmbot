# Reward-exit 无订单 ID take 成交归因 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让无订单 ID 的真实 batch take 成交可靠写入唯一批次，防止重复 FAK 买入。

**Architecture:** `LiveBroker` 保存每次已提交 batch take 的唯一待归因上下文。用户成交流若无订单 ID 且匹配该上下文，附上 `batch_take`/`batch_id` 并以 exchange `fill_id` 持久化；编排器只凭此持久化进度决定下一次提交。

**Tech Stack:** Python 3.12、pytest、SQLite MetricsStore。

## Global Constraints

- 不回补历史成交，不控制运行中的机器人或订单。
- 普通奖励 fill 仍独立创建 `2q` take 目标。
- 只以真实 `fill_id` 写入 take 成交；不使用盘口或限价估算。

---

### Task 1: 无订单 ID batch take 的真实成交归因

**Files:**
- Modify: `pmbot/brokers.py`
- Test: `tests/test_brokers.py`

**Interfaces:**
- Produces: WebSocket fill entry with `intent="batch_take"` and nonempty `batch_id` when它匹配唯一已提交 take。

- [ ] **Step 1: Write the failing test**

构造唯一 batch-take 待归因上下文并提交无 order ID 的 taker BUY fill；断言 SQLite 的 `reward_exit_fills` 有该真实 fill ID、batch ID、价格、数量、手续费。

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\\Scripts\\python.exe -m pytest tests/test_brokers.py -k unidentified_batch_take -q`

Expected: FAIL，当前 fill 被归为 forced hedge 或未写入 batch take 事实。

- [ ] **Step 3: Write minimal implementation**

在 `LiveBroker` 保存 batch take 提交上下文。处理无 order ID taker fill 时仅在 CID、token、side、时间和唯一 batch ID 匹配时采用该上下文；把 `batch_take` 写入 MetricsStore。

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\\Scripts\\python.exe -m pytest tests/test_brokers.py -k unidentified_batch_take -q`

Expected: PASS。

### Task 2: 未归因的已提交 take 禁止再次提交

**Files:**
- Modify: `pmbot/main.py`
- Test: `tests/test_reward_exit_lifecycle.py`

**Interfaces:**
- Consumes: 对应批次的 persisted `batch_take` fill。
- Produces: 有待归因成交时不调用第二次 `taker_buy`；真实 fill 进入后按持久化数量继续或封存。

- [ ] **Step 1: Write the failing test**

第一次 FAK 回执返回正数但批次尚无持久化 fill，立即运行下一 tick；断言 `taker_buy` 仍仅调用一次。随后写入真实 batch take fill 并断言批次进度更新。

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\\Scripts\\python.exe -m pytest tests/test_reward_exit_lifecycle.py -k awaiting_fill -q`

Expected: FAIL，当前 30 秒以内之外会再次提交，且持久化 fill 不清除提交等待。

- [ ] **Step 3: Write minimal implementation**

将等待状态建立在 batch 的已提交、未归因 context 上；`_credit_take_fills` 读取真实 fill 后清除等待并更新进度。保留已有成本门控与阻塞重试。

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\\Scripts\\python.exe -m pytest tests/test_reward_exit_lifecycle.py -k awaiting_fill -q`

Expected: PASS。

### Task 3: 定向回归

- [ ] **Step 1: Run suites**

Run: `.venv\\Scripts\\python.exe -m pytest tests/test_brokers.py tests/test_reward_exit.py tests/test_reward_exit_lifecycle.py -q`

- [ ] **Step 2: Compile and inspect whitespace**

Run: `.venv\\Scripts\\python.exe -m compileall -q pmbot` and `git diff --check`
