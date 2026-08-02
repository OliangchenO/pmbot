# Empty Quote Rescan Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refill a selected quoting slot by rescanning once when an empty-inventory selected market produces no submittable quote.

**Architecture:** `_quote_all()` will collect selected, flat market IDs whose final quote list is empty. After processing every market and before the next quote cycle, it will invoke the existing `_rescan(rotate=True)` once when the existing rotation debounce permits it. Held markets remain managed through inventory recovery and never trigger this replacement path.

**Tech Stack:** Python 3, asyncio, pytest.

## Global Constraints

- Reuse `_rescan(rotate=True)` and its `ROTATE_MIN_INTERVAL_SECS` debounce; do not duplicate market-selection logic.
- A market with `abs(unpaired) >= MIN_TAKER_SHARES` must not trigger rescan.
- Do not change pricing, risk thresholds, order submission, or `config.yaml`.
- Preserve the user's existing uncommitted `config.yaml` change.
- Do not create a git commit unless the user explicitly asks.

---

### Task 1: Regression tests for empty-quote replacement

**Files:**
- Modify: `tests/test_main.py`
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: `Bot._quote_all()`, `Bot._rescan(initial=False, rotate=False)`, `MIN_TAKER_SHARES`.
- Produces: regression coverage proving rescan happens for a selected flat empty-quote market and does not happen for a held market.

- [ ] **Step 1: Write the failing tests**

Add asynchronous scenarios using the existing `Bot` test fixture. Stub only the scanner boundary (`bot._rescan`) with an async recorder; retain the real `_quote_all()` decision path. Configure an in-range market whose book produces `desired == []`, then assert one call with `rotate=True`.

```python
async def test_quote_all_rescans_when_selected_flat_market_has_no_submittable_quotes():
    bot, market = _make_quote_bot_with_empty_desired()
    calls = []

    async def record_rescan(*, initial=False, rotate=False):
        calls.append((initial, rotate))

    bot._rescan = record_rescan
    await bot._quote_all()
    assert calls == [(False, True)]
```

Add a second scenario with `unpaired_shares()` returning `MIN_TAKER_SHARES`; assert no rescan.

```python
async def test_quote_all_does_not_rescan_held_market_with_no_submittable_quotes():
    bot, market = _make_quote_bot_with_empty_desired(unpaired=main.MIN_TAKER_SHARES)
    calls = []

    async def record_rescan(*, initial=False, rotate=False):
        calls.append((initial, rotate))

    bot._rescan = record_rescan
    await bot._quote_all()
    assert calls == []
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/test_main.py -k "rescan and submittable" -v`

Expected: the flat-market test fails because `_quote_all()` does not request a rescan.

- [ ] **Step 3: Add one debounce regression test**

Use two selected flat markets that both finish with an empty quote list and assert only one recorded rescan call. This catches an implementation that rescans inside the per-market loop.

```python
async def test_quote_all_batches_multiple_flat_empty_markets_into_one_rescan():
    bot = _make_quote_bot_with_two_empty_selected_markets()
    calls = []

    async def record_rescan(*, initial=False, rotate=False):
        calls.append((initial, rotate))

    bot._rescan = record_rescan
    await bot._quote_all()
    assert calls == [(False, True)]
```

- [ ] **Step 4: Run the three tests to verify they fail for the missing behavior**

Run: `.venv\Scripts\python.exe -m pytest tests/test_main.py -k "rescan and submittable" -v`

Expected: all flat-market rescan expectations fail; the held-market guard remains a behavior specification.

### Task 2: Batch a safe replacement rescan after quote decisions

**Files:**
- Modify: `pmbot/main.py` in `Bot._quote_all()`
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: the final per-market `desired` list, `selected_cids`, `unpaired`, `_last_rotate`, `ROTATE_MIN_INTERVAL_SECS`, and `_rescan(rotate=True)`.
- Produces: at most one asynchronous rescan request per quote loop for selected flat markets with no final quotes.

- [ ] **Step 1: Add the minimal collection state before the market loop**

```python
empty_selected_flat_cids: set[str] = set()
```

- [ ] **Step 2: Record only final empty desired lists for selected flat markets**

Immediately after all recovery/side-guard filtering that can change `desired`, add:

```python
if (not desired and m.condition_id in selected_cids
        and not needs_recovery):
    empty_selected_flat_cids.add(m.condition_id)
```

Do not record held markets, because they must remain under inventory recovery management.

- [ ] **Step 3: Request one debounced rescan after market updates finish**

After quote updates are dispatched, use the existing rotation interval before calling `_rescan`:

```python
if (empty_selected_flat_cids
        and now - self._last_rotate >= ROTATE_MIN_INTERVAL_SECS):
    log.info("%d 个空仓市场未生成可提交报价，重新扫描以补足报价槽位",
             len(empty_selected_flat_cids))
    await self._rescan(rotate=True)
```

- [ ] **Step 4: Run the targeted tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/test_main.py -k "rescan and submittable" -v`

Expected: 3 passed.

- [ ] **Step 5: Run focused quote-loop regression coverage**

Run: `.venv\Scripts\python.exe -m pytest tests/test_main.py -v`

Expected: all `tests/test_main.py` tests pass.

- [ ] **Step 6: Inspect the final diff and working tree**

Run: `git diff --check` and `git status`.

Expected: only `pmbot/main.py`, `tests/test_main.py`, and the approved design/plan documents are changed; the pre-existing `config.yaml` modification remains untouched.
