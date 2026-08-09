# P0 逆向选择前置防护设计与开发计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development to implement this plan task-by-task, then use superpowers:verification-before-completion before claiming success. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不改动市场选择和库存恢复逻辑的前提下，减少 maker 成交后 30 秒与 300 秒的负 markout，并在有毒流量出现时及时扩大或撤下危险侧报价。

**Architecture:** 将现有的流量失衡、短时价格移动和历史 markout 汇总为一个纯函数 `QuoteRiskDecision`。它只决定当前市场两侧报价是 `allow`、`widen` 还是 `pull`；订单生命周期仍由 `Bot` 和 broker 负责，库存恢复订单不受此模块影响。新策略先以 shadow 模式记录决策，达到效果门槛后才允许显式开启。

**Tech Stack:** Python 3.11、pytest、SQLite、现有 `MarketGuards` / `MarkoutTracker` / `Bot`。

## Global Constraints

- 不启动或重启 live 进程，不修改当前 `config.yaml`，不下单或撤单。
- 默认行为必须保持现状；新增开关默认 `shadow`，不能默认影响真实报价。
- `ORDER_PLACED`、成交、退出、merge、奖励必须分开统计。
- 只修改本任务列出的文件，不顺带重构库存恢复或市场选择。

---

## 1. 问题证据与根因判断

审计窗口为现存主账本 `data/metrics.db`，成交时间覆盖 2026-07-30 至 2026-08-08 UTC：

- 300 秒 markout 共 71 个样本，其中 47 个为负，27 个不高于 -3¢；均值为 -2.5915¢，最差为 -18.35¢。
- 30 秒 markout 共 76 个样本，其中 38 个为负，22 个不高于 -3¢；均值为 -1.6349¢。
- 数据库记录了 93 次 `side_guard_pull`、44 次 `queue_depth_pull`、26 次 `market_guard_pull`，说明保护已经触发，但仍未阻止大部分负 markout。
- 现配置要求流量达到 200 股后才使用 flow imbalance；相对于 20–100 股常见单笔成交，这个信号可能在第一次被扫后才形成。
- 已平衡市场的交易经济损益为 -$54.475；其中 exit-only 路径 -$27.865，paired/merged 路径 -$26.610。奖励必须另列，不能用于掩盖成交质量。

根因假设：现有保护主要是成交后反应，且 flow 最小样本门槛偏迟；历史毒性、短时价格变化和实时流向没有形成统一、带迟滞的报价准入决策，导致危险侧仍能在信号形成前继续排队。

## 2. 范围与非目标

本任务只负责 maker 报价准入和宽度调整。

不包含：

- 修改 `gamma.scan()` 的市场排序；
- 修改 `_manage_market_inventory()`、退出单或强制对冲；
- 修改资金规模、每日亏损限额；
- 自动修改 live 配置。

## 3. 文件与接口设计

**Files:**

- Modify: `pmbot/risk.py`
- Modify: `pmbot/main.py`
- Modify: `pmbot/metrics.py`
- Modify: `config.debug.yaml`（仅给出非 live 示例）
- Test: `tests/test_risk.py`
- Test: `tests/test_main.py`
- Test: `tests/test_metrics.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class QuoteRiskDecision:
    yes_action: Literal["allow", "widen", "pull"]
    no_action: Literal["allow", "widen", "pull"]
    yes_widen: float
    no_widen: float
    reason: str
    score: float

class MarketGuards:
    def quote_risk_decision(
        self,
        market: Market,
        now: float,
        markout_avg: float | None,
        markout_samples: int,
    ) -> QuoteRiskDecision: ...

class MetricsStore:
    def record_quote_risk_decision(
        self,
        cid: str,
        mode: str,
        decision: QuoteRiskDecision,
        ts: float | None = None,
    ) -> None: ...
```

决策优先级固定为 `pull > widen > allow`。`pull` 只针对危险侧；市场级价格跳变仍沿用现有整市场撤单。使用两个阈值形成迟滞：进入 `pull` 的阈值高于退出 `pull` 的阈值，避免每轮反复挂撤。

## 4. 开发计划

### Task 1: 纯决策器与迟滞状态

- [ ] 在 `tests/test_risk.py` 写失败测试：低样本时保持 `allow`；达到流量门槛时只扩大危险侧；超过 pull 阈值时只撤危险侧；信号下降但未到恢复阈值时保持 `pull`；冷却结束后恢复。
- [ ] 运行 `rtk pytest tests/test_risk.py -q`，确认新增测试因接口不存在而失败。
- [ ] 在 `pmbot/risk.py` 增加 `QuoteRiskDecision` 和 `quote_risk_decision()`；只复用现有 `_flow_stats()`、mid 变化和 `market_avg()`，不新增外部依赖。
- [ ] 再次运行 `rtk pytest tests/test_risk.py -q`，确认通过。

### Task 2: Bot 接线与 shadow/active 双模式

- [ ] 在 `tests/test_main.py` 写失败测试：`shadow` 只记录、不改变 quote；`active` 才执行 widen/pull；恢复订单带 `path=inventory_recovery` 时不经过该决策器。
- [ ] 在 `pmbot/main.py` 的正常报价生成后、`_set_quotes_locked()` 前应用决策；保持已有 market lock，禁止新增竞态路径。
- [ ] 在 `config.debug.yaml` 增加示例配置：

```yaml
guards:
  quote_risk_mode: shadow
  quote_risk_widen_score: 0.60
  quote_risk_pull_score: 0.85
  quote_risk_resume_score: 0.45
```

- [ ] 运行 `rtk pytest tests/test_main.py tests/test_risk.py -q`。

### Task 3: 可回放证据

- [ ] 在 `tests/test_metrics.py` 写失败测试，验证决策时间、CID、模式、两侧 action、score、reason 可持久化和按 UTC 窗口查询。
- [ ] 在 `pmbot/metrics.py` 增加 `quote_risk_decisions` 表和读写方法；SQLite 写失败只能告警，不能中止报价循环。
- [ ] 增加只读报表字段：shadow 被拦截样本数、对应 30/300 秒 markout、允许组 markout、动作分布。
- [ ] 运行 `rtk pytest tests/test_metrics.py tests/test_main.py tests/test_risk.py -q`。

## 5. 独立验收标准

### 代码验收

- 上述 focused tests 全部通过；现有 `tests/test_strategy.py` 通过。
- `quote_risk_mode=shadow` 时，给同一盘口输入，输出报价与改动前逐项相同。
- `active` 模式下，危险侧 pull 从事件到 broker 撤单调用不超过一个事件循环 tick，且不会撤掉库存恢复订单。
- 同一稳定信号 10 次轮询最多产生一次动作变更，证明迟滞有效。

### 策略效果验收

先运行 shadow，满足全部门槛才允许单独申请 active：

- 至少 7 个完整 UTC 日、至少 100 个可配对的报价决策样本；
- 被标记为 `pull` 的样本，其 300 秒负 markout 命中率至少 60%；
- `allow` 组 300 秒平均 markout 比本次基线 -2.5915¢ 改善至少 1.0¢；
- 非负 markout 样本被误拦比例不高于 35%；
- active 小流量观察期内，maker fills 不得因重复挂撤产生重复成交或 orphan order。

任一效果门槛未满足则保持 shadow，本任务仍可按“功能完成、效果未通过”独立验收，不得宣称已降低 live 亏损。

## 6. 回滚

将 `quote_risk_mode` 设回 `shadow` 或 `off` 即回滚行为；保留决策表用于复盘。回滚不触碰库存、退出单、市场选择或历史数据。
