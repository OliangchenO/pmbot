# Held-market minimum net shares Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Do not adopt a live position as a held market unless its unpaired net position is at least five shares.

**Architecture:** Keep the change at the live broker's held-market hydration boundary. It reads the existing per-CID `yes` and `no` shares in `_positions`; CIDs below the `abs(yes - no) >= 5` threshold are omitted before the Gamma lookup. Existing pending-hedge behavior and position accounting remain untouched.

**Tech Stack:** Python 3, pytest, existing `LiveBroker` test doubles.

## Global Constraints

- Treat `abs(yes_shares - no_shares) < 5` as no inventory exposure for held-market adoption.
- Do not alter pending-hedge handling, live configuration, orders, or running processes.
- Keep skipped CIDs out of Gamma hydration and held-market management.

---

### Task 1: Filter sub-threshold held-market hydration

**Files:**
- Modify: `tests/test_brokers.py:889-910`
- Modify: `pmbot/brokers.py:1508-1524`

**Interfaces:**
- Consumes: `LiveBroker._positions: dict[str, dict]`, where each value contains numeric `yes` and `no` shares.
- Produces: `LiveBroker._hydrate_held_markets(condition_ids: set[str]) -> list[Market]`, which only fetches CIDs whose net shares are at least `5`.

- [ ] **Step 1: Write failing boundary tests**

```python
@pytest.mark.parametrize("yes, no", [(4.0, 0.0), (4.0, 4.0)])
def test_live_hydrate_held_markets_skips_subthreshold_net_position(
        monkeypatch, yes, no):
    market = _market()
    stub = _live_stub()
    stub._markets = {}
    stub._positions = {market.condition_id: {"yes": yes, "no": no, "value": 0.0}}
    fetched = []
    monkeypatch.setattr(brokers.gamma, "fetch_market", lambda cid: fetched.append(cid))

    assert LiveBroker._hydrate_held_markets(stub, {market.condition_id}) == []
    assert fetched == []
    assert LiveBroker.held_markets(stub) == []


def test_live_hydrate_held_markets_adopts_five_net_shares(monkeypatch):
    market = _market()
    stub = _live_stub()
    stub._markets = {}
    stub._positions = {market.condition_id: {"yes": 5.0, "no": 0.0, "value": 0.0}}
    monkeypatch.setattr(brokers.gamma, "fetch_market", lambda cid: market)

    assert LiveBroker._hydrate_held_markets(stub, {market.condition_id}) == [market]
    assert LiveBroker.held_markets(stub) == [market]
```

The regression should fail under current code because four net shares and equal four-share sides still invoke Gamma and adopt the market.

- [ ] **Step 2: Run tests to verify red**

Run: `.venv\\Scripts\\python.exe -m pytest tests/test_brokers.py -k "hydrate_held_markets" -v`

Expected: FAIL for each sub-threshold case because `_hydrate_held_markets` calls `gamma.fetch_market` and adopts the CID.

- [ ] **Step 3: Make the minimal production change**

At the start of each `_hydrate_held_markets` loop iteration, read the position record and skip when the hand-derived net size is below five:

```python
position = self._positions.get(cid, {})
net_shares = abs(float(position.get("yes", 0.0)) - float(position.get("no", 0.0)))
if net_shares < 5:
    continue
```

Leave the existing known-market check, Gamma lookup, unresolved-CID assignment, and return value unchanged.

- [ ] **Step 4: Run focused tests to verify green**

Run: `.venv\\Scripts\\python.exe -m pytest tests/test_brokers.py -k "hydrate_held_markets" -v`

Expected: PASS. The sub-threshold cases make no lookup and return no held market; exactly five shares remains adopted.

- [ ] **Step 5: Run regression verification**

Run: `.venv\\Scripts\\python.exe -m pytest tests/test_brokers.py -v`

Expected: PASS, including existing hydration, inventory, and position-snapshot coverage.

- [ ] **Step 6: Commit implementation**

Commit `pmbot/brokers.py` and `tests/test_brokers.py` with message `fix: ignore subthreshold held positions`.
