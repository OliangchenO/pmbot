# P1 库存恢复 Episode 控制设计与开发计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development to implement this plan task-by-task, then use superpowers:verification-before-completion before claiming success. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将未配平库存恢复从循环式条件判断改为有开始、阶段、损失预算和终止结果的 episode，在限定亏损下缩短方向风险暴露时间。

**Architecture:** 保留现有 passive complement、passive exit 和 forced hedge 执行能力，在其上增加纯函数 `RecoveryDecision` 和持久化的 `RecoveryEpisode`。每个 CID 同时最多一个 episode；决策器比较继续等待、卖出原腿、买入互补腿三条路径的可执行价格与最坏损失，只允许在单 episode 风险预算内自动执行。超预算时暂停新增报价并明确升级为人工持有，不得无限重试吃单。

**Tech Stack:** Python 3.11、asyncio、pytest、SQLite、现有 `Bot` / broker / `MetricsStore`。

## Global Constraints

- 不启动或重启 live 进程，不修改当前 `config.yaml`，不下单或撤单。
- 新控制器默认 `shadow`；live active 必须由用户另行授权。
- 每个市场的恢复任务可独立执行，但必须继续使用现有 per-market lock 和 pending hedge 防重机制。
- 不改变 P0 报价风控和 P2 市场选择接口。

---

## 1. 问题证据与根因判断

- 最新库存快照有 18 个未配平市场、约 496.1051 股单边库存，绝对方向暴露合计约 $151.50；`inventory_usd` 是方向风险指标，不是库存公允价值。
- `forced_hedge_deferred/over_hard_cap` 覆盖 32 个 CID；循环记录共 65,075 行，表明状态持续存在，但该行数不是独立失败次数。
- `book_unavailable_or_wide` 覆盖 26 个 CID；`forced_hedge_filled` 仅 9 条、7 个 CID。
- 4,375 条 `quote_placed` 的平均报价比 pair cap 高约 7.21¢，最大高 38¢；这些是重复决策样本，不是已实现损失。
- 已平衡市场中，exit-only 路径交易损失 -$27.865，paired/merged 路径 -$26.610，说明“退出原腿”和“买互补腿”都可能成为主要损失来源。

根因假设：现逻辑以轮询事件为中心，而非以风险 episode 为中心；`over_hard_cap` 能阻止立即锁定负 pair PnL，却没有显式衡量继续暴露的时间成本，也没有在退出与互补对冲之间做统一、可审计的最小损失选择。

## 2. 范围与非目标

本任务只处理已发生 maker fill 后的单边库存。

不包含：

- 改变正常 maker quote 的价格；
- 改变 scanner 排序；
- 自动突破全局 daily loss / hard kill；
- 强制处理 `manual_hold_cids`；
- 将数据库循环行数当作 episode 数。

## 3. 文件与接口设计

**Files:**

- Create: `pmbot/recovery.py`
- Modify: `pmbot/main.py`
- Modify: `pmbot/metrics.py`
- Modify: `config.debug.yaml`（仅非 live 示例）
- Test: `tests/test_recovery.py`
- Test: `tests/test_main.py`
- Test: `tests/test_metrics.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class RecoveryQuote:
    path: Literal["wait", "buy_complement", "sell_original", "manual_hold"]
    token_id: str | None
    price: float | None
    size: float
    expected_loss_usd: float | None
    reason: str

@dataclass
class RecoveryEpisode:
    cid: str
    started_ts: float
    initial_unpaired: float
    peak_abs_exposure_usd: float
    stage: Literal["passive", "escalated", "terminal"]

def choose_recovery_action(
    *,
    unpaired: float,
    basis: float,
    complement_ask: float | None,
    original_bid: float | None,
    fee_per_share: float,
    elapsed_secs: float,
    max_loss_usd: float,
) -> RecoveryQuote: ...
```

经济口径固定：

```text
buy_complement_loss = max(0, basis + ask + taker_fee - 1) * shares
sell_original_loss  = max(0, basis - bid + taker_fee) * shares
```

选择可执行且预期损失更小的路径；任何自动路径都必须满足 `expected_loss_usd <= max_loss_usd`。缺盘口、缺 basis、超预算时返回 `manual_hold`，同时撤掉新增 maker quote，但不擅自卖出或对冲。

## 4. 开发计划

### Task 1: 纯恢复决策器

- [ ] 新建 `tests/test_recovery.py`，覆盖：保本互补腿优先；卖出原腿损失更小时选择 sell；超过预算返回 manual_hold；缺 basis/盘口不猜测；小于最小成交股数返回 wait。
- [ ] 运行 `rtk pytest tests/test_recovery.py -q`，确认因模块不存在而失败。
- [ ] 新建 `pmbot/recovery.py`，实现上面的 dataclass 和纯函数；不得访问 broker、SQLite 或全局时钟。
- [ ] 再次运行 `rtk pytest tests/test_recovery.py -q`。

### Task 2: Episode 生命周期与持久化

- [ ] 在 `tests/test_metrics.py` 写失败测试：首次非零 unpaired 创建 episode；重启后恢复同一 episode；回到阈值内只关闭一次；记录 peak exposure、duration、chosen path、expected/actual outcome。
- [ ] 在 `pmbot/metrics.py` 增加 `recovery_episodes` 表；使用 `(cid, started_ts)` 唯一键，循环轮询只更新当前行，不新增 episode。
- [ ] 提供 `open_recovery_episode()`、`update_recovery_episode()`、`close_recovery_episode()` 和只读聚合查询。
- [ ] 运行 `rtk pytest tests/test_metrics.py -q`。

### Task 3: 接入现有库存管理

- [ ] 在 `tests/test_main.py` 写失败测试：同一 CID 并发调用只提交一条恢复订单；pending hedge 时不重复提交；manual hold 完全跳过；shadow 只记录建议；active 才调用 broker。
- [ ] 在 `pmbot/main.py::_manage_market_inventory()` 中，把路径选择委托给 `choose_recovery_action()`；保留现有 `_market_lock()`、`has_pending_hedge()`、最小股数和全局 risk action。
- [ ] 对 `buy_complement` 使用 FAK/现有 taker 路径；对 `sell_original` 使用 reduce-only exit 路径；`manual_hold` 必须输出稳定 reason code。
- [ ] 在 `config.debug.yaml` 增加示例：

```yaml
risk:
  recovery_episode_mode: shadow
  recovery_max_loss_usd_per_market: 3.0
  recovery_escalate_after_secs: 180
  recovery_terminal_after_secs: 900
```

- [ ] 运行 `rtk pytest tests/test_recovery.py tests/test_main.py tests/test_metrics.py tests/test_brokers.py -q`。

### Task 4: 历史回放报表

- [ ] 增加只读 CLI 报表，按 episode 输出 duration、峰值方向暴露、路径、预期损失、实际闭环状态；奖励不进入 episode PnL。
- [ ] 对 `data/metrics.db` 做只读回放，将原始循环事件折叠为 episode，输出旧策略与新决策器的对比。
- [ ] 回放缺少原腿 bid 或互补 ask 时标记 `insufficient_evidence`，不得补 0 或猜价格。

## 5. 独立验收标准

### 代码验收

- focused tests 全部通过。
- 单 CID 同时最多一个 open episode、一个 active recovery order、一个 pending hedge。
- 任意自动动作的计算损失不得超过 `recovery_max_loss_usd_per_market`，浮点容差 `1e-9`。
- shadow 模式下 broker 调用次数与改动前完全相同。
- manual hold CID 不创建 episode、不撤单、不补仓、不对冲。

### 策略效果验收

先用历史回放，再用至少 7 个完整 UTC 日 shadow 数据：

- 至少 20 个有完整盘口证据的 recovery episodes；
- 相对旧策略，episode duration 中位数下降至少 50%；
- `abs(exposure_usd) × duration` 的 p95 下降至少 40%；
- 回放/试运行中单 episode 自动实现损失不超过配置预算；
- duplicate recovery order、重复 FAK、pending hedge 重入均为 0；
- `insufficient_evidence` 单独计数，不进入效果提升样本。

若持续时间改善但实际损失恶化，或样本不足，则保持 shadow，不得进入 active。

## 6. 回滚

将 `recovery_episode_mode` 设回 `shadow` 或 `off`，主流程回到现有 `_manage_market_inventory()` 行为。新表仅追加证据，不改写旧 `recovery_events`。
