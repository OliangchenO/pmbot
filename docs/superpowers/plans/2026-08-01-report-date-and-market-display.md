# Report Date and Market Display Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let `report` render an audited UTC calendar day chosen with `--date`, and display full market questions in terminal tables.

**Architecture:** Reuse `MetricsStore.daily_report(date)`, which already applies closed UTC day bounds. Add date-scoped companion summaries for rewards and the cash ledger so a historical daily decomposition does not mix with rolling 24-hour figures. Remove application-level slicing and let Rich wrap cells to fit the terminal.

**Tech Stack:** Python 3, argparse, SQLite, Rich, pytest.

## Global Constraints

- `report` without `--date` remains a current-UTC-day report.
- `report --date YYYY-MM-DD` uses that one UTC calendar date for daily PnL, recovery, rewards, and trading ledger figures.
- All-time totals remain all-time and retain that label.
- Application code must not silently truncate market questions; Rich may wrap cells to terminal width.
- `performance --date` remains unchanged.

---

### Task 1: Add date-scoped metrics summaries

**Files:**
- Modify: `pmbot/metrics.py:951-973`
- Modify: `pmbot/metrics.py:1040-1092`
- Test: `tests/test_metrics.py`

**Interfaces:**
- Consumes: `MetricsStore.daily_report(date: str | None) -> dict` UTC day-boundary convention.
- Produces: `reward_totals(date: str | None = None) -> dict` and `trading_pnl_ledger(date: str | None = None) -> dict`. No argument preserves rolling 24-hour behavior; an explicit date returns the selected UTC day's value in the existing `*_24h` key.

- [ ] **Step 1: Write the failing test**

```python
def test_report_totals_use_the_requested_utc_day(tmp_path):
    store = MetricsStore(str(tmp_path / "metrics.db"))
    old = datetime(2026, 7, 30, tzinfo=timezone.utc)
    new = old + timedelta(days=1)
    store.record_fill({"ts": old.timestamp() + 60, "cid": "old", "market": "Old",
                       "side": "YES", "token": "yes", "price": 0.40, "size": 10})
    store.record_fill({"ts": new.timestamp() + 60, "cid": "new", "market": "New",
                       "side": "YES", "token": "yes", "price": 0.40, "size": 20})
    store.record_realized_reward("2026-07-30", 1.25)
    store.record_realized_reward("2026-07-31", 2.50)

    assert store.reward_totals("2026-07-30")["realized_24h"] == 1.25
    assert store.trading_pnl_ledger("2026-07-30")["realized_24h"] == -4.0
```

- [ ] **Step 2: Run the test and confirm it fails**

Run: `pytest tests/test_metrics.py::test_report_totals_use_the_requested_utc_day -v`

Expected: FAIL because the two methods do not accept a date argument.

- [ ] **Step 3: Implement the minimal metrics change**

Accept `date: str | None = None` in both methods. For a supplied date, derive UTC midnight and the exclusive next midnight; query fills, fees, and merges with `ts >= start AND ts < end`. Keep total queries unchanged, and obtain reward day totals with `WHERE date = ?`.

- [ ] **Step 4: Run the focused test and confirm it passes**

Run: `pytest tests/test_metrics.py::test_report_totals_use_the_requested_utc_day -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pmbot/metrics.py tests/test_metrics.py
git commit -m "feat: scope report totals to UTC date"
```

### Task 2: Wire report date through the CLI

**Files:**
- Modify: `pmbot/main.py:208-275`
- Modify: `pmbot/main.py:1956-2005`
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: the date-aware metrics methods from Task 1.
- Produces: `cmd_report(cfg: dict, date: str | None) -> None`; argparse passes `report --date` to this function.

- [ ] **Step 1: Write the failing test**

```python
def test_report_command_renders_requested_utc_date(tmp_path):
    cfg = dict(BASE_CFG)
    cfg["metrics"] = {"db_path": str(tmp_path / "metrics.db")}
    store = main._metrics_store(cfg)
    store.record_realized_reward("2026-07-30", 1.25)
    store.record_realized_reward("2026-07-31", 2.50)
    store.close()

    with main.console.capture() as capture:
        main.cmd_report(cfg, "2026-07-30")

    output = capture.get()
    assert "PnL report — 2026-07-30" in output
    assert "$+1.25" in output
    assert "$+2.50" not in output
```

- [ ] **Step 2: Run the test and confirm it fails**

Run: `pytest tests/test_main.py::test_report_command_renders_requested_utc_date -v`

Expected: FAIL because `cmd_report` currently accepts only `cfg`.

- [ ] **Step 3: Implement the CLI contract**

Add `--date` to the `report` subparser, pass it to `cmd_report`, and pass it through to `daily_report`, `reward_totals`, and `trading_pnl_ledger`. With an explicit date, rename the report score-table column from `Last 24h` to `Selected day`; otherwise retain the existing label and values.

- [ ] **Step 4: Run the focused test and confirm it passes**

Run: `pytest tests/test_main.py::test_report_command_renders_requested_utc_date -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pmbot/main.py tests/test_main.py
git commit -m "feat: add report date filter"
```

### Task 3: Preserve complete market questions in output

**Files:**
- Modify: `pmbot/main.py:275-430`
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: market dictionaries containing the full `market` string.
- Produces: terminal output containing the complete original question, with wrapping delegated to Rich.

- [ ] **Step 1: Write the failing test**

```python
def test_performance_command_keeps_full_market_question(tmp_path):
    cfg = dict(BASE_CFG)
    cfg["metrics"] = {"db_path": str(tmp_path / "metrics.db")}
    question = "Will the complete market question remain visible in PowerShell output?"
    store = main._metrics_store(cfg)
    store.record_fill({"cid": "cid", "market": question, "side": "YES",
                       "token": "yes", "price": 0.5, "size": 10})
    store.close()

    with main.console.capture() as capture:
        main.cmd_performance(cfg, None)

    assert question in capture.get()
```

- [ ] **Step 2: Run the test and confirm it fails**

Run: `pytest tests/test_main.py::test_performance_command_keeps_full_market_question -v`

Expected: FAIL because the renderer slices the label before creating the Rich table row.

- [ ] **Step 3: Remove presentation-layer slicing**

Replace uses such as `m["market"][:40]` with `m["market"]` in `cmd_performance` and `cmd_trades`. Do not alter persisted data, table numeric columns, or SQLite queries.

- [ ] **Step 4: Run focused and regression tests**

Run: `pytest tests/test_main.py::test_performance_command_keeps_full_market_question -v`

Expected: PASS.

Run: `pytest tests/test_metrics.py tests/test_main.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pmbot/main.py tests/test_main.py
git commit -m "fix: show complete market names in reports"
```

