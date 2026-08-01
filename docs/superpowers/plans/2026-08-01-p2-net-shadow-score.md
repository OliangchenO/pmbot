# P2.1 净收益影子评分 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不改变任何选市或交易行为的前提下，记录市场级净收益影子评分并在 performance 中比较候选集合。

**Architecture:** `MetricsStore` 聚合本地市场级事实、持久化扫描候选快照并生成影子报告数据；`gamma.scan()` 仅填充影子字段，继续使用原始 `score` 排序；`Bot` 在现有 scan/rescan 路径记录快照，CLI 将结果展示为只读报告。

**Tech Stack:** Python 3、SQLite、pytest、现有 Gamma 扫描与 MetricsStore。

## Global Constraints

- `net_shadow_score` 绝不能改变 `gamma.scan()` 返回顺序、`Bot._select_markets()`、报价、下单、仓位或 guard。
- 仅使用已有本地 metrics 数据和扫描中的市场属性；不得增加网络请求。
- 样本不足只能使用显式保守先验，并输出来源、样本数和原因。
- paper/live metrics 数据库与日志继续由现有配置隔离；验证仅使用 paper 或临时数据库。
- P2.2 的 `size_factor`、市场级冷却/退出/恢复状态机不在本计划范围内。

---

### Task 1: 影子评分模型与不改变旧排序

**Files:**
- Modify: `pmbot/gamma.py`
- Modify: `pmbot/strategy.py`
- Modify: `config.yaml`
- Test: `tests/test_gamma.py`
- Test: `tests/test_strategy.py`

**Interfaces:**
- Produces: `Market.net_shadow_score: float` and `Market.net_shadow_inputs: dict[str, object]`.
- Produces: `strategy.compute_net_shadow_score(market, inputs, cfg) -> tuple[float, dict[str, object]]`.
- Consumes: `scanner.net_shadow` with `lookback_hours`, `min_uptime_samples`, `min_markout_samples`, `min_fill_samples`, `min_recovery_samples`, `reward_realization_prior`, `uptime_prior`, `markout_cost_per_hour_prior`, and `recovery_cost_per_hour_prior`.

- [ ] **Step 1: Write failing tests**

```python
def test_shadow_score_uses_market_inputs_without_changing_legacy_order():
    high = _market_with_score("legacy-high", daily_pool=240, score=2.4)
    low = _market_with_score("legacy-low", daily_pool=120, score=1.2)
    ranked = _rank_with_shadow([high, low], {
        "legacy-high": {"uptime_ratio": 0.2, "markout_cost_per_hour": 1.0},
        "legacy-low": {"uptime_ratio": 1.0, "markout_cost_per_hour": 0.0},
    })
    assert [m.condition_id for m in ranked] == ["legacy-high", "legacy-low"]
    assert ranked[1].net_shadow_score > ranked[0].net_shadow_score

def test_shadow_score_marks_insufficient_metric_with_conservative_prior():
    score, inputs = compute_net_shadow_score(market, {}, cfg_with_shadow)
    assert inputs["uptime"]["source"] == "prior"
    assert inputs["insufficient_sample"] is True
    assert score < market.daily_pool / 24
```

- [ ] **Step 2: Run focused tests and verify they fail**

Run: `.venv\\Scripts\\python.exe -m pytest tests/test_gamma.py tests/test_strategy.py -k shadow -v`

Expected: FAIL because the shadow fields and scoring function do not exist.

- [ ] **Step 3: Implement the minimal deterministic scoring API**

```python
def compute_net_shadow_score(market, inputs, cfg):
    # Select market measurements only when each metric meets its threshold;
    # otherwise use the configured prior and annotate its source.
    reward = market.daily_pool / 24 * inputs["reward_realization"]["value"]
    score = reward * inputs["uptime"]["value"]
    score -= inputs["markout_cost_per_hour"]["value"]
    score -= inputs["recovery_cost_per_hour"]["value"]
    score -= inputs["taker_fee_per_hour"]["value"]
    return score, inputs
```

Add `Market` defaults, explicit `scanner.net_shadow` defaults, and populate fields after existing score computation. Preserve `candidates.sort(key=lambda m: m.score, reverse=True)` exactly.

- [ ] **Step 4: Run focused tests and verify they pass**

Run: `.venv\\Scripts\\python.exe -m pytest tests/test_gamma.py tests/test_strategy.py -k shadow -v`

Expected: PASS.

### Task 2: 市场级输入与候选快照账本

**Files:**
- Modify: `pmbot/metrics.py`
- Test: `tests/test_metrics.py`

**Interfaces:**
- Produces: `MetricsStore.net_shadow_inputs(lookback_hours, thresholds) -> dict[str, dict[str, object]]`.
- Produces: `MetricsStore.record_net_shadow_snapshot(markets, scanned_at, config) -> None`.
- Produces: `MetricsStore.net_shadow_report(date) -> dict[str, object]`.
- Consumes: existing `fills`, `markouts`, `uptime`, `reward_samples`, `market_rewards`, and `recovery_events` tables.

- [ ] **Step 1: Write failing tests**

```python
def test_net_shadow_inputs_use_only_one_market_and_mark_missing_samples(tmp_path):
    store = MetricsStore(str(tmp_path / "metrics.db"))
    store.record_fill({"ts": 1_700_000_000, "cid": "A", "market": "A",
                       "side": "YES", "token": "y", "price": 0.45,
                       "size": 10, "taker": False, "exit": False, "fee": 0})
    inputs = store.net_shadow_inputs(lookback_hours=24,
                                     thresholds={"min_markout_samples": 2})
    assert inputs["A"]["maker_fill_count"] == 1
    assert inputs["A"]["markout"]["source"] == "prior"

def test_net_shadow_snapshot_round_trips_ranks_scores_and_input_sources(tmp_path):
    store = MetricsStore(str(tmp_path / "metrics.db"))
    market_a = _market_with_score("A", daily_pool=240, score=2.4)
    market_a.net_shadow_score = 0.1
    market_b = _market_with_score("B", daily_pool=120, score=1.2)
    market_b.net_shadow_score = 0.2
    store.record_net_shadow_snapshot([market_a, market_b], 1_700_000_000, {"top_n": 2})
    report = store.net_shadow_report("2023-11-14")
    assert report["shadow_top"][0]["cid"] == "B"
    assert report["legacy_top"][0]["cid"] == "A"
```

- [ ] **Step 2: Run focused tests and verify they fail**

Run: `.venv\\Scripts\\python.exe -m pytest tests/test_metrics.py -k net_shadow -v`

Expected: FAIL because tables and APIs do not exist.

- [ ] **Step 3: Implement additive SQLite migration and read-only aggregates**

Create append-only snapshot/header and candidate-row tables plus indexes. Aggregate each cid across the bounded lookback; serialize full scoring inputs and scoring config as JSON. A database write error is isolated to the caller and cannot alter existing metrics writes.

- [ ] **Step 4: Run focused tests and verify they pass**

Run: `.venv\\Scripts\\python.exe -m pytest tests/test_metrics.py -k net_shadow -v`

Expected: PASS.

### Task 3: 扫描记录与 performance 展示

**Files:**
- Modify: `pmbot/main.py`
- Modify: `pmbot/metrics.py`
- Test: `tests/test_main.py`
- Test: `tests/test_metrics.py`

**Interfaces:**
- Consumes: `MetricsStore.net_shadow_inputs`, `gamma.scan(cfg, exclude_cids=None, full=False, shadow_inputs=None)`, and `MetricsStore.record_net_shadow_snapshot`.
- Produces: scan/rescan candidate snapshots and a `performance` output section named `shadow_selection`.

- [ ] **Step 1: Write failing tests**

```python
def test_rescan_records_shadow_snapshot_without_changing_selected_markets(tmp_path, monkeypatch):
    bot = _bot(tmp_path)
    # legacy score chooses A; shadow score prefers B
    asyncio.run(bot._rescan(initial=True))
    assert [m.condition_id for m in bot.markets] == ["A"]
    assert bot.metrics.net_shadow_report(today)["shadow_top"][0]["cid"] == "B"

def test_performance_report_has_explicit_no_shadow_data_state(tmp_path):
    report = MetricsStore(str(tmp_path / "metrics.db")).performance_report("2026-08-01")
    assert report["shadow_selection"]["status"] == "no_shadow_scan_data"
```

- [ ] **Step 2: Run focused tests and verify they fail**

Run: `.venv\\Scripts\\python.exe -m pytest tests/test_main.py tests/test_metrics.py -k "shadow and (rescan or performance)" -v`

Expected: FAIL because rescan neither requests inputs nor writes a snapshot and performance lacks the section.

- [ ] **Step 3: Implement passive scan instrumentation and report formatting**

Fetch metrics inputs before invoking scan, pass them only for field population, then write the full candidate snapshot after scanning. Catch and warn on snapshot failures. Extend `cmd_performance` to display legacy/shadow top-N, overlap and later per-market facts; never use the shadow order to select markets.

- [ ] **Step 4: Run focused tests and verify they pass**

Run: `.venv\\Scripts\\python.exe -m pytest tests/test_main.py tests/test_metrics.py -k "shadow and (rescan or performance)" -v`

Expected: PASS.

### Task 4: Regression verification and roadmap update

**Files:**
- Modify: `docs/optimization-roadmap.md`
- Test: `tests/test_gamma.py`
- Test: `tests/test_strategy.py`
- Test: `tests/test_metrics.py`
- Test: `tests/test_main.py`

- [ ] **Step 1: Run all focused P2.1 suites**

Run: `.venv\\Scripts\\python.exe -m pytest tests/test_gamma.py tests/test_strategy.py tests/test_metrics.py tests/test_main.py -v`

Expected: all selected tests PASS.

- [ ] **Step 2: Run the full suite**

Run: `.venv\\Scripts\\python.exe -m pytest -q`

Expected: report all results; distinguish pre-existing unrelated failures from P2.1 regressions.

- [ ] **Step 3: Update roadmap evidence precisely**

Mark only the implemented P2.1 code items complete. Keep its 7-day real-data evaluation unchecked and all P2.2 items unchecked; include test commands/results in the progress log.
