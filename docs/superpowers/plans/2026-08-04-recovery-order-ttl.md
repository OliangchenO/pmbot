# 补单独立过期时长 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将库存互补补单的 GTD TTL 设为独立的 900 秒，同时保持普通报价 180 秒和所有风险触发的即时替换。

**Architecture:** `Bot._quote_all()` 在已有每-token `audit_context` 标记恢复订单。`LiveBroker.set_quotes()` 根据该标记选择 `recovery_order_ttl_secs` 或普通 `order_ttl_secs`，并将相同 TTL 写入签名与 `RestingOrder.expiration`；现有 `due_for_refresh()` 继续仅依据实际 expiration 判断。

**Tech Stack:** Python 3、pytest、PyYAML、Polymarket CLOB GTD order API。

## Global Constraints

- 新键为 `quoting.recovery_order_ttl_secs: 900`；缺失时回退 `order_ttl_secs`。
- GTD 安全余量 60 秒和续期窗口 90 秒保持不变。
- 仅等于净敞口数量的互补恢复买单使用长 TTL；普通买单、卖出退出单、FAK 与风险规则不变。
- 不修改正在运行的进程、生产配置或交易所订单。

---

### Task 1: 配置和恢复订单 TTL 选择

**Files:**
- Modify: `config.yaml:quoting`
- Modify: `pmbot/brokers.py:629-635,914-1025`
- Test: `tests/test_brokers.py`

**Interfaces:**
- Consumes: `audit_context[token_id]["recovery_order"]: bool`。
- Produces: `LiveBroker._buy_order_ttl(token_id, audit_context) -> int`；`RestingOrder.expiration` 按每笔买单 TTL 生成。

- [ ] **Step 1: Write failing broker tests**

```python
def test_live_recovery_buy_uses_recovery_ttl(monkeypatch, live_broker):
    expiration = capture_batch_expiration(live_broker, recovery_audit=True)
    assert expiration - fixed_now == 900 + GTD_SECURITY_THRESHOLD_SECS

def test_live_normal_buy_uses_normal_ttl(monkeypatch, live_broker):
    expiration = capture_batch_expiration(live_broker, recovery_audit=False)
    assert expiration - fixed_now == 180 + GTD_SECURITY_THRESHOLD_SECS

def test_live_recovery_ttl_falls_back_to_order_ttl(live_config):
    del live_config["quoting"]["recovery_order_ttl_secs"]
    assert LiveBroker.__new__(LiveBroker)._buy_order_ttl(... ) == 180
```

- [ ] **Step 2: Run and verify failure**

Run: `.venv\\Scripts\\python.exe -m pytest tests\\test_brokers.py -q -k "recovery_ttl or normal_buy_uses_normal_ttl"`

Expected: recovery order expiration equals the ordinary TTL before the implementation.

- [ ] **Step 3: Implement TTL resolution and use it consistently**

```python
self.recovery_order_ttl = int(cfg["quoting"].get(
    "recovery_order_ttl_secs", self.order_ttl))

def _buy_order_ttl(self, token_id, audit_context):
    audit = (audit_context or {}).get(token_id, {})
    return self.recovery_order_ttl if audit.get("recovery_order") else self.order_ttl
```

In the batch order loop, compute `ttl = _buy_order_ttl(q.token_id, audit_context)` once, pass it to `_gtd_expiration(ttl)`, and store that exact expiration on `RestingOrder`. Add the config comment documenting the 900-second default and 16-minute exchange lifetime.

- [ ] **Step 4: Run focused tests and verify pass**

Run: `.venv\\Scripts\\python.exe -m pytest tests\\test_brokers.py -q -k "recovery_ttl or normal_buy_uses_normal_ttl"`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add config.yaml pmbot/brokers.py tests/test_brokers.py
git commit -m "fix: extend recovery order ttl"
```

### Task 2: 由报价循环标记恢复订单并回归验证

**Files:**
- Modify: `pmbot/main.py:1679-1717`
- Test: `tests/test_main.py`
- Test: `tests/test_brokers.py`

**Interfaces:**
- Consumes: `unpaired`, `MIN_TAKER_SHARES`、互补 token 和 `final` quotes。
- Produces: 互补恢复 quote 的 audit context 含 `"recovery_order": True`，普通 quote 不含该标记。

- [ ] **Step 1: Write failing quote-loop test**

```python
async def test_quote_all_marks_equal_size_complement_as_recovery_order(...):
    broker.set_position(market, yes=12, no=0)
    await bot._quote_all()
    _, _, audit = broker.set_quote_calls[-1]
    assert audit[market.no_token]["recovery_order"] is True
```

- [ ] **Step 2: Run and verify failure**

Run: `.venv\\Scripts\\python.exe -m pytest tests\\test_main.py -q -k "marks_equal_size_complement"`

Expected: audit context lacks `recovery_order`.

- [ ] **Step 3: Add the audit marker without broadening its scope**

```python
if recovery_quote is not None:
    audit_context[recovery_quote.token_id]["recovery_order"] = True
```

Set it only inside the existing `abs(unpaired) >= MIN_TAKER_SHARES` block after selecting the complement from `final`.

- [ ] **Step 4: Run focused recovery and broker tests**

Run: `.venv\\Scripts\\python.exe -m pytest tests\\test_main.py tests\\test_brokers.py -q -k "recovery or ttl"`

Expected: PASS, including safe-recovery retention and hard-cap replacement tests.

- [ ] **Step 5: Commit**

```bash
git add pmbot/main.py tests/test_main.py tests/test_brokers.py
git commit -m "fix: mark recovery quotes for extended ttl"
```

### Task 3: 完整验证

**Files:**
- Verify: `pmbot/main.py`, `pmbot/brokers.py`, `config.yaml`, `tests/test_main.py`, `tests/test_brokers.py`

**Interfaces:**
- Consumes: 完成的候选排除和 TTL 实现。
- Produces: 可复现的测试结果与既有状态污染失败的单独记录。

- [ ] **Step 1: Check static integrity**

Run: `.venv\\Scripts\\python.exe -m compileall -q pmbot` and `git diff --check`

Expected: both commands exit 0.

- [ ] **Step 2: Run full tests**

Run: `.venv\\Scripts\\python.exe -m pytest -q`

Expected: all tests pass except any pre-existing failures demonstrably caused by shared `data/paper_state.json`; report those separately without changing that file.

- [ ] **Step 3: Commit only verification-related corrections**

```bash
git add pmbot/main.py pmbot/brokers.py config.yaml tests/test_main.py tests/test_brokers.py
git commit -m "test: verify recovery selection and ttl"
```
