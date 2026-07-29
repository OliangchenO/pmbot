# Trade Audit Log Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce an append-only JSONL audit trail that closes every live order, WS fill, and merge lifecycle.

**Architecture:** A small `AuditLogger` owns durable JSONL writes. Live broker code supplies order and fill facts, while the bot supplies the strategy path and pair-economics snapshot. The merger returns a typed result used to emit submitted, confirmed, and failed merge events.

**Tech Stack:** Python 3, JSONL, existing `pytest`, existing runtime logging.

## Global Constraints

- Do not start, stop, or modify the live process.
- Never claim an on-chain hash or redeemed amount unless returned by the relayer/RPC or verified balance data.
- Preserve existing text log and metrics behaviour.
- Do not commit without explicit user request.

---

### Task 1: Durable audit event writer

**Files:**
- Create: `pmbot/audit.py`
- Test: `tests/test_audit.py`

**Interfaces:**
- Produces `AuditLogger(path: str | None)` with `record(event: dict) -> None`.

- [ ] Write a test that records an event and verifies one JSON object with `ts` and `event` is appended.
- [ ] Run the test and verify it fails because `pmbot.audit` does not exist.
- [ ] Implement the minimal locking, append, flush, and non-fatal error behaviour.
- [ ] Run `pytest tests/test_audit.py -q` and verify it passes.

### Task 2: Order, cancellation, and WS-fill facts

**Files:**
- Modify: `pmbot/brokers.py`
- Test: `tests/test_brokers.py`, `tests/test_userfeed.py`

**Interfaces:**
- Consumes `AuditLogger.record`.
- Produces audit events containing external IDs plus `path`, `unpaired_cost`, `pair_cap`, and `expected_pair_pnl` whenever known.

- [ ] Write failing tests for a placed/cancelled order and a WS fill carrying the requested identifiers and economics fields.
- [ ] Run the focused tests and verify the expected missing audit event assertions fail.
- [ ] Add metadata to resting orders and emit audit records from placement, cancellation, and fill handling.
- [ ] Run the focused tests and verify they pass.

### Task 3: Strategy-path and pair-economics snapshots

**Files:**
- Modify: `pmbot/main.py`, `pmbot/brokers.py`
- Test: `tests/test_main.py`

**Interfaces:**
- Produces per-quote metadata with one of `normal`, `inventory_recovery`, `cooldown_recovery`, `forced_hedge`.

- [ ] Write failing tests that recovery and forced hedge submissions carry the required path and pair calculations.
- [ ] Run focused tests and verify they fail on absent metadata.
- [ ] Thread metadata through quote submission without changing price, size, or order sequencing.
- [ ] Run focused tests and verify they pass.

### Task 4: Merge confirmation closure

**Files:**
- Modify: `pmbot/merger.py`, `pmbot/brokers.py`
- Test: `tests/test_merger.py`, `tests/test_brokers.py`

**Interfaces:**
- Produces merge events `merge_submitted`, `merge_confirmed`, and `merge_failed` with transaction identifiers and non-invented redemption values.

- [ ] Write failing tests for relayer submitted/confirmed and absent redemption evidence.
- [ ] Run tests and verify failures identify the current boolean-only result.
- [ ] Return structured merge results and publish all merge lifecycle events.
- [ ] Run focused merger/broker tests and verify they pass.

### Task 5: Full verification

**Files:**
- Modify: no additional files expected

- [ ] Run all tests and report any unrelated failure separately.
- [ ] Run `git diff --check`.
- [ ] Inspect a generated JSONL sample for all required fields and status transitions.
