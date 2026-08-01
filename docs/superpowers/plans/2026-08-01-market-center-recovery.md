# 超时中枢补单 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 使超时单边库存按当前盘口中枢的策略价提交等量反向被动补单，即使市场位于普通做市价格区间外。

**Architecture:** 在 `strategy.py` 增加只负责恢复中枢定价的函数，复用正常报价的 microprice、偏移、库存倾斜和 tick 对齐计算，但不应用普通 `mid_range` 过滤。`Bot` 在超时恢复阶段调用该函数，且始终只保留互补方向、未配平等量的订单；强平吃单逻辑不改。

**Tech Stack:** Python 3、现有 `pmbot` 策略/运行器、pytest。

## Global Constraints

- 恢复单价格可高于成本保本价，但只限被动恢复单。
- 恢复单方向必须是当前单边库存的互补方向，数量必须等于当前未配平数量。
- 普通 `scanner.mid_range` 不得阻止恢复中枢定价；缺 bid/ask、过期盘口和低于最小交易数量时不得下单。
- 主动强平继续要求成本加 taker fee 不超过 $1。
- 每个恢复决策记录中枢、偏移、策略价、成本和中文原因。

---

### Task 1: 恢复中枢报价函数

**Files:**
- Modify: `pmbot/strategy.py`
- Test: `tests/test_strategy.py`

**Interfaces:**
- Produces: `compute_recovery_quote(market, yes_book, net_yes_exposure_usd, cfg, max_inventory_usd, complement_token, size, fade_yes=0.0, fade_no=0.0, flow_imbalance=0.0, markout_avg=None) -> tuple[Quote | None, dict[str, float | str]]`。
- Consumes: `microprice`, `adaptive_offset`, `_round_tick`。

- [ ] **Step 1: 写失败测试**

```python
def test_recovery_quote_uses_center_outside_normal_mid_range():
    quote, audit = strategy.compute_recovery_quote(
        market, yes_book_at_0_11, 2.5, cfg, 30,
        market.no_token, 20,
    )
    assert quote is not None
    assert quote.token_id == market.no_token
    assert quote.size == 20
    assert audit["fair"] > 0
```

- [ ] **Step 2: 运行失败测试**

Run: `python -m pytest tests/test_strategy.py::test_recovery_quote_uses_center_outside_normal_mid_range -q`

Expected: FAIL，因为函数不存在。

- [ ] **Step 3: 实现最小中枢定价函数**

```python
fair = microprice(yes_book) or yes_book.mid
offset = max(band * cfg["quoting"]["offset_frac_of_max_spread"], market.tick)
yes_price = _round_tick(fair - offset - skew - fade_yes, market.tick)
no_price = _round_tick((1.0 - fair) - offset + skew - fade_no, market.tick)
return Quote(complement_token, selected_price, float(size)), audit
```

函数必须仅要求有效 YES bid/ask，不检查 `scanner.mid_range`，并返回审计字段。

- [ ] **Step 4: 运行策略测试**

Run: `python -m pytest tests/test_strategy.py -q`

Expected: PASS。

### Task 2: 超时恢复路径接入和审计日志

**Files:**
- Modify: `pmbot/main.py`
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: `strategy.compute_recovery_quote(...)`。
- Produces: `_escalated_recovery_quotes(...)` 返回单一互补方向、等于 `abs(unpaired)` 的报价，并标记 `market_center_recovery`。

- [ ] **Step 1: 写失败测试**

```python
def test_escalated_recovery_uses_center_quote_outside_mid_range(bot, market):
    bot.broker.set_unpaired(market, 20)
    quotes = bot._held_market_recovery_quotes(market, [], 20, now=after_timeout)
    assert [(q.token_id, q.size) for q in quotes] == [(market.no_token, 20)]

def test_escalated_recovery_never_returns_same_side_quote(bot, market):
    quotes = bot._held_market_recovery_quotes(market, normal_desired, -20, now=after_timeout)
    assert all(q.token_id == market.yes_token for q in quotes)
```

- [ ] **Step 2: 运行失败测试**

Run: `python -m pytest tests/test_main.py -k escalated_recovery -q`

Expected: FAIL，因为旧路径从普通 `desired` 列表筛选，区间外时会返回空列表。

- [ ] **Step 3: 接入恢复专用函数并更新日志**

```python
quote, pricing = strategy.compute_recovery_quote(...)
if quote is None:
    log.warning("INVENTORY_RECOVERY_SKIPPED ... reason=%s 说明=...", reason)
else:
    log.warning("INVENTORY_RECOVERY_QUOTE ... path=market_center_recovery fair=... strategy_price=... 说明=...")
```

恢复路径必须不再使用 `escalated_recovery`；被动补单的 `expected_pair_pnl` 继续写入指标，但不得因其为负而取消报价。强平函数保持不变。

- [ ] **Step 4: 运行运行器测试**

Run: `python -m pytest tests/test_main.py -q`

Expected: PASS。

### Task 3: 文档与回归验证

**Files:**
- Modify: `README.md`
- Modify: `docs/optimization-roadmap.md`
- Test: `tests/test_main.py`, `tests/test_strategy.py`, `tests/test_metrics.py`, `tests/test_risk.py`

- [ ] **Step 1: 更新用户文档**

说明超时恢复使用盘口中枢策略价、仅补互补方向等量仓位、强平仍受成本加手续费限制，并给出日志事件的查询方式。

- [ ] **Step 2: 执行针对性回归**

Run: `python -m pytest tests/test_main.py tests/test_strategy.py tests/test_metrics.py tests/test_risk.py -q`

Expected: PASS。

- [ ] **Step 3: 检查变更范围**

Run: `git diff --check` and `git diff -- pmbot/main.py pmbot/strategy.py tests/test_main.py tests/test_strategy.py README.md docs/optimization-roadmap.md`

Expected: 无空白错误，且变更仅覆盖已确认的恢复路径、日志、文档和测试。
