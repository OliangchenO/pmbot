# 价格感知型退出策略修订实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将当前基于历史中间价触发的退出原型，修订为基于可成交价格、手续费、盘口深度和成交确认的安全退出机制，并先以 shadow 模式验证，未经明确批准不得启用 live 下单。

**Architecture:** 把“是否存在退出机会”做成无副作用的纯计算层，把“撤单、下单、等待成交、仓位确认”做成每市场互斥的生命周期状态机。现有时间驱动恢复和强制对冲继续保留，但只能在明确的优先级和状态转换下接管，不能与价格退出在同一轮互相覆盖。

**Tech Stack:** Python 3.11、asyncio、Polymarket CLOB、SQLite、pytest、现有 `BookTracker`、`PaperBroker`、`LiveBroker`、`MetricsStore`。

## Global Constraints

- `config.yaml` 中 `exit_strategy.enabled` 必须默认为 `false`；完成 shadow 验证前不得改为 `true`。
- 不启动或重启 live 进程，不撤销真实订单，不提交真实订单；上线动作需要用户另行明确授权。
- 盈利判断只能使用可成交 `best_bid` / `best_ask`、对应深度、tick 和手续费；`mid` 仅可用于观测指标。
- 同侧卖出、互补 FAK 和现有强制对冲在同一市场必须互斥。
- 下单成功、部分成交、完全成交、REST 仓位确认和 merge 确认是不同事件，不得合并为“退出成功”。
- 只有仓位快照确认 `abs(unpaired) < MIN_TAKER_SHARES` 后，才清理 `_over_since`、`unpaired_since` 和退出状态。
- 保留现有强制对冲硬约束：`basis + hedge_price + taker_fee <= 1.00`；价格退出如要求正利润，使用更严格的目标上限。
- 任何新增日志继续写入现有日志路径，保留可搜索事件 ID 和 `reason=`，不得阻塞交易循环。
- 当前工作区已有未提交修改；实施时只修改本计划列出的文件，不覆盖、删除或提交无关改动。

---

## 1. 当前原型的问题摘要

### 1.1 经济判断不成立

- 同侧路径用 `mid >= target` 触发，但真正能够卖出的价格是 bid。
- 互补路径用 `comp_mid <= cap` 触发，却按 ask 买入，且没有把 taker fee 纳入盈利判断。
- `best_bid - N*tick` 会降低卖出保护价；剩余未成交部分可能挂在目标利润以下。
- 历史 `prices-history` 中间价触及率只能说明价格路径，不能证明当时存在足量、扣费后仍盈利的可成交盘口。

### 1.2 订单生命周期不安全

- 当前互补路径不检查 `taker_buy()` 返回的 `filled`，零成交和部分成交都会返回 `complement_profit`。
- 在仓位确认前清理 `_over_since` / `unpaired_since`，会丢失真实风险年龄。
- 互补 FAK 前没有确认撤销同市场普通报价和 EXIT SELL，可能发生双重成交或反向库存。
- 同侧 EXIT SELL 提交后仍继续运行强制对冲，同一轮可能先挂卖单、再撤单、再买互补 token。

### 1.3 状态、指标和测试不可靠

- `_exit_state` 在库存归零时未清理；同一 CID 的下一轮库存可能继承旧去抖时间。
- `_over_since` 不区分库存方向和成本批次，新成交可能直接继承旧仓位的 24 小时阶段。
- `exit_events` 在订单提交前记录“盈利退出”，记录的是决策而非成交，且会重复写入。
- 当前新增测试把 async 方法当同步方法调用，并使用了错误的 `Book` 构造方式；实测结果为 `6 passed, 11 failed`。

## 2. 目标行为

### 2.1 可成交经济约束

同侧卖出目标偏移记为 `target_offset`，目标保护价为：

```python
min_net_sell_price = basis + target_offset + estimated_sell_fee_per_share
sell_limit = ceil_to_tick(min_net_sell_price, market.tick)
eligible = best_bid >= sell_limit and executable_bid_size >= current_clob_min_order_size
```

下单时使用 `sell_limit`，不使用 `best_bid - tick`。订单如果跨过买盘，可获得价格改善；未立即成交的剩余部分也不会低于净收益保护价。

互补买入的最高价格必须逐 tick 计算：

```python
basis + complement_price + taker_fee(market, complement_price) <= 1.0 - target_offset
```

只有 `best_ask <= complement_cap` 且 cap 内累计 ask 深度达到当前市场最小下单量时，才允许生成互补退出决策。FAK 的 limit 使用 `complement_cap`，实际净收益按成交均价重新计算。

### 2.2 两条机会同时出现时的选择

纯计算层同时评估同侧和互补路径，按以下顺序选一个动作：

1. 过滤掉不满足最小规模、深度、费用和价格上限的动作。
2. 比较预计净 PnL/股，选择更高者。
3. 净 PnL 相同时优先同侧卖出，因为它减少库存和抵押占用，不新增互补头寸。
4. 一次管理循环最多返回一个可执行动作。

### 2.3 每市场状态机

状态定义：

```text
IDLE
  -> SAME_SIDE_EXIT_OPEN
  -> COMPLEMENT_PREPARING

SAME_SIDE_EXIT_OPEN
  -> IDLE                    价格失效且订单已确认撤销
  -> PENDING_RECONCILE       发生卖出成交
  -> FORCED_HEDGE_PREPARING  urgent 接管且 EXIT SELL 已确认撤销

COMPLEMENT_PREPARING
  -> IDLE                    冲突订单撤销失败或机会失效
  -> PENDING_RECONCILE       FAK 有成交

PENDING_RECONCILE
  -> FLAT_CONFIRMED          REST 仓位确认配平/清仓
  -> IDLE                    仅部分成交，按新剩余仓位重新评估

FLAT_CONFIRMED
  -> IDLE                    清理本轮状态
```

状态机不把“订单已提交”当成“成交”，也不因部分成交清空风险计时。

### 2.4 与现有时间管道的优先级

每轮 `_manage_market_inventory()` 使用固定顺序：

1. manual hold / pending reconcile / flat guard；
2. 计算库存事实、basis、库存批次和 urgent；
3. urgent 为真时，取消价格退出订单并由现有强制对冲接管；
4. 非 urgent 时运行价格退出决策；
5. 若已提交或保留同侧退出订单，本轮直接返回，不再强制对冲；
6. 若互补 FAK 已进入 pending reconcile，本轮直接返回；
7. 没有价格动作时才继续现有恢复和强制对冲逻辑。

同侧决策只依赖同侧 book；互补 book 缺失或价差过宽不能阻止同侧盈利退出。

## 3. 文件结构与接口

### 新增 `pmbot/exit_strategy.py`

职责：纯计算，不写数据库、不写 broker、不修改 Bot 状态。

```python
from dataclasses import dataclass
from typing import Literal

ExitAction = Literal["none", "same_side_sell", "complement_buy"]

@dataclass(frozen=True)
class ExitDecision:
    action: ExitAction
    token_id: str | None
    limit_price: float | None
    requested_size: float
    expected_net_pnl_per_share: float | None
    phase: str
    reason: str

phase_target_offset(cfg: dict, elapsed_hours: float) -> tuple[str, float]

complement_profit_cap(
    market: gamma.Market,
    basis: float,
    target_offset: float,
) -> float

evaluate_exit(
    market: gamma.Market,
    unpaired: float,
    basis: float | None,
    elapsed_hours: float,
    excess_book: Book | None,
    complement_book: Book | None,
    clob_min_order_size: float | None,
    cfg: dict,
) -> ExitDecision
```

### 修改 `pmbot/main.py`

职责：采集事实、维护状态机、协调撤单/下单/仓位确认。

```python
@dataclass
class ExitRuntimeState:
    inventory_side: str
    basis: float
    initial_size: float
    started_ts: float
    status: str = "IDLE"
    decision_key: str | None = None
    submitted_size: float = 0.0
    filled_size: float = 0.0

_prepare_complement_taker(self, market: gamma.Market) -> Awaitable[bool]
_execute_exit_decision(
    self,
    market: gamma.Market,
    cid: str,
    unpaired: float,
    basis: float,
    decision: ExitDecision,
    now: float,
) -> Awaitable[str]
```

`_prepare_complement_taker()` 必须：

1. 调用 `set_exit(market, None)`；
2. 调用 `cancel_quotes_for_market(market)`；
3. 检查本地 `exit_quote(market) is None` 且 `open_quotes(market) == []`；
4. 任一条件不成立时返回 `False`，记录 `reason=conflicting_orders_not_confirmed_cancelled`，不提交 FAK。

### 修改 `pmbot/metrics.py`

`exit_events` 改为生命周期事件表，不再把决策直接命名为退出成功。至少包含：

```text
decision_id, event, action, reason,
requested_size, filled_size, remaining_size,
basis, target_offset, limit_price,
expected_net_pnl_per_share, actual_avg_price,
realized_net_pnl_per_share, order_id, phase
```

允许的 `event`：

```text
decision | skipped | order_submitted | zero_fill | partial_fill |
pending_reconcile | reconciled_partial | flat_confirmed | order_cancelled
```

### 修改 `scripts/analyze_fill_targets.py`

职责改为“历史机会区间分析”，不能输出“策略盈利率”。分析必须：

- 按 CID、方向和连续未配平区间聚合为库存 episode，不按单笔 fill 重复计数；
- 每个 episode 使用当时加权成本和真实起止时间；
- 搜索窗口分别截断为 2h、8h、24h，以及真实退出/市场结束时间中的最早值；
- 排除市场结束之后的价格点；
- 单独报告数据缺失、只有单侧 token 历史和被截尾样本；
- 把 midpoint 命中率标记为“机会率上界”，不等价于可成交率；
- 按市场聚类报告 episode 数，避免把同一市场的多个 fill 当作独立样本。

## 4. 实施任务

### Task 1: 先封住 live 风险并修复测试基线

**Files:**
- Modify: `config.yaml`
- Modify: `tests/test_exit_strategy.py`
- Test: `tests/test_exit_strategy.py`

**Interfaces:**
- Consumes: 当前未提交的 `Bot._exit_strategy_check()` 原型。
- Produces: 默认关闭的配置和能够真实执行 async 代码的测试基线。

- [ ] **Step 1: 写失败测试，固定默认关闭约束**

```python
def test_live_config_keeps_price_exit_disabled():
    cfg = load_config("config.yaml")
    assert cfg["exit_strategy"]["enabled"] is False
```

- [ ] **Step 2: 修复 async 测试调用方式**

不新增 pytest 插件，统一使用现有 Python：

```python
result = asyncio.run(bot._exit_strategy_check(
    m, "cid1", unpaired=10, basis=0.55,
    now=time.time(), urgent=False,
))
```

通过 `Book()` 后赋值构造盘口，不再传不存在的构造参数：

```python
book = Book("y1")
book.bids = {0.55: 10.0}
book.asks = {0.56: 10.0}
```

- [ ] **Step 3: 将 `exit_strategy.enabled` 改为 `false`**

- [ ] **Step 4: 运行测试，确认基线真实执行**

```powershell
rtk proxy .venv\Scripts\python.exe -m pytest tests\test_exit_strategy.py -q
```

Expected: 不再出现 `coroutine was never awaited` 或 `Book.__init__()` 参数错误；任何剩余失败必须是策略断言失败。

### Task 2: 建立纯经济决策层

**Files:**
- Create: `pmbot/exit_strategy.py`
- Create: `tests/test_exit_strategy_economics.py`
- Modify: `pmbot/main.py`

**Interfaces:**
- Consumes: `gamma.Market`、`books.Book`、现有 fee 公式和 tick。
- Produces: `ExitDecision`、`phase_target_offset()`、`complement_profit_cap()`、`evaluate_exit()`。

- [ ] **Step 1: 写同侧 bid 约束失败测试**

覆盖 `mid` 达标但 `best_bid` 未达标时返回：

```python
assert decision.action == "none"
assert decision.reason == "same_side_bid_below_net_target"
```

- [ ] **Step 2: 写互补 ask 加手续费失败测试**

使用 `basis=0.55`、NO `bid=0.42`、`ask=0.45`；即使 mid 为 `0.435`，也必须拒绝 `+1c` 目标。

```python
assert decision.action != "complement_buy"
assert decision.reason == "complement_ask_above_profit_cap"
```

- [ ] **Step 3: 写深度、最小下单量和双机会择优测试**

必须覆盖：cap 内深度不足、市场最小量高于剩余仓位、同侧与互补同时满足时选择净 PnL 更高者、同收益时同侧优先。

- [ ] **Step 4: 实现最小纯函数**

复用 `_forced_hedge_max_price()` 的逐 tick 思路，但把右侧从 `1.0` 改为 `1.0 - target_offset`。不得读取 Bot 状态或调用 broker。

- [ ] **Step 5: 运行纯函数测试**

```powershell
rtk proxy .venv\Scripts\python.exe -m pytest tests\test_exit_strategy_economics.py -q
```

Expected: 全部通过。

### Task 3: 实现互斥生命周期和成交确认

**Files:**
- Modify: `pmbot/main.py`
- Modify: `pmbot/brokers.py`（仅在现有接口无法返回确认结果时扩展最小返回值）
- Modify: `tests/test_exit_strategy.py`
- Modify: `tests/test_brokers.py`

**Interfaces:**
- Consumes: Task 2 的 `ExitDecision`。
- Produces: `ExitRuntimeState`、`_prepare_complement_taker()`、`_execute_exit_decision()`。

- [ ] **Step 1: 写零成交和部分成交测试**

断言：

```python
assert bot._over_since[cid] == original_started_ts
assert bot._exit_state[cid].status in {"IDLE", "PENDING_RECONCILE"}
```

零成交不得清理计时；部分成交必须保存 `filled_size`，等待新仓位快照。

- [ ] **Step 2: 写冲突订单撤销失败测试**

让 `set_exit(None)` 或 `cancel_quotes_for_market()` 后仍能读到本地订单，断言 `taker_buy()` 未被调用，并记录：

```text
EXIT_STRATEGY_SKIPPED reason=conflicting_orders_not_confirmed_cancelled
```

- [ ] **Step 3: 写同轮互斥测试**

同侧 EXIT SELL 被创建或保留后，断言 `_forced_hedge_allowed()` / `taker_buy()` 在本轮不执行。urgent 接管时，必须先确认 EXIT SELL 已撤销。

- [ ] **Step 4: 写库存批次重置测试**

覆盖归零、方向翻转、basis 显著变化和新增仓位。归零必须清理 `_exit_state`；方向翻转必须开始新 episode；追加仓位不得无条件继承旧 phase D。

- [ ] **Step 5: 实现状态机并调整 `_manage_market_inventory()` 顺序**

价格检查移动到互补 book 宽度 guard 之前；同侧检查不依赖互补 book。任何已执行的价格动作在本轮直接返回。

- [ ] **Step 6: 运行相关回归测试**

```powershell
rtk proxy .venv\Scripts\python.exe -m pytest tests\test_exit_strategy.py tests\test_brokers.py tests\test_main.py -q
```

Expected: 全部通过；若全套存在与本改动无关的已知失败，必须分别报告相关测试结果与全套结果。

### Task 4: 把指标改成可审计生命周期

**Files:**
- Modify: `pmbot/metrics.py`
- Modify: `pmbot/main.py`
- Create: `tests/test_exit_metrics.py`

**Interfaces:**
- Consumes: Task 3 的状态转换和 broker 返回的实际 filled size。
- Produces: `record_exit_event(cid: str, decision_id: str, event: str, action: str, **facts) -> None` 生命周期记录。

- [ ] **Step 1: 写 schema 和事件语义测试**

同一 `decision_id` 应允许多条按时间排序的生命周期记录；`decision` 行的 `actual_avg_price` 和 `realized_net_pnl_per_share` 必须为 NULL。

- [ ] **Step 2: 写去重测试**

相同 decision key、相同未变化挂单不得在每个管理循环重复写 `decision` 或 `order_submitted`。

- [ ] **Step 3: 修改 schema 与记录调用点**

仅 `flat_confirmed` 可以作为完整退出统计；`partial_fill` 必须同时记录 `filled_size` 和 `remaining_size`；没有真实成交均价时不得填写 realized PnL。

- [ ] **Step 4: 运行指标测试**

```powershell
rtk proxy .venv\Scripts\python.exe -m pytest tests\test_exit_metrics.py tests\test_exit_strategy.py -q
```

Expected: 全部通过。

### Task 5: 修订历史分析口径

**Files:**
- Modify: `scripts/analyze_fill_targets.py`
- Create: `tests/test_analyze_fill_targets.py`

**Interfaces:**
- Consumes: SQLite fills、市场结束时间、`prices-history` midpoint。
- Produces: episode 级“机会率上界”报告，不输出策略盈利结论。

- [ ] **Step 1: 写 episode 聚合测试**

同一 CID 连续三笔同方向 fill、未曾归零时只能形成一个 episode；归零后再次建仓形成新 episode。

- [ ] **Step 2: 写窗口截断和结算后价格排除测试**

成交后 25 小时或市场结束后的 `p=1.0` 不得计入 24 小时目标命中。

- [ ] **Step 3: 写缺失与聚类统计测试**

缺少互补 token 历史的 episode 必须计为 `missing_complement_history`，不能计为“未命中”；汇总同时报告 episode 数和独立市场数。

- [ ] **Step 4: 实现报告修订**

输出列至少包含：`episode_id`、`cid`、方向、开始/结束、basis、窗口、是否截尾、midpoint 命中、首次命中时间、数据质量原因。标题使用“历史 midpoint 机会率上界”。

- [ ] **Step 5: 运行分析单元测试**

```powershell
rtk proxy .venv\Scripts\python.exe -m pytest tests\test_analyze_fill_targets.py -q
```

Expected: 全部通过。重新访问外部 API 或生成 Excel 需要单独执行，不能作为单元测试前提。

### Task 6: Shadow 验证与上线门槛

**Files:**
- Modify: `config.debug.yaml`
- Modify: `pmbot/main.py`
- Modify: `pmbot/metrics.py`
- Modify: `docs/superpowers/plans/2026-08-09-price-aware-exit-strategy-revision.md`（仅填写真实验证结果）

**Interfaces:**
- Consumes: Task 2 决策与 Task 4 指标。
- Produces: 不下单的 shadow 样本和可审计的启用结论。

- [ ] **Step 1: 增加 `exit_strategy.mode: shadow | active`**

`shadow` 只记录 decision 和后续可观测盘口，不调用 `set_exit()`、`cancel_quotes_for_market()` 或 `taker_buy()`；live 配置继续 `enabled: false`。

- [ ] **Step 2: 写 shadow 无副作用测试**

对所有 broker 写方法使用 spy，断言满足退出机会时调用次数仍为 0，但指标存在 `event=decision`、`mode=shadow`。

- [ ] **Step 3: 运行 focused 和全量测试**

```powershell
rtk proxy .venv\Scripts\python.exe -m pytest tests\test_exit_strategy_economics.py tests\test_exit_strategy.py tests\test_exit_metrics.py tests\test_analyze_fill_targets.py -q
rtk proxy .venv\Scripts\python.exe -m pytest -q
rtk git diff --check
```

- [ ] **Step 4: 收集不少于 7 个完整 UTC 日的 shadow 数据**

启用 active 前必须同时满足：

- 至少 7 个完整 UTC 日；
- 每条 decision 有对应的可成交 bid/ask、深度、费用和目标依据；
- 分别统计同侧与互补的机会数、可成交深度、5 秒/30 秒/5 分钟 markout；
- 模拟成交净 PnL 优于现有退出管道，并单独报告尾部损失；
- 无重复 decision、无同市场并发动作、无仓位状态无法解释的样本；
- paper 中完成零成交、部分成交、撤单失败、REST 延迟和重启恢复演练。

- [ ] **Step 5: 独立审批 active**

只有用户基于 shadow 报告明确批准后，才能提出把指定配置从 `shadow` 改为 `active`；配置变更、进程重启和 live 验证分别授权，不能合并推定。

## 5. 验收标准

- 所有利润判断以 executable bid/ask、深度、tick 和手续费为依据。
- 互补 FAK 的 limit 不超过费用后目标 cap；零成交和部分成交不被标记为完整退出。
- 同一市场任何时刻最多存在一种退出动作；提交 FAK 前冲突订单已确认撤销。
- 同侧退出不会因互补 book 缺失/过宽而被跳过，也不会在同轮被强制对冲覆盖。
- 归零、方向翻转和新库存 episode 的状态与计时正确重置。
- 指标能够从 decision 追踪到订单、成交、REST 确认；没有确认时不填写 realized PnL。
- 历史报告明确是 midpoint 机会率上界，排除结算后价格并使用库存 episode。
- `exit_strategy.enabled: false` 时，与修改前时间驱动管道行为一致。
- focused tests、全量 tests 和 `git diff --check` 的真实结果均被分别记录；不得用 focused 通过代替全量通过。

## 6. 明确不在本次范围

- 不改变市场选择和 reward 排名。
- 不改变普通双边报价定价、库存上限或主题风险上限。
- 不改变强制对冲的 break-even 硬约束。
- 不把历史 midpoint 回测包装成真实可成交 PnL。
- 不自动启用 live、不自动重启进程、不自动撤单或下单。
- 不提交 Git；如需提交，由用户在实施和验证完成后另行授权。
