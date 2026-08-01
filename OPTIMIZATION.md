# pmbot 优化路线图

> 详细的分阶段目标、验收口径、上线门槛和进度记录见
> [`docs/optimization-roadmap.md`](docs/optimization-roadmap.md)。本文件保留为快速索引；
> 任何优化项目的状态变更应同时更新详细规划中的对应条目。

## 已完成

- [x] **P0.1** `risk.scale_with_equity: true` — 仓位随净值增长同比缩放（config.yaml line 285）
- [x] **P1.2** 补单成功率监控 — `recovery_events` 表 + 7 个事件点 + CLI 展示
  - 事件：skip（7 种 reason）、quote_placed（含 price/pair_cap）、forced_hedge
  - `python -m pmbot.main report` → Recovery stats 表格（成功率、溢价均值/最大值）
  - `python -m pmbot.main performance` → 每市场 Recovery 列（skips/quotes/hedges）
  - paper/live 数据隔离：`metrics_paper.db` / `logs_paper/`

## 待推进

### 待数据积累后评估
- [ ] **P0.2** `recovery_max_loss_cents` 参数调优 — 当前 1.5¢，需跑几天后看 avg/max premium over cap + hedge success rate 再决策

### 下一步可直接推进
- [x] **P0.3** 选市策略优化（`gamma.py`）— 新增 `ranking_mode` 开关（density/capture），capture 模式使用预期捕获奖励排名 + toxicity/band 权重，`min_liquidity: 2000`

### P1（风控 / 稳定性）
- [ ] **P1.1** 每日止损纪律（`risk.daily_loss_limit_usd`）— 评估当前值是否合理
- [x] **P1.3** markout 联动自动退出市场 — `markout_ban_on_trip: true`，一次 trip 直接 ban，持久化到 `data/banned_markets.json`，重启后仍然有效

### P2（效率 / 体验）
- [ ] **P2.1** controller 第三层（reward objective）— 当前只有 capital tiers + toxicity
- [ ] **P2.2** 多策略并行 quote（不同 offset/size 组合）— A/B 测试框架

## 关键参考

- `config.yaml` line 285: `scale_with_equity: true`
- `config.yaml` line 306-311: recovery 参数
  - `recovery_soft_window_minutes: 30`
  - `recovery_max_loss_cents: 1.5`
  - `recovery_escalate_after_minutes: 30`
- metrics 数据在 `data/metrics.db`，paper 模式在 `data/metrics_paper.db`
- `REPORT_reward_selection.md` — 选市分析报告
- 当前 1 个 taker fill 有 unpaired 敞口（Will "Widow's Bay" win Emmys 2026），可关注它的 recovery 表现
