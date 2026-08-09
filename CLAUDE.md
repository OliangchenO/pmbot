# PMBot — Polymarket Market Maker

## 项目概述

基于 Python 的 Polymarket 自动做市机器人，在预测市场中进行双边报价赚取流动性奖励。支持 Paper（模拟）和 Live（实盘）两种模式。

## 启动命令

```bash
python -m pmbot.main run          # 启动做市（根据 config.yaml 中的 mode 决定 paper/live）
python -m pmbot.main scan         # 扫描当前最优奖励市场
python -m pmbot.main report       # 当日 PnL 报告
python -m pmbot.main performance  # 按市场绩效明细
```

## 核心模块

- **main.py** — 编排器 Bot 类，包含完整的报价循环、补单逻辑、风险管理、仓位管理
- **strategy.py** — 报价引擎：计算双边买入报价（YES bid + NO bid），含库存偏斜、自适应偏移
- **brokers.py** — PaperBroker（模拟）和 LiveBroker（实盘），含订单管理、持仓跟踪、持久化
- **books.py** — 订单簿跟踪（WebSocket 订阅 Polymarket CLOB）
- **gamma.py** — 市场扫描与数据结构
- **risk.py** — 风险管理：MarketGuards（熔断/冷却）、MarkoutTracker（成交滑点追踪）、RiskManager（日亏损上限等）
- **controller.py** — AdaptiveController：根据盈亏自动调整 offset、flatten 参数
- **metrics.py** — SQLite 持久化：成交记录、PnL、markout、uptime 采样
- **userfeed.py** — WebSocket 用户成交推送

## 配置文件

- `config.yaml` — 主配置（模式、扫描参数、报价参数、风控参数）
- `config_paper.yaml` — Paper 模式专用配置

## 持久化文件

- `data/metrics.db` — 成交/收益/绩效 SQLite 数据库
- `data/live_state.json` — LiveBroker 持久化 `unpaired_since`（重启后恢复补单窗口计时）
- `data/paper_state.json` — PaperBroker 持久化状态
- `logs/pmbot.YYYY-MM-DD.log` — 按日滚动的运行时日志（北京时间）

## 补单（Recovery）逻辑

当市场出现单向库存（unpaired inventory，如持有 YES 40 股但没有对应 NO）时触发：

### 三个阶段
1. **Phase 1 (软窗口补单)** — 在 `recovery_soft_window_minutes` 内，互补报价可在 pair-cap（保本价）之上额外浮动 `recovery_max_loss_cents`，提高成交概率
2. **Phase 1 (硬窗口)** — 软窗口过期后，互补报价严格限制在 pair-cap 内
3. **Phase 2 (公允价补单)** — 超过 `recovery_escalate_after_minutes` 后，以公允价（无 pair-cap 限制）挂互补单，接受小额亏损加速出清

### 阶段判断
- 使用 `broker.last_fill_ts(cid)` 而非内存 `_over_since`，保障重启后正确判断
- `last_fill_ts` 只返回真实成交时间，不包含 Data API 推测的仓位变动
- `unpaired_since` 持久化到 `live_state.json` / `paper_state.json`

### 强制对冲
- 当敞口超过阈值且等待时间超过 `flatten_after_secs`，以保本价 taker 买入互补 token 配平
- 对冲被推迟的情况记录在日志中（`强制对冲推迟`），常见原因：ask 价格超过 pair-cap

## 关键 Bug 修复记录

1. **Phase 2 重启后不生效** → 使用 `last_fill_ts()` 替代 `_over_since`，时间戳持久化
2. **Phase 2 价格仍是 pair-cap 价** → `_escalated_recovery_quotes` 需要使用 `normal_desired`（原始公允价）而非已被 pair-cap 截断的 `desired`
3. **补单数量超量** → 改为直接用 `abs(unpaired)` 而非 `max(q.size, abs(unpaired))`
4. **重复报价误报** → `check_logs.py` 中 `cancelled_counts` key 顺序错误（side/price/size vs price/size/size），已修复
5. **PaperBroker.last_fill_ts** → 补充排除 Data API 推测成交（无 price 字段的 entry）

## 日志监控

```bash
python scripts/check_logs.py -m 10   # 检查最近 10 分钟日志
```

监控项：ERROR、重复报价（placed-cancelled > 1）、强制对冲推迟（>3 次）、Phase 转换、订单失败、WebSocket 异常

## 当前状态（2026-08-01）

- 分支：`feature/20260801_traderV2`
- P1.2 补单成功率监控已完成（recovery_events 表 + CLI），等待线上数据积累
- **Will Super Typhoon Dolphin hit Japan?** 市场长期敞口 -40（持有 NO，需买 YES 补单），已在 Phase 2 公允价 0.550 挂单，但 ask 0.560 > pair-cap 0.490 导致对冲推迟（经济原因非 bug）
- **Will "Widow's Bay" win Emmys 2026** 市场敞口 +39，出现过成本基准未知导致补单跳过
- SSL EOF 报错偶发（Polymarket API 连接瞬时中断），自动恢复

## 编码约定

- 时间戳：内部全部 Unix epoch，显示用北京时间（BEIJING_TZ = UTC+8）
- 日志：中文 + 英文混合，关键业务日志用中文
- Python 3.12+，asyncio 异步架构

## P1 Recovery Episode 模块（2026-08-09 已实现）

- **`pmbot/recovery.py`** — 纯函数恢复决策器：`choose_recovery_action()` 比较 `buy_complement` vs `sell_original` vs `manual_hold` vs `wait`，在损失预算内选择最优路径
- **`pmbot/metrics.py`** — `recovery_episodes` 表 + 5 个生命周期方法（open/update/close/get_open/list）+ `replay_old_recovery_events()` 回放
- **`pmbot/main.py`** — `_manage_market_inventory()` 集成 episode 控制器，支持 `off`/`shadow`/`active` 三种模式
- **`config.debug.yaml`** — `risk.recovery_episode_mode: shadow`（默认），`recovery_max_loss_usd_per_market: 3.0`，`recovery_escalate_after_secs: 180`，`recovery_terminal_after_secs: 900`
- **CLI** — `python -m pmbot.main recovery-episodes`（列出所有 episode），`python -m pmbot.main recovery-replay`（旧事件回放对比），`python -m pmbot.main recovery-history <cid>`（单市场时间线）

### 运行测试
```bash
python -m pytest tests/test_recovery.py tests/test_main.py tests/test_metrics.py -v
```
共 131 个测试通过（1 个预存在的 Rich 表格格式化测试失败，与此功能无关）

### 模式说明
- `off` — 完全走旧代码路径，向后兼容
- `shadow` — 调用 `choose_recovery_action()` 并记录日志/数据库，但不执行 broker 调用（默认，安全）
- `active` — 实际执行 taker_buy 恢复操作（需用户明确授权）
