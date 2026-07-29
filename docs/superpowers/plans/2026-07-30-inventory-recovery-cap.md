# Inventory Recovery Pair-Cap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve a safe passive complement order when a newly calculated recovery price exceeds the pair-cost cap.

**Architecture:** `Bot._inventory_recovery_quotes` remains the single recovery filter. Instead of dropping an over-cap complement quote, it returns the same token and capped inventory size at the existing computed pair cap. Forced taker hedging remains unchanged.

**Tech Stack:** Python, pytest.

## Global Constraints

- Do not change live credentials, forced-hedge permission, or order placement behavior.
- Do not quote the held token or more than unpaired shares.
- Unknown cost basis remains fail-closed.

---

### Task 1: Clamp a recovery quote to the safe pair cap

**Files:**
- Modify: `tests/test_main.py:526-540`
- Modify: `pmbot/main.py:853-872`

- [ ] **Step 1: Write the failing test**

Assert that a complementary quote at `0.340` with a `0.330` pair cap becomes `Quote(yes_token, 0.330, 50.0)`.

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv\\Scripts\\python.exe -m pytest tests/test_main.py -k inventory_recovery_rejects_quote_above_pair_cost_cap -v`

Expected: failure because the current code returns an empty list.

- [ ] **Step 3: Write minimal implementation**

Replace the over-cap rejection with a `Quote` on the complement token at `max_price`, preserving `min(q.size, abs(unpaired))`.

- [ ] **Step 4: Run focused and full tests**

Run: `.venv\\Scripts\\python.exe -m pytest tests/test_main.py -k inventory_recovery -v`, then `.venv\\Scripts\\python.exe -m pytest tests -q`.
