# Inventory Recovery Pair-Cost Cap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent passive inventory-recovery quotes from completing a YES+NO pair at a cost above $1 after applicable fees.

**Architecture:** Preserve the existing quote-generation pipeline, then apply one additional economic filter only to recovery quotes. The filter obtains the unpaired inventory basis from the broker and uses the existing fee-aware forced-hedge price ceiling. It affects normal inventory recovery and cooldown recovery, while ordinary two-sided quotes and forced-taker hedges retain their current behavior.

**Tech Stack:** Python 3, pytest, existing `pmbot.main.Bot` and `pmbot.brokers.LiveBroker` interfaces.

## Global Constraints

- Do not start, restart, or alter a live trading process.
- Do not change normal two-sided quote pricing, forced-hedge escalation, quote size, or merge behavior.
- Unknown unpaired cost basis must fail closed: no passive recovery quote is emitted.
- Preserve the existing fee-aware `_forced_hedge_max_price()` calculation as the source of the cap.
- Do not commit unless the user explicitly requests it.

---

### Task 1: Specify passive recovery cost-cap behavior

**Files:**
- Modify: `tests/test_main.py`
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: `Bot._inventory_recovery_quotes(market, desired, unpaired)` and `Bot._cooldown_recovery_quotes(market, desired, unpaired)`.
- Produces: regression coverage proving a complementary quote at or below the cap is kept, while a quote above the cap or lacking a basis is removed.

- [x] **Step 1: Write failing tests**

Add a small fake broker to the existing `Bot` test fixture with `unpaired_cost_basis()` returning a configurable value. For a market with `tick=0.001`, a held NO basis of `0.669`, and a desired YES quote of `0.329`, assert that a 50-share quote is retained. Then assert that desired YES quotes at `0.340` and `0.372` yield an empty list. Add the same above-cap assertion through `_cooldown_recovery_quotes()`. Add an assertion that `None` basis yields an empty list.

```python
assert bot._inventory_recovery_quotes(market, [Quote(market.yes_token, 0.329, 50)], -50) == [Quote(market.yes_token, 0.329, 50)]
assert bot._inventory_recovery_quotes(market, [Quote(market.yes_token, 0.372, 50)], -50) == []
assert bot._cooldown_recovery_quotes(market, [Quote(market.yes_token, 0.340, 50)], -50) == []
```

- [x] **Step 2: Run the focused tests and verify RED**

Run: `rtk pytest tests/test_main.py -k recovery`

Expected: the above-cap and missing-basis assertions fail because the current recovery helper filters only token direction and size.

### Task 2: Enforce the cap in recovery helpers

**Files:**
- Modify: `pmbot/main.py:817-842`
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: `LiveBroker.unpaired_cost_basis(market) -> float | None` and `Bot._forced_hedge_max_price(market, basis) -> float`.
- Produces: `Bot._inventory_recovery_quotes()` emits only complementary recovery quotes at or below the economic ceiling; cooldown and held-market helpers inherit the behavior.

- [x] **Step 1: Implement the minimal filter**

Change `_inventory_recovery_quotes` from a static method to an instance method so it can call `self.broker.unpaired_cost_basis(m)`. Keep the existing minimum-size, complement-token, and size-cap behavior. If the broker does not expose the method or returns `None`, return `[]`. Otherwise compute `max_price = self._forced_hedge_max_price(m, basis)` and retain only a complementary quote with `q.price <= max_price + 1e-9`.

```python
basis_fn = getattr(self.broker, "unpaired_cost_basis", None)
basis = basis_fn(m) if basis_fn else None
if basis is None:
    return []
max_price = self._forced_hedge_max_price(m, basis)
return [
    strategy.Quote(q.token_id, q.price, min(q.size, abs(unpaired)))
    for q in desired
    if q.token_id == complement and q.price <= max_price + 1e-9
]
```

- [x] **Step 2: Run the focused tests and verify GREEN**

Run: `rtk pytest tests/test_main.py -k recovery`

Expected: all recovery tests pass, including the new below-cap, above-cap, cooldown, and unknown-basis cases.

### Task 3: Regression validation

**Files:**
- Modify: none
- Test: `tests/test_main.py`, full test suite

**Interfaces:**
- Consumes: the completed Task 2 helper behavior.
- Produces: evidence that normal quote reconciliation and forced hedge tests continue to pass.

- [x] **Step 1: Run focused forced-hedge and quote-reconciliation tests**

Run: `rtk pytest tests/test_main.py -k "forced_hedge or reconcile_quotes or recovery"`

Expected: PASS. These tests prove the new filter does not change the existing forced-hedge cap or ordinary quote replacement behavior.

- [ ] **Step 2: Run the full suite**

Run: `rtk pytest`

Result: 148 passed; one pre-existing, unrelated Gamma request-parameter test fails. It is outside this change set and must be diagnosed separately.

- [x] **Step 3: Inspect the final diff**

Run: `rtk git diff --check` and `rtk git diff -- pmbot/main.py tests/test_main.py`

Expected: only the recovery cost-cap logic and regression tests change; no credentials, configuration, or process-control files change.
