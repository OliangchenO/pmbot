# Reward Exit Stale SELL_PENDING Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent startup stale cleanup from closing a valid `SELL_PENDING` reward-exit batch and exposing its inventory to generic P1 recovery.

**Architecture:** The startup path in `Bot._run_reward_exit_batch_tick` restores open batches before ordinary quoting and inventory management. Limit stale cleanup to unfinished complementary-take batches; retain the existing non-closed-batch lock synchronization for `SELL_PENDING`.

**Tech Stack:** Python 3, asyncio, unittest, existing `Bot` test doubles.

## Global Constraints

- Modify only the startup stale-cleanup state predicate and its regression test.
- Do not change live configuration, start/restart the bot, or submit/cancel orders.
- Preserve `TAKE_PENDING` cleanup at the configured stale threshold.

---

### Task 1: Preserve SELL_PENDING batches at startup

**Files:**
- Modify: `pmbot/main.py:1964-1993`
- Modify: `tests/test_main.py`

**Interfaces:**
- Consumes: `MetricsStore.get_open_reward_exit_batches() -> list[dict]` with `status`, `created_ts`, `batch_id`, and `cid`.
- Produces: `Bot._run_reward_exit_batch_tick(now)` that closes only stale `TAKE_PENDING` batches and retains a `SELL_PENDING` CID in `_reward_exit_locked`.

- [ ] **Step 1: Write the failing test**

Add an async regression test using the existing bot/metrics test doubles. Seed an old batch with `status="SELL_PENDING"`, invoke `_run_reward_exit_batch_tick(now)` with `now - created_ts > 900`, then assert the batch was not passed to `close_reward_exit_batch` and its CID is present in `bot._reward_exit_locked`.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\\Scripts\\python.exe -m unittest tests.test_main.<new_test_name>`

Expected: FAIL because the prior startup cleanup accepts both `TAKE_PENDING` and `SELL_PENDING`.

- [ ] **Step 3: Write minimal implementation**

Replace the startup cleanup state predicate with:

```python
if b["status"] != "TAKE_PENDING":
    continue
```

Update the nearby comment to state that only stale incomplete TAKE batches are closed.

- [ ] **Step 4: Run focused tests to verify they pass**

Run: `.venv\\Scripts\\python.exe -m unittest tests.test_main`

Expected: PASS, including the new SELL_PENDING restart regression and existing reward-exit coverage.

- [ ] **Step 5: Run final diff and targeted verification**

Run: `git diff --check` and `.venv\\Scripts\\python.exe -m unittest tests.test_main tests.test_reward_exit_lifecycle`

Expected: no whitespace errors and all selected tests pass.
