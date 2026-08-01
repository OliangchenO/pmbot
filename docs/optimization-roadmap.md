# pmbot 收益优化规划

> 快速索引：[根目录优化路线图](../OPTIMIZATION.md)

## 目标与边界

目标不是提高挂单数或预估奖励，而是建立并持续扩大**可验证的净收益**：

```text
净收益 = 已实现奖励 + 合并/退出现金流 - 买入成本 - 对冲成本 - 手续费 + 未配对库存市值
```

- 本规划先以观测、影子计算和 paper 验证为主；任何 live 参数或下单逻辑变更须单独确认。
- 不把 `estimated reward` 当作已实现收益，也不把挂单、信号或未配对成交当作盈利。
- 在“单市场净 edge”为正以前，不提升仓位档位、不增加市场数。

## 当前基线（2026-08-01）

| 指标 | 当前观察值 | 解释 |
| --- | ---: | --- |
| 已实现奖励 | $15.42 | 已入账奖励，不含估算值 |
| 交易现金流 | -$78.04 | 合并、成交和手续费的现金流总和 |
| 交易 MTM | -$44.02 | 现金流加当前库存市值后的暂估值 |
| 长周期 markout | -2.41¢ | 5 分钟样本均值，负值代表逆向选择 |
| 当前日 in-band uptime | 88.5% | 有资格赚取奖励的挂单时长比例 |

样本量仍有限，以上仅作为优先级依据，不能据此宣称策略长期亏损或盈利。

## 状态约定

- `[ ]` 未开始
- `[-]` 正在进行
- `[x]` 已完成并通过该条目的验收
- `[!]` 已发现问题，暂停后续扩大风险

## P0：先建立可决策的收益事实

### P0.4 单市场净收益账本

**目的：** 让每个 `condition_id` 都能回答“赚/亏多少、来自哪里、证据是什么”。

- [x] 为 fill、merge、hedge、exit 和 reward 建立可按 `cid` 汇总的经济归因。
- [x] 已在 `performance` 增加单市场买入成本、合并收入、交易现金流、预估奖励和预估净收益。
- [x] 仅使用可由本地账本精确核对的成交、合并、退出、手续费与预估奖励；未做市场级已实现奖励或 MTM 的猜测分摊。
- [x] 每分钟记录未配对库存的数量、成本基准、敞口估值和观测状态（`unpaired` / `flat`）。
- [x] 记录最近一次 `fill` / `exit` / `hedge` / `merge` 库存事件，并在 `performance` 展示。
- [x] 将库存终态区分为 `paired` / `exit` / `hedged` / `unresolved`：仅在 `flat` 快照之后，有足量 merge、同侧 reduce-only exit 或 forced complement hedge 时标记为对应终态。
- [x] 已联结事件与后续快照形成可证明终态；证据不足、没有先前裸仓快照或当前仍有裸仓时一律保持 `unresolved`。
- [x] 新增带来源的市场级已实现奖励账本；`performance` 仅在存在显式市场来源时显示已实现奖励和兑现率，否则明确标为“仅账户汇总”或“无数据”。
- [x] 接入可验证的官方逐市场奖励明细；通过官方 `get_earnings_for_user_for_day()` 的 `condition_id` 行写入 `market_rewards`，账户日汇总不做比例分摊。
- [x] 在 `performance` 中增加未配对库存 MTM、含该 MTM 的预估净收益、已完成对数、可归因的每对现金流和交易事件样本数。
- [x] 为跨日库存设置保守归因规则：从日初前的净 YES/NO token 和已 merge 数量计算 carry-in 上界；当日 merge/exit 可能消耗旧库存时标为 `mixed_with_carry_in`，不归因到当日选市现金流。

**涉及文件：** `pmbot/metrics.py`、`pmbot/brokers.py`、`pmbot/main.py`、`tests/test_metrics.py`、`tests/test_brokers.py`。

**验收：** 任意一笔 fill 可追溯至最终状态；报告能逐市场核对总额与全局现金流一致；paper 与 live 数据库严格隔离。

### P0.5 奖励预估校准与影子报表

**目的：** 把“预估奖励”转换为有历史误差边界的运营信号。

- [x] 按市场和日期计算 `realized_reward / estimated_reward` 校准系数；仅在市场级实际奖励有显式来源、且预估值大于零时计算。
- [x] 样本不足、缺少市场奖励来源或缺少预估时明确显示 `unattributed` / `missing_estimate`，不把系数默认为 1。
- [x] 在校准报告中同时展示奖励兑现率、in-band uptime 和已记录的 recovery 跳过原因。
- [x] 将普通 quote guard 中断按市场、日期和可观察动作原因写入结构化指标；无事件时仍明确标记为 `guard unrecorded`，不从日志猜测未发生的中断。
- [x] 新增只读 `reward-calibration --days N` 影子报表；不改变选市和下单行为。

**验收：** 每日可解释“奖励低于预估”是兑现率、低 uptime，还是市场退出；影子数据连续积累至少 7 天。实现已完成，连续 7 天属于后续真实运行数据验收，未到期前不据此调整策略或扩大仓位。

## P1：降低当前最确定的损耗

### P1.1 补仓与强平的单对经济约束

**目的：** 防止为配平库存而支付超过可由奖励覆盖的成本。

- [x] 对每个未配对库存计算互补 token 的 break-even 买价，包含已知 taker fee。
- [x] 将软回收窗口的额外容忍值记录为“预期单对损失”，而非仅记录报价溢价。
- [x] 软窗口内的被动补仓不超过市场级硬上限；超时后使用 `market_center_recovery`，按盘口中枢策略价挂等量互补单，即使普通 `mid_range` 过滤不产出报价。
- [x] 已记录回收事件、预期单对 PnL、软窗口预期损失与最终库存终态；`recovery_max_loss_cents` 参数值须在真实样本积累后再调整。

**涉及文件：** `pmbot/main.py::_inventory_recovery_quotes`、`pmbot/brokers.py`、`pmbot/metrics.py`、`tests/test_main.py`。

**验收：** 每个 recovery event 都能关联到最终 PnL；超时恢复单可复核其盘口中枢、策略价与预期配对盈亏，且方向/数量只减少现有敞口；任何主动 hedge 均可验证 `成本 + fee <= 1.00` 或有显式例外原因。

### P1.2 保留并评估每日止损纪律

**目的：** 让止损暂停新风险，同时不放弃已有库存的安全管理。

- [x] 对每次 `PAUSE_DAY` 记录触发原因、原始/平滑 equity、日损失、库存规模及恢复时间。
- [x] 用平滑后的 equity 作为止损观测值，区分薄盘口 MTM 噪声与真实损失。
- [x] 已积累评估 `daily_loss_limit_usd` 所需的止损事实；参数值须在 P0 数据样本足够后另行确认，不作为收益调节按钮。

**验收：** 暂停期间不新增普通双边报价，已有库存仍继续执行受成本限制的回收；同一暂停状态不会重复 `cancel_all()`。

## P2：让选市和仓位服从预期净收益

### P2.1 净收益影子评分（先不接管实盘）

**目的：** 替代仅按 capture/density 的单维选市。

```text
expected_net_hourly =
  校准后预估奖励/小时 × 近期 in-band uptime
  - 预期 markout 成本/小时
  - 预期回收与对冲成本/小时
  - 预期 taker fee
```

- [x] 在 `gamma.py` 保留现有 `density/capture` 排名，新增只记录的 `net_shadow_score`。
- [x] 使用市场级而非全局 markout、uptime、奖励兑现率和回收成本；样本不足市场使用保守先验。
- [x] 在 `performance` 中并列显示旧评分与影子评分的候选集合、实际后续表现。
- [ ] 连续 7 天评估影子前 N 与实际已选市场的净收益差异。

**涉及文件：** `pmbot/gamma.py`、`pmbot/strategy.py`、`pmbot/metrics.py`、`pmbot/main.py`、`tests/test_gamma.py`、`tests/test_strategy.py`。

**验收：** 影子评分不改变行为；有足够样本时可复现相同输入得到相同排序；报告清楚标出样本不足市场。

### P2.2 控制器第三层：市场级降仓/退出

**目的：** 解决现有控制器只处理资本档位和全局毒性、会掩盖局部亏损市场的问题。

- [ ] 以市场级净收益、markout、uptime 和样本数计算 `size_factor`。
- [ ] 连续两个观察窗口负净收益时从 1.0 降至 0.5；仍为负才进入冷却，不一次性因偶然样本退出。
- [ ] 正净收益且高 uptime 的市场保持 sticky，避免为短期 scanner 排名丢失队列优先级。
- [ ] 将控制器决定和输入指标写入日志/metrics，便于复盘。

**验收：** 仅在满足最小样本量后生效；降仓、恢复、退出都可从报告复现原因；风险 guard 的即时退出优先级高于收益控制器。

## P3：报价参数实验和规模门禁

### P3.1 单变量报价实验

- [ ] 建立时间分桶 A/B 实验：一次只变更 `offset_frac_of_max_spread`、size factor 或 `requote_move_cents` 之一。
- [ ] 每组记录净收益/对、奖励/有效挂单分钟、markout、回收成本、成交率。
- [ ] 同一市场不同时运行两套实盘报价，避免订单竞争和归因污染。
- [ ] 每项实验至少覆盖一个完整奖励结算周期；样本不足仅标记“无结论”。

**验收：** 实验数据可导出、可复算；胜出参数必须在净收益而非单一奖励或成交数上优于基线。

### P3.2 扩仓门禁

- [ ] 最近 7 天校准后的奖励兑现率稳定，且没有未解决对账异常。
- [ ] 最近 50 个 maker fills 或等价的市场小时样本，平均净 edge 为正。
- [ ] 回收/强平成本不超过已实现奖励的预设比例（先报告，确认后再固定阈值）。
- [ ] 满足条件后，每次只提升一个变量：单市规模或市场数量二选一。

**验收：** 每次扩仓有对应基线报告、批准记录和回滚参数；不满足任一门禁则维持或回退当前档位。

## 推荐执行顺序

1. P0.4：先补齐市场级经济账本。
2. P0.5：积累并展示奖励兑现率与影子数据。
3. P1.1：基于真实单对损益收紧补仓经济边界。
4. P2.1：运行净收益影子评分至少 7 天。
5. P2.2：确认评分有效后接入市场级降仓/退出。
6. P3.1：逐一做报价参数实验。
7. P3.2：只有净 edge 已验证为正时才扩大规模。

## 每周复盘模板

```markdown
### YYYY-MM-DD 周复盘

- 本周运行时长 / in-band uptime：
- 已实现奖励 / 预估奖励 / 兑现率：
- 交易现金流 / 库存 MTM / 净收益：
- Top 3 市场净收益与样本数：
- Bottom 3 市场净收益、markout、回收成本：
- 未解决库存与对账异常：
- 本周实验结论：
- 下周仅推进的一个优化项目：
- 是否满足扩仓门禁：是 / 否；证据：
```

## 进度日志

| 日期 | 项目 | 状态 | 证据或报告链接 | 结论 / 下一步 |
| --- | --- | --- | --- | --- |
| 2026-08-01 | 规划建立 | [x] | `metrics.db`、`report`、`performance` | 当前先做 P0.4，不扩大仓位 |
| 2026-08-01 | P0.4 市场级现金流、奖励、库存快照、事件、终态与跨日归因 | [x] | `performance_report()`、`market_rewards`、`inventory_snapshots`、`inventory_events`、`tests/test_metrics.py`、`tests/test_main.py` | 已完成可核对现金流、只读库存采样、终态归因、官方逐市场奖励导入、未配对 MTM、每对现金流与跨日库存上界；真实奖励兑现率仍需后续运行积累 |
| 2026-08-01 | P0.5 奖励预估校准影子报表 | [x] | `reward_calibration_report()`、`pmbot.main reward-calibration --days 7`、`tests/test_metrics.py` | 已可逐市场逐日输出兑现率或无归因状态；没有官方市场级实际奖励时不能用于策略调整 |
| 2026-08-01 | P0.5 校准可解释性 | [x] | `reward_calibration_report()`、`uptime`、`recovery_events`、`guard_events`、`tests/test_metrics.py` | 已显示 in-band uptime、recovery 跳过原因和普通 guard 拉单事件；事件原因仅记录实际发生的拉单动作 |
| 2026-08-01 | P1 补单/强平经济约束与日止损审计 | [x] | `recovery_events`、`pause_day_events`、`logs/pmbot.YYYY-MM-DD.log`、`tests/test_main.py`、`tests/test_strategy.py`、`tests/test_metrics.py` | 软窗口被动补单不超过含手续费硬上限；超时后按盘口中枢策略价补等量互补仓位，可记录超保本的预期损失但不扩大同向敞口；强平、拒绝、成交与日止损触发/恢复均有可复核依据。真实样本积累前不调整风险参数。 |
| 2026-08-01 | P2.1 净收益影子评分 | [-] | `net_shadow_scans`、`net_shadow_candidates`、`pmbot.strategy.compute_net_shadow_score()`、`pmbot.main performance`、P2.1 定向 pytest | 已记录旧评分与影子评分、市场级输入来源和保守先验；不参与选市、报价、下单或仓位。连续 7 天真实候选/后续收益比较仍待数据积累，P2.2 保持未开始。 |
