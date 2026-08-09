## P0 pickoff 信号 — 改动说明

### 改动文件

| 文件 | 改动类型 |
|---|---|
| `config.yaml` | guards 注释全部中文化，新增 4 个 pickoff 配置项 |
| `pmbot/risk.py` | `_load()` 读取配置、`quote_risk_decision()` 新增 `own_fills` 参数、新增 `_compute_pickoff_score()` 方法、各侧用自己的独立分数（`yes_score`/`no_score`）、方向优先级改为 flow→pickoff→markout |
| `pmbot/main.py` | 调用 `quote_risk_decision` 时传入 `own_fills=self.broker.fills_log` |

### 信号逻辑

`_compute_pickoff_score(market, now, fills_log)` 在 `pickoff_window_secs`（默认 300s）内按侧统计自己 **maker 成交**（过滤 taker/exit），返回 `(yes_score, no_score)`。

| 同侧被吃次数 | yes/no 侧 score | P0 动作 |
|---|---|---|
| 0 | 0.00 | allow |
| 1 (pickoff_widen_fills) | 0.60 | widen（拉宽该侧报价） |
| 2 (pickoff_pull_fills) | 0.85 | pull（撤下该侧报价） |
| 3+ | 0.90~1.00 | pull（持续加深） |

### 危险方向判定

优先级：**flow（大流量失衡）→ pickoff（累积小单）→ markout（成交后漂移）**

当 flow=0 时（单笔成交 < `flow_min_volume_shares`），pickoff 接管方向判断。被吃的是 YES 侧 → YES 侧危险 → 只拉/撤 YES；被吃的是 NO 侧 → 只拉/撤 NO。**每侧用自己的独立分数**（`yes_score = max(shared, pickoff_yes)`, `no_score = max(shared, pickoff_no)`），YES 被吃掉 1 笔不会把 NO 也拉下来。

### 与 check_fills 的关系

互补，不冲突：
- **check_fills**：3 笔同侧吃单 → 停整个市场 45 分钟（硬熔断，阻塞式）
- **P0 pickoff**：1 笔拉宽、2 笔撤单，只影响被吃侧（软预警，渐进式）
- pickoff 在 P0 审计链路中（`quote_risk_decisions` 表），可追溯

### 向后兼容

`own_fills` 默认 `None`，不传时 pickoff=0 完全不影响原有逻辑。paper 模式同样受益。
