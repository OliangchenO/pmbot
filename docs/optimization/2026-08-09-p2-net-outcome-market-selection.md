# P2 净结果选市与闭环账本设计及开发计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development to implement this plan task-by-task, then use superpowers:verification-before-completion before claiming success. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立可核验的单市场交易闭环，在证据充分时用净结果而非奖励密度单独排序市场，同时保持旧排序为默认和可即时回滚路径。

**Architecture:** 先从 fills、exits、merges、持有的完整 YES/NO pairs、未配平库存和 market rewards 生成 `MarketOutcome`，明确区分 realized、paired-but-unmerged、unpaired-MTM 和 incomplete。然后扩展现有 P2.1 shadow score 输入与对照报表；只有达到样本和净优势门槛后，才允许通过显式配置启用 `net_outcome` 排序。该模块只决定候选顺序，不改变 quote、size、风控或恢复。

**Tech Stack:** Python 3.11、pytest、SQLite、现有 `gamma.scan()` / `compute_net_shadow_score()` / `MetricsStore`。

## Global Constraints

- 不启动或重启 live 进程，不修改当前 `config.yaml`，不下单或撤单。
- 旧 API、旧表和 `ranking_mode` 默认行为保持不变。
- 账户级 realized rewards 不得按市场摊派；只有 `market_rewards` 的明确 CID 数据才能进入市场净结果。
- `inventory_usd` 是方向风险，不是库存市值，禁止把它直接加到现金 PnL。
- P2.1 shadow 数据不能自动视为 P2.2 上线许可。

---

## 1. 问题证据与根因判断

主账本显示：

- 129 笔成交：正常 maker 买入现金流约 $1,224.70，taker 买入约 $447.43，退出卖出约 $242.43，merge 950 pairs，数据库费用为 $0。
- 直接现金公式 `merges + sells - buys - fees` 得到约 -$479.70，但其中包含未退出的库存，不能解释为已实现亏损。
- 对 YES/NO 股数已平衡的 20 个市场，把尚未 merge 的完整 pair 按 $1/pair 计入后，交易经济损益为 -$54.475；其中负贡献 -$57.805、正贡献仅 $3.33。
- 这 20 个市场对应的明确 market rewards 约 $15.929，净结果仍约 -$38.546。账户级 rewards 总额 $88.177 必须单列。
- 典型完整 pair：Astra Aug-31 以 NO 0.36 与 YES 0.72 各 81 股组对，pair cost 1.08，交易损失 $6.48；Sergio Ramos 市场 pair cost 1.11，43 pairs 损失 $4.73。
- `gamma.scan()` 当前仍只按 legacy `score` 排序；`net_shadow_score` 已计算和记录，但明确不参与选择。
- 现有数据只有 8 个左右完整 UTC 日，且部分候选缺少 markout、fee、recovery 和完整 fill-inventory closure，不能直接启用 P2.2。

根因判断：当前选市首先优化奖励密度/捕获量，交易损失和恢复成本只存在于被动 shadow 字段；同时账本把“paired-but-unmerged”和“unpaired”混在现金缺口里，导致无法对候选市场形成稳定、可验证的净收益标签。

## 2. 范围与非目标

本任务只负责结果核算、shadow 对照和候选排序。

不包含：

- 改变 quote price、quote size；
- 改变 P0 风控阈值；
- 改变 P1 恢复动作；
- 将账户奖励按持仓、成交量或 uptime 猜测分配；
- 未满足门槛时自动切换 live 排序。

## 3. 文件与接口设计

**Files:**

- Create: `pmbot/outcomes.py`
- Modify: `pmbot/metrics.py`
- Modify: `pmbot/strategy.py`
- Modify: `pmbot/gamma.py`
- Modify: `pmbot/main.py`
- Modify: `config.debug.yaml`（仅非 live 示例）
- Test: `tests/test_outcomes.py`
- Test: `tests/test_metrics.py`
- Test: `tests/test_strategy.py`
- Test: `tests/test_gamma.py`
- Test: `tests/test_main.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class MarketOutcome:
    cid: str
    state: Literal[
        "realized", "paired_unmerged", "unpaired_marked", "incomplete"
    ]
    buy_cash_usd: float
    sell_cash_usd: float
    merge_cash_usd: float
    fees_usd: float
    held_pairs: float
    unpaired_shares: float
    unpaired_mtm_usd: float | None
    trading_pnl_usd: float | None
    market_reward_usd: float | None
    net_outcome_usd: float | None
    evidence_flags: tuple[str, ...]

def build_market_outcomes(
    conn: sqlite3.Connection,
    start_ts: float,
    end_ts: float,
) -> list[MarketOutcome]: ...
```

核算公式固定：

```text
cash_pnl = merges + exits - buys - fees
paired_value = max(min(net_yes, net_no) - merged_pairs, 0) * $1
trading_pnl = cash_pnl + paired_value + unpaired_mtm
net_outcome = trading_pnl + explicit_market_reward
```

若 unpaired 不为零且没有同时间附近的可验证 mid，则 `unpaired_mtm_usd`、`trading_pnl_usd`、`net_outcome_usd` 均为 `None`，状态为 `incomplete`。不能用 `inventory_snapshots.exposure_usd` 代替市值。

## 4. 开发计划

### Task 1: 闭环核算器

- [ ] 新建 `tests/test_outcomes.py`，构造并验证四类账本：已 merge、完整 pair 未 merge、退出闭环、未配平且缺 mid；验证账户 reward 不会进入市场 outcome。
- [ ] 运行 `rtk pytest tests/test_outcomes.py -q`，确认因模块不存在而失败。
- [ ] 新建 `pmbot/outcomes.py`，实现 `MarketOutcome` 和 `build_market_outcomes()`；SQL 连接由调用方传入，函数只读。
- [ ] 增加 Astra 示例测试：81×(1-0.36-0.72) = -$6.48。
- [ ] 运行 `rtk pytest tests/test_outcomes.py -q`。

### Task 2: 完整性与净结果报表

- [ ] 在 `tests/test_metrics.py` 写失败测试：报表分列 cash、paired value、unpaired MTM、trading PnL、market reward、account reward；incomplete 不参与均值和排名。
- [ ] 在 `pmbot/metrics.py` 增加 outcome 查询入口和 completeness 汇总，不改写历史 fills/merges/rewards。
- [ ] 在 `pmbot/main.py` 增加只读 `outcomes --date/--days` 命令，输出 `complete_count`、`incomplete_count`、缺失原因。
- [ ] 运行 `rtk pytest tests/test_metrics.py tests/test_main.py tests/test_outcomes.py -q`。

### Task 3: 扩展 shadow score 输入

- [ ] 在 `tests/test_strategy.py` 写失败测试：完整市场使用每小时净结果；incomplete 或样本不足继续使用保守 prior；费用、恢复成本和奖励只计一次。
- [ ] 扩展 `compute_net_shadow_score()` 的输入审计，新增 `closed_cycle_net_per_hour` 和 `closed_cycle_samples`，保留现有返回签名。
- [ ] 在 `tests/test_gamma.py` 验证默认 legacy 排序逐项不变，shadow 排名只记录、不选择。
- [ ] 运行 `rtk pytest tests/test_strategy.py tests/test_gamma.py -q`。

### Task 4: 受门槛控制的独立排序模式

- [ ] 在 `tests/test_gamma.py` 写失败测试：只有 `selection_mode=net_outcome` 且 gate 状态为 passed 时按 `net_shadow_score` 排序；否则回退 legacy，并记录稳定 reason code。
- [ ] 在 `pmbot/gamma.py` 增加显式选择分支，不修改 cheap eligibility filters；held market、manual hold、unpaired exclusion 等现有后续规则保持不变。
- [ ] 在 `config.debug.yaml` 增加示例：

```yaml
scanner:
  selection_mode: legacy
  net_outcome_gate:
    min_utc_days: 14
    min_closed_cycles: 30
    min_complete_ratio: 0.90
    min_advantage_usd_per_hour: 0.0
```

- [ ] 运行 `rtk pytest tests/test_gamma.py tests/test_main.py tests/test_metrics.py tests/test_outcomes.py -q`。

## 5. 独立验收标准

### 代码验收

- focused tests 全部通过。
- 使用本次主账本时，20 个股数已平衡市场的交易经济损益复算为 -$54.475，允许误差 $0.01；Astra 示例为 -$6.48，允许误差 $0.01。
- account rewards 始终独立展示，任何市场结果中都不存在账户奖励摊派。
- `selection_mode=legacy` 时候选排序和改动前完全一致。
- incomplete 市场不参与净排序；报表必须显示其数量和缺失字段。

### P2.2 效果门槛

必须同时满足才可单独申请把 `selection_mode` 改为 `net_outcome`：

- 至少 14 个完整 UTC 日；
- 至少 30 个 closed cycles，且 outcome 完整率至少 90%；
- 每个进入 Top-N 的候选至少有 3 个 closed cycles，或明确使用保守 prior；
- 在相同扫描时点、相同 eligibility 集合上，shadow Top-N 的净结果/小时相对 legacy Top-N 为正，且 bootstrap 95% 置信区间下界大于 0；
- shadow Top-N 的强制对冲率、300 秒负 markout 率、最大单市场暴露均不劣于 legacy；
- 连续 3 个 UTC 日没有 `fill_without_inventory_closure`、重复 fill 或 reward double-count。

当前数据不满足上述门槛，因此本文档交付时的正确状态是“设计可实施，P2.2 未获准”。

## 6. 回滚

将 `selection_mode` 设回 `legacy` 即恢复旧排序；outcome 表和 shadow 快照继续只读保留。回滚不改变报价、库存、订单或历史账本。
