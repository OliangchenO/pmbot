# Recovery Order Retention Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve an active, safe complementary recovery order through normal quote recalculations so it retains queue position.

**Architecture:** Add a narrow decision in `Bot._quote_all()` after the recovery quote has been computed and before `strategy.reconcile_quotes()`. When a market has managed inventory and its sole open quote already matches the complement, current net exposure, and fee-inclusive hard price cap, reconcile against that resting quote instead of the newly computed normal-refresh price. Existing stale-book, near-resolution, risk, GTD-expiry, and order-reconciliation paths remain unchanged.

**Tech Stack:** Python 3.14, pytest.

## Global Constraints

- Do not alter live configuration, process state, or live orders.
- Retain only the complementary recovery order for `abs(unpaired) >= MIN_TAKER_SHARES`.
- Preserve normal-market refresh behavior and all existing global safety pulls.
- Repost at GTD refresh margin using the existing overlap-safe `LiveBroker.set_quotes()` path.

---

### Task 1: Retain a safe recovery order during ordinary book movement

**Files:**
- Modify: `tests/test_main.py`
- Modify: `pmbot/main.py:1626-1660`

**Interfaces:**
- Consumes: `broker.open_quotes(m) -> list[Quote]`, `broker.unpaired_shares(m) -> float`, and `_forced_hedge_max_price(m, basis) -> float`.
- Produces: unchanged `updates` behavior when recovery order must change; no update for a safe equal-size recovery order whose only difference is normal recomputed price.

- [ ] **Step 1: Write the failing test**

```python
async def scenario():
    bot, market = _quote_loop_bot_with_empty_strategy(tmp_path, 12.0)
    bot.broker.set_quotes(market, [Quote(market.no_token, 0.33, 12.0)])
    bot._forced_hedge_max_price = lambda *_args: 0.35

    await bot._quote_all()

    assert bot.broker.open_quotes(market) == [Quote(market.no_token, 0.33, 12.0)]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\\Scripts\\python.exe -m pytest tests/test_main.py::test_quote_all_keeps_safe_recovery_order_at_original_price -q`

Expected: FAIL because the ordinary quote calculation changes the recovery bid and `set_quotes()` replaces the resting order.

- [ ] **Step 3: Write minimal implementation**

```python
if self._should_retain_recovery_quote(m, current, unpaired):
    final = current
else:
    final = strategy.reconcile_quotes(current, desired, requote_move)
```

The helper must require exactly one current complement quote, equal remaining size to `abs(unpaired)`, known cost basis, and current price no higher than `_forced_hedge_max_price()`.

- [ ] **Step 4: Add safety-change tests**

```python
assert bot._should_retain_recovery_quote(market, [Quote(market.no_token, 0.36, 12)], 12) is False
assert bot._should_retain_recovery_quote(market, [Quote(market.no_token, 0.33, 10)], 12) is False
```

- [ ] **Step 5: Run focused tests to verify green**

Run: `.venv\\Scripts\\python.exe -m pytest tests/test_main.py -q`

Expected: PASS, including equal-size recovery and retention/change tests.

- [ ] **Step 6: Run static and full verification**

Run: `.venv\\Scripts\\python.exe -m compileall -q pmbot; .venv\\Scripts\\python.exe -m pytest -q; git diff --check`

Expected: compilation and diff check succeed; report the known PaperBroker state-pollution failures separately if they recur.

- [ ] **Step 7: Commit**

```bash
git add pmbot/main.py tests/test_main.py docs/superpowers/plans/2026-08-04-recovery-order-retention.md
git commit -m "fix: retain safe recovery orders"
```
