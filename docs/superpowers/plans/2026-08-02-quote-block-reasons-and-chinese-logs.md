# 未报价原因与中文运行日志 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在运行表格展示未报价原因，并把 pmbot 自身的运行日志改为中文易读说明。

**Architecture:** `Bot` 维护仅供面板展示的市场诊断状态，报价链路在每个早退点写入一个稳定 reason code 和中文详情。Rich 表格读取该状态并渲染新列。日志只改人类文本，保留机器事件代码、字段名和异常。

**Tech Stack:** Python 3.12、Rich、pytest。

## Global Constraints

- 不修改运行中的 LIVE 进程、订单、配置或审计 JSON schema。
- 保留 `ORDER_PLACED`、`FORCED_HEDGE_DEFERRED`、`reason=` 等稳定检索标识。
- 第三方库日志保持原样；仅修改 `pmbot.*` logger 文本。

---

### Task 1: 逐市场未报价诊断

**Files:**
- Modify: `pmbot/main.py:Bot.__init__`, `_quote_all`, `_render_status`
- Test: `tests/test_main.py`

**Interfaces:**
- Produces: `Bot._quote_block_reasons: dict[str, str]`，键为 condition id，值为中文展示文本。

- [ ] **Step 1: Write the failing test**

```python
def test_render_status_shows_flow_side_block_reason(bot, market, capsys):
    bot.markets = [market]
    bot._quote_block_reasons[market.condition_id] = "YES 买单因单边流量保护暂停（剩余 10 分钟）"
    bot._render_status()
    assert "未报价原因" in capsys.readouterr().out
    assert "单边流量保护" in capsys.readouterr().out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\\Scripts\\python.exe -m pytest tests/test_main.py::test_render_status_shows_flow_side_block_reason -v`

Expected: FAIL because the table has no reason column.

- [ ] **Step 3: Write minimal implementation**

Add a reason column and update `_quote_all` to clear/recompute the first applicable reason for each managed market. Include reason text for every existing early `continue`, an empty desired quote set, and an active side block with a computed remaining duration.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\\Scripts\\python.exe -m pytest tests/test_main.py::test_render_status_shows_flow_side_block_reason -v`

Expected: PASS.

### Task 2: 中文化 pmbot 运行日志

**Files:**
- Modify: `pmbot/audit.py`, `pmbot/books.py`, `pmbot/brokers.py`, `pmbot/controller.py`, `pmbot/gamma.py`, `pmbot/main.py`, `pmbot/merger.py`, `pmbot/metrics.py`, `pmbot/risk.py`, `pmbot/userfeed.py`
- Test: `tests/test_main.py`, `tests/test_brokers.py`, `tests/test_risk.py`

**Interfaces:**
- Preserves: existing logger names, event codes, audit fields, exception values and order-operation identifiers.

- [ ] **Step 1: Write failing focused assertions**

```python
def test_flow_imbalance_log_is_chinese(caplog, market, guards):
    guards.check_flow(market, now=time.time())
    assert "单边流量失衡" in caplog.text
    assert "YES" in caplog.text
```

- [ ] **Step 2: Run focused tests to verify failure**

Run: `.venv\\Scripts\\python.exe -m pytest tests/test_risk.py -k flow -v`

Expected: FAIL because the active text is English.

- [ ] **Step 3: Translate pmbot logger messages**

Replace human-readable message templates module by module. Do not alter log levels, structured values, audit payloads, event constants, or third-party logger configuration.

- [ ] **Step 4: Run focused tests**

Run: `.venv\\Scripts\\python.exe -m pytest tests/test_main.py tests/test_brokers.py tests/test_risk.py -v`

Expected: PASS.

### Task 3: Regression verification

**Files:**
- Test: `tests/test_main.py`, `tests/test_brokers.py`, `tests/test_risk.py`

- [ ] **Step 1: Run the complete suite**

Run: `.venv\\Scripts\\python.exe -m pytest`

Expected: PASS, or report inherited failures separately from this change.

- [ ] **Step 2: Inspect diff**

Run: `rtk git diff --check; rtk git diff --stat`

Expected: no whitespace errors and only the dashboard/log localization files plus tests/docs.
