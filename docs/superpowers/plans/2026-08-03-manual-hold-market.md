# 单市场完全人工持仓 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 使配置指定的市场完全不被 bot 选入或处理，同时保留其既有仓位和订单。

**Architecture:** 在 `risk.manual_hold_cids` 定义 CID 集合。重扫时把它合并到 Gamma 排除集，并避免将该市场从旧选择集中移除时自动撤单；报价循环在接触订单簿、恢复逻辑和强制对冲前跳过该 CID。

**Tech Stack:** Python 3、pytest、YAML。

## Global Constraints

- `manual_hold_cids` 默认为空列表，CID 比较精确匹配。
- 手工持仓市场不生成、替换或撤销订单；既有仓位与交易所挂单保持原样。
- 不重启 live 进程、不提交或撤销任何交易所订单。
- 不带入现有 `config.yaml` 中无关的 `recovery_escalate_after_minutes` 改动。

---

### Task 1: 将人工持仓市场排除在扫描和移除副作用之外

**Files:**
- Modify: `pmbot/main.py:961-1049`
- Modify: `config.yaml:risk`
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: `risk.manual_hold_cids: list[str] | None`
- Produces: `_manual_hold_cids() -> set[str]`，供 `_rescan` 与 `_quote_all` 使用。

- [ ] **Step 1: Write the failing test**

```python
def test_rescan_excludes_manual_hold_cid_without_cancelling_its_orders(tmp_path, monkeypatch):
    bot = _bot(tmp_path)
    market = _market()
    bot.cfg["risk"]["manual_hold_cids"] = [market.condition_id]
    broker = _setup(bot, tmp_path, market)
    broker.set_quotes(market, [Quote(market.yes_token, 0.45, 10.0)])
    monkeypatch.setattr(main.gamma, "scan", lambda *_args: [])

    asyncio.run(bot._rescan())

    assert broker.open_quotes(market) == [Quote(market.yes_token, 0.45, 10.0)]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\\Scripts\\python.exe -m pytest tests/test_main.py::test_rescan_excludes_manual_hold_cid_without_cancelling_its_orders -q`

Expected: FAIL because `_rescan` treats the deselected market like a normal rotation and clears its orders.

- [ ] **Step 3: Write minimal implementation**

```python
def _manual_hold_cids(self) -> set[str]:
    return {str(cid) for cid in (self.cfg.get("risk") or {}).get("manual_hold_cids") or []}

# _rescan
manual_hold = self._manual_hold_cids()
exclude |= manual_hold
...
if old_m.condition_id in old_cids - new_cids and old_m.condition_id not in manual_hold:
    await self._broker_call(self.broker.cancel_quotes_for_market, old_m)
```

Add `manual_hold_cids` and the Masterson CID to `config.yaml` under `risk`, without modifying other user changes.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\\Scripts\\python.exe -m pytest tests/test_main.py::test_rescan_excludes_manual_hold_cid_without_cancelling_its_orders -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pmbot/main.py config.yaml tests/test_main.py
git commit -m "feat: exclude manually held markets from scans"
```

### Task 2: 使持仓市场跳过报价、恢复与强制对冲

**Files:**
- Modify: `pmbot/main.py:1415-1648`
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: `Bot._manual_hold_cids() -> set[str]`
- Produces: 手工 CID 不进入 `_quote_all` 的订单簿、恢复、报价更新或强制对冲路径。

- [ ] **Step 1: Write the failing test**

```python
def test_quote_all_does_not_touch_manually_held_market(tmp_path):
    bot = _bot(tmp_path)
    market = _market()
    bot.cfg["risk"]["manual_hold_cids"] = [market.condition_id]
    broker = _setup(bot, tmp_path, market)
    broker._positions[market.yes_token] = 10.0
    old = Quote(market.no_token, 0.45, 10.0)
    broker.set_quotes(market, [old])

    asyncio.run(bot._quote_all())

    assert broker.open_quotes(market) == [old]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\\Scripts\\python.exe -m pytest tests/test_main.py::test_quote_all_does_not_touch_manually_held_market -q`

Expected: FAIL because the held market is currently included in `all_markets` and reconciled.

- [ ] **Step 3: Write minimal implementation**

```python
manual_hold = self._manual_hold_cids()
...
for m in all_markets:
    if m.condition_id in manual_hold:
        self._quote_block_reasons[m.condition_id] = "人工持仓：bot 不处理该市场"
        continue
```

Place the branch before `unpaired_shares`, book access, metrics sampling, recovery quote generation and reconciliation.

- [ ] **Step 4: Run focused tests to verify they pass**

Run: `.venv\\Scripts\\python.exe -m pytest tests/test_main.py::test_rescan_excludes_manual_hold_cid_without_cancelling_its_orders tests/test_main.py::test_quote_all_does_not_touch_manually_held_market -q`

Expected: PASS.

- [ ] **Step 5: Run regression verification and commit**

Run: `.venv\\Scripts\\python.exe -m pytest tests/test_main.py -q`

Expected: PASS.

```bash
git add pmbot/main.py tests/test_main.py
git commit -m "feat: skip bot processing for manually held markets"
```

## Self-Review

- Spec coverage: Task 1 prevents reselection and preserves existing orders; Task 2 blocks every quote/recovery/FAK path.
- Placeholder scan: no unresolved implementation or test steps.
- Type consistency: both tasks use the same `Bot._manual_hold_cids() -> set[str]` interface.
