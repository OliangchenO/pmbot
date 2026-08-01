# P1 补单与强平审计日志 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为每个补单和强平决策写入足以判定“为何执行、为何未执行、最终结果如何”的中文审计记录。

**Architecture:** 保留现有异步 Python logging 和 `recovery_events` SQLite 账本。补单路径产生统一的经济快照；强平路径把风险触发、订单簿、成本、手续费、上限和拒绝理由记录为结构化事件与中文日志。

**Tech Stack:** Python 3、SQLite、pytest、现有 QueueHandler/QueueListener。

## Global Constraints

- 只写现有 `logs/pmbot.log` 异步日志，不新建运行时日志文件。
- 日志时间沿用北京时间格式；日志失败不得影响下单流程。
- 不改变 normal quote、补单、强平的价格或下单条件；此次仅增加可观测性。
- paper/live 共用字段，但账本仍使用各自配置的数据库。

---

### Task 1: 补单经济快照与结构化事件

**Files:**
- Modify: `pmbot/metrics.py`
- Modify: `pmbot/main.py`
- Test: `tests/test_metrics.py`
- Test: `tests/test_main.py`

**Interfaces:**
- Produces: `MetricsStore.record_recovery_event(..., basis, fee, expected_pair_pnl, hard_cap, proposed_price)`。
- Produces: 补单日志包含裸仓方向/数量、成本、break-even、候选价、实际价、手续费及预期单对 PnL。

- [x] **Step 1: Write failing tests**
  - 验证 recovery 事件保存全部经济字段。
  - 验证补单日志记录候选价被硬上限裁剪的依据。
- [x] **Step 2: Run focused tests and verify expected failures**
- [x] **Step 3: Implement additive schema migration, event API and structured Chinese logs**
- [x] **Step 4: Re-run focused tests and verify pass**

### Task 2: 强平决策、拒绝和执行结果日志

**Files:**
- Modify: `pmbot/main.py`
- Modify: `pmbot/metrics.py`
- Test: `tests/test_main.py`
- Test: `tests/test_metrics.py`

**Interfaces:**
- Produces: `forced_hedge_deferred` / `forced_hedge_submitted` / `forced_hedge_filled` recovery events with a reason and economic snapshot.
- Produces: 强平日志覆盖风险触发条件、簿价/价差、成本/费用/上限、拒绝或执行结果。

- [x] **Step 1: Write failing tests**
  - 验证超过 pair cap 的强平拒绝会记录价格、cap、费用和 `over_hard_cap`。
  - 验证符合条件的强平会记录提交和成交数量。
- [x] **Step 2: Run focused tests and verify expected failures**
- [x] **Step 3: Implement additive audit events and Chinese decision logs**
- [x] **Step 4: Re-run focused tests and verify pass**

### Task 3: 验收与路线图

**Files:**
- Modify: `docs/optimization-roadmap.md`
- Test: `tests/test_main.py`
- Test: `tests/test_metrics.py`

- [x] **Step 1: Run focused regression tests**
- [x] **Step 2: Run full test suite and separately report any inherited failure**
- [x] **Step 3: Update P1 progress only for implemented/verified logging evidence**
