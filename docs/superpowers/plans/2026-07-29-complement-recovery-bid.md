# Complement Recovery Bid Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep a capped complementary BUY quote for meaningful unpaired inventory even when ordinary one-sided flow protection blocks that token's normal quote.

**Architecture:** `Bot._inventory_recovery_quotes()` already selects and caps the complement by pair economics. The quote loop must preserve that output as a risk-reduction exception instead of applying the ordinary `guards.allow_side()` filter to it. Normal two-sided quoting remains subject to the guard.

**Tech Stack:** Python 3, pytest, existing `Bot`/`PaperBroker` test fixtures.

## Global Constraints

- Do not start, restart, or alter the live process.
- Do not change forced-taker-hedge thresholds or pair-cap arithmetic.
- Do not commit Git changes.

---

### Task 1: Preserve capped complementary recovery bid across a side guard

**Files:**
- Modify: `tests/test_main.py`
- Modify: `pmbot/main.py:966-978`

**Interfaces:**
- Consumes: `Bot._inventory_recovery_quotes(market, desired, unpaired)` returns only the complementary `Quote`, capped by `unpaired` shares and pair-cap price.
- Produces: Quote-loop behavior where an unpaired NO position retains a `BUY YES` recovery bid even if `guards.allow_side(yes_token, now)` is false.

- [ ] **Step 1: Write the failing test**

```python
def test_unpaired_no_keeps_capped_yes_recovery_bid_when_yes_side_is_blocked(tmp_path):
    # Set a 9.67-share NO imbalance and a 0.62 cost basis.
    # Block YES via the ordinary side guard.
    # Run _quote_all and assert the sole quote is BUY YES, size 9.67,
    # and price <= 0.38.
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_main.py::test_unpaired_no_keeps_capped_yes_recovery_bid_when_yes_side_is_blocked -q`

Expected: FAIL because the generic `guards.allow_side()` filter removes the recovery bid.

- [ ] **Step 3: Write minimal implementation**

```python
if abs(unpaired) >= MIN_TAKER_SHARES:
    desired = self._inventory_recovery_quotes(m, desired, unpaired)
else:
    desired = [q for q in desired if self.guards.allow_side(q.token_id, now)]
```

Keep the existing held-market and whole-market cooldown safety branches unchanged.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_main.py::test_unpaired_no_keeps_capped_yes_recovery_bid_when_yes_side_is_blocked -q`

Expected: PASS.

- [ ] **Step 5: Run regression verification**

Run: `pytest tests/test_main.py -q`

Expected: PASS with no test failures.
