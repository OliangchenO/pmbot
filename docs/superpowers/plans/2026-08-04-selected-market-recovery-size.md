# Selected Market Recovery Size Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ensure a selected market with unpaired inventory places only an equal-size complementary recovery bid.

**Architecture:** Keep `_inventory_recovery_quotes()` as the single source for the complement token, fee-inclusive cap, minimum size, and `abs(unpaired)` quantity. Change `_selected_market_recovery_quotes()` to return that recovery quote directly while inventory is unpaired, instead of adding it to the ordinary quote.

**Tech Stack:** Python 3.14, pytest.

## Global Constraints

- Do not alter live configuration, process state, or live orders.
- Preserve the existing `MIN_TAKER_SHARES`, CLOB minimum-size, and pair-cost cap checks.
- Preserve ordinary selected-market two-sided quotes when no managed inventory exists.

---

### Task 1: Make selected-market recovery equal its net inventory

**Files:**
- Modify: `tests/test_main.py`
- Modify: `pmbot/main.py:1257-1280`

**Interfaces:**
- Consumes: `Bot._inventory_recovery_quotes(m, normal, unpaired) -> list[Quote]`.
- Produces: `Bot._selected_market_recovery_quotes(m, normal, unpaired) -> list[Quote]`, returning exactly one complement quote of `abs(unpaired)` during managed recovery.

- [ ] **Step 1: Write the failing test**

```python
def test_selected_market_recovery_quote_is_exactly_unpaired_size(tmp_path):
    bot = _bot(tmp_path)
    market = _market()
    normal = [Quote(market.yes_token, 0.60, 69.0),
              Quote(market.no_token, 0.35, 69.0)]
    bot.broker.state.positions[market.condition_id] = Position(yes_shares=68.0)

    quotes = bot._selected_market_recovery_quotes(market, normal, 68.0)

    assert quotes == [Quote(market.no_token, 0.35, 68.0)]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\\Scripts\\python.exe -m pytest tests/test_main.py::test_selected_market_recovery_quote_is_exactly_unpaired_size -q`

Expected: FAIL because current code returns the ordinary quote plus the 68-share recovery quantity.

- [ ] **Step 3: Write minimal implementation**

```python
recovery = self._inventory_recovery_quotes(m, normal, unpaired)
if not recovery:
    return []
return recovery
```

- [ ] **Step 4: Run focused tests to verify they pass**

Run: `.venv\\Scripts\\python.exe -m pytest tests/test_main.py -q`

Expected: PASS, including both the new equal-size test and the existing inventory-recovery coverage.

- [ ] **Step 5: Run static and full verification**

Run: `.venv\\Scripts\\python.exe -m compileall -q pmbot; .venv\\Scripts\\python.exe -m pytest -q; git diff --check`

Expected: compilation succeeds, tests pass, and diff check produces no output.

- [ ] **Step 6: Commit**

```bash
git add pmbot/main.py tests/test_main.py docs/superpowers/plans/2026-08-04-selected-market-recovery-size.md
git commit -m "fix: size recovery quotes to net inventory"
```
