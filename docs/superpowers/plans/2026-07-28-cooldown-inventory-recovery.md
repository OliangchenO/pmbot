# Cooldown Inventory Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep a capped complementary buy order active to reduce an existing unpaired position while its market is in a guard cooldown.

**Architecture:** The quote loop will distinguish a flat market in cooldown from a market with unpaired inventory. A flat market keeps the existing full pull. A market holding at least `MIN_TAKER_SHARES` will skip normal two-sided quoting and pass its desired quotes through `_inventory_recovery_quotes`, yielding only the complementary token with size capped at the inventory gap.

**Tech Stack:** Python 3.14, asyncio, pytest.

## Global Constraints

- Do not alter forced-hedge thresholds, cooldown durations, or ordinary two-sided quoting.
- A cooldown recovery quote must never buy the same token as the unpaired inventory.
- A cooldown recovery quote must not exceed the absolute unpaired share count.

---

### Task 1: Permit complementary inventory recovery during cooldown

**Files:**
- Modify: `tests/test_main.py`
- Modify: `pmbot/main.py:768-817`

**Interfaces:**
- Consumes: `MarketGuards.allow(condition_id, now) -> bool`, `LiveBroker.unpaired_shares(market) -> float`, and `Bot._inventory_recovery_quotes(market, desired, unpaired) -> list[Quote]`.
- Produces: a quote update containing only the complementary quote for a cooled-down market with unpaired inventory, or an empty quote update for a cooled-down flat market.

- [ ] **Step 1: Write the failing test**

Create an async quote-loop test with a guard that denies the market and a `Position(yes_shares=12)`. Assert `set_quotes` receives exactly one quote with `token_id == market.no_token` and `size == 12`. Add the symmetric `Position(no_shares=12)` assertion for the YES token, and a flat-position assertion that receives `[]`.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_main.py -k cooldown -v`

Expected: FAIL because the current cooldown branch always appends `[]` before inventory recovery is evaluated.

- [ ] **Step 3: Write minimal implementation**

In `Bot._quote_all`, when `guards.allow(...)` is false, obtain `unpaired_shares(m)`. If its absolute value is below `MIN_TAKER_SHARES`, retain the existing pull behavior. Otherwise compute normal desired quotes using the current valid books, filter them through `_inventory_recovery_quotes`, and submit only that complement quote. Preserve all stale-book, unquotable-book, resolution, and theme-cap pulls.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_main.py -k cooldown -v`

Expected: PASS for both complementary directions and the flat-market pull.

- [ ] **Step 5: Run focused regression tests**

Run: `pytest tests/test_main.py -k "inventory_recovery or cooldown" -v`

Expected: PASS, including the existing inventory-cap tests.
