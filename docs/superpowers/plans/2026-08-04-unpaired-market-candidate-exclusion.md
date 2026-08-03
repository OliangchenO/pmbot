# 未配平市场候选排除 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让达到未配平阈值的持仓市场退出普通候选竞争，但持续由库存路径补单。

**Architecture:** 重扫时从 `broker.held_markets()` 计算恢复 CID 集合，并在传入 `_select_markets()` 前过滤排名结果。市场离开 `self.markets` 后仍由 `_quote_all()` 的 held-market 合并逻辑报价；重扫的撤单/订阅清理须把恢复 CID 当作受管理库存保留。

**Tech Stack:** Python 3、pytest、现有 `Bot`/`PaperBroker`/`LiveBroker`。

## Global Constraints

- 阈值固定为 `MIN_TAKER_SHARES`，判断为 `abs(unpaired_shares) >= MIN_TAKER_SHARES`。
- 不等同于 `manual_hold_cids`：恢复报价、强制对冲、合并、退出和持仓订单簿订阅保持启用。
- 不修改正在运行的进程、生产配置或交易所订单。

---

### Task 1: 将未配平 CID 排除出候选选择

**Files:**
- Modify: `pmbot/main.py:971-1085`
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: `broker.held_markets() -> list[Market]`、`broker.unpaired_shares(market) -> float`。
- Produces: `_rescan()` 传给 `_select_markets()` 的 `ranked` 不含恢复 CID；`self.markets` 只含普通候选。

- [ ] **Step 1: Write the failing tests**

```python
def test_rescan_excludes_unpaired_held_market_from_selected_slots(...):
    broker.set_position(m_held, yes=12, no=0)
    bot.markets = [m_held]
    await bot._rescan()
    assert m_held.condition_id not in {m.condition_id for m in bot.markets}
    assert m_flat.condition_id in {m.condition_id for m in bot.markets}

def test_rescan_allows_flattened_held_market_back_into_selection(...):
    broker.set_position(m_held, yes=12, no=12)
    await bot._rescan()
    assert m_held.condition_id in {m.condition_id for m in bot.markets}
```

- [ ] **Step 2: Run the focused tests and verify failure**

Run: `.venv\\Scripts\\python.exe -m pytest tests\\test_main.py -q -k "rescan and unpaired"`

Expected: the unpaired held market still occupies a selected slot.

- [ ] **Step 3: Implement minimal selection filtering**

```python
recovery_cids = {
    m.condition_id for m in self.broker.held_markets()
    if abs(self.broker.unpaired_shares(m)) >= MIN_TAKER_SHARES
}
ranked = [m for m in ranked if m.condition_id not in recovery_cids]
markets = self._select_markets(ranked)
```

Apply the same CID set to the old-market removal path so it does not cancel a recovery order or discard its subscribed books merely because the market left `self.markets`.

- [ ] **Step 4: Run the focused tests and verify pass**

Run: `.venv\\Scripts\\python.exe -m pytest tests\\test_main.py -q -k "rescan and unpaired"`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pmbot/main.py tests/test_main.py
git commit -m "fix: exclude unpaired markets from selection"
```

### Task 2: 验证离开候选集后仍持续补单

**Files:**
- Modify: `pmbot/main.py:1047-1090` (only if Task 1 tests expose a missing retention branch)
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: `_quote_all()` 中 `managed = self.markets + broker.held_markets()`。
- Produces: 未配平市场不在 `self.markets` 时仍输出一个等敞口的互补 `Quote`。

- [ ] **Step 1: Write the failing regression test**

```python
async def test_unselected_unpaired_market_keeps_complement_recovery_quote(...):
    bot.markets = [m_flat]
    broker.set_position(m_held, yes=12, no=0)
    await bot._quote_all()
    assert broker.open_quotes(m_held) == [Quote(m_held.no_token, pytest.approx(...), 12)]
```

- [ ] **Step 2: Run and verify failure**

Run: `.venv\\Scripts\\python.exe -m pytest tests\\test_main.py -q -k "unselected_unpaired"`

Expected: FAIL only if rescan cleanup removed recovery management; otherwise record immediate PASS as the existing management invariant.

- [ ] **Step 3: Make the smallest retention correction if needed**

```python
if old_m.condition_id in recovery_cids:
    continue  # preserve its recovery order and book; _ensure_held_market_books owns it
```

- [ ] **Step 4: Run focused regression tests**

Run: `.venv\\Scripts\\python.exe -m pytest tests\\test_main.py -q -k "unpaired or recovery"`

Expected: PASS.

- [ ] **Step 5: Commit any Task 2 correction**

```bash
git add pmbot/main.py tests/test_main.py
git commit -m "test: preserve recovery after candidate exclusion"
```
