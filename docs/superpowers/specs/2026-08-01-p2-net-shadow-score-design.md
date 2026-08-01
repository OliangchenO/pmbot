# P2.1 净收益影子评分设计

## 目标

在不改变现有 `density/capture` 排名、市场选择、报价、下单、仓位或风险 guard 行为的前提下，为每个已通过现有扫描过滤的市场计算并记录可复现的 `net_shadow_score`。`performance` 报告并列展示旧评分候选与影子评分候选，以及后续可观测表现；连续运行 7 天后再决定是否启动 P2.2。

## 非目标与边界

- 本期不开发 P2.2 的 `size_factor`、市场级冷却、退出或恢复状态机。
- 影子评分不能参与 `gamma.scan()` 的排序、`Bot._select_markets()` 的 sticky 决策、报价价格或数量计算。
- 不新增网络请求；只使用本地 `MetricsStore` 已记录的市场级事实和扫描中已有的市场属性。
- 缺少或不足样本时不使用全局实测值替代市场值；使用明确标识的保守先验，并在报告中说明。
- 不运行 live 模式；paper 与 live 的 metrics 数据库继续由现有配置隔离。

## 评分模型

每个候选市场新增以下只读字段：

- `net_shadow_score`：单位为预期美元/小时，仅用于展示和影子排名。
- `net_shadow_inputs`：评分输入、样本数、数据来源和 `insufficient_sample` 标记。

计算式：

```text
expected_net_hourly =
  calibrated_reward_per_hour * uptime_ratio
  - expected_markout_cost_per_hour
  - expected_recovery_cost_per_hour
  - expected_taker_fee_per_hour
```

- `calibrated_reward_per_hour`：扫描时可得的 `daily_pool / 24` 乘以市场级奖励兑现率；兑现率仅在市场级实际奖励来源和正预估奖励均存在时使用。否则使用配置的保守 `reward_realization_prior`。
- `uptime_ratio`：市场级 in-band uptime 的百分比除以 100；分钟样本不足时使用配置的保守 `uptime_prior`。
- `expected_markout_cost_per_hour`：市场级负向 markout 的绝对值乘以可观测 maker fill 频率；markout 或 fill 样本不足时使用配置的保守 `markout_cost_per_hour_prior`。
- `expected_recovery_cost_per_hour`：市场级 `recovery_events` 中已记录的负 `expected_pair_pnl` 或实际可归因现金流按观察时长年化；无足够样本时使用 `recovery_cost_per_hour_prior`。
- `expected_taker_fee_per_hour`：市场级已记录 taker fee 按观察时长年化；无足够样本时为 0，并保留 `fee_sample_missing` 标记，不虚构费用。

所有分母采用有界观察小时数；无法计算的市场不会产生 NaN 或无穷值。相同指标快照、配置和扫描输入必须产生相同分数与同分时按 `condition_id` 的稳定排序。

## 数据与接口

`MetricsStore` 增加只读的市场级影子输入聚合接口，返回每个 `cid` 的奖励兑现率、uptime、markout、maker fill 频率、recovery 成本、taker fee、观察时长及各自样本数。该接口不写数据库。

`gamma.Market` 增加默认值为零/空的影子评分字段。`gamma.scan()` 在完成当前 eligibility 与旧评分后，接受可选影子输入并填充字段；它仍按既有 `score` 排序并返回既有结果。

`Bot` 在扫描候选集合后从 `MetricsStore` 取得输入，写入一次候选快照；快照含扫描时间、旧排名/评分、影子排名/评分、输入来源与样本不足状态。写入失败只记录 warning，不能影响扫描、选市或交易循环。

`performance` 增加“影子选市”段落：当日旧评分前 N、影子评分前 N、两集合交并情况，以及各市场截至报告时可观测的现金流、库存 MTM、markout、uptime、奖励和样本状态。没有候选快照时明确显示“无影子扫描数据”，不从当前市场倒推历史候选。

## 配置

在 `scanner.net_shadow` 下新增显式配置：最小样本阈值、奖励兑现率先验、uptime 先验、markout/recovery 成本先验和观察窗口小时数。缺省配置应保证功能启用但仅影子记录；不得改变既有 `ranking_mode`、过滤阈值或现有 score。

## 错误处理与可审计性

- 对每个先验值记录 `source=prior` 和原因；真实市场指标记录 `source=market` 与样本数。
- 每个候选快照记录配置版本或完整评分参数，以便之后重算。
- SQLite schema 变更只能追加新表/索引；既有 paper/live 数据库可安全打开和迁移。
- CLI 报告采用确定性的数值格式和排序，避免相同数据的展示顺序漂移。

## 验收与测试

1. 单元测试证明旧 density/capture 排名和 `score` 在启用影子评分后字节级等价；影子字段不影响 `scan()` 返回顺序。
2. 单元测试证明充足样本的评分采用市场数据，样本不足时采用保守先验并带完整标记。
3. 单元测试证明相同输入得到相同影子排序，并以 `condition_id` 打破同分。
4. metrics 测试证明候选快照、市场级输入和 `performance` 影子段落可写入、读取和按日期报告，且 paper/live 数据库不混合。
5. main 测试证明扫描/报告路径只记录影子数据，不调用报价、下单、仓位或市场替换行为。
6. 真实运行验收另计：连续 7 天收集候选快照后，比较影子前 N 与实际所选集合的已记录净收益；在此之前 P2.2 保持未开始。
