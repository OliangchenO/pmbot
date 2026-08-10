# 普通奖励成交后双倍反向 Take 与盈利覆盖退出设计

## 1. 目标

当一笔普通奖励 BUY 订单成交后，为该笔成交建立独立批次，立即 take 买入双倍数量的互补 token。双倍 take 全部确认成交后，用其中与原始成交等量的互补份额锁定二元市场配对结果，计算这部分交易的实际亏损；再将剩余互补份额以 maker SELL 挂出，使其全部成交后的净收益至少覆盖前述配对亏损。

示例：普通奖励订单 `BUY YES 20 @ 0.51` 成交后，立即执行 `BUY NO 40`。前 20 份 NO 与 20 份 YES 配对，后 20 份 NO 按覆盖配对亏损所需的最低价格挂 maker SELL。

本设计只定义新批次流程，不启动或重启实盘进程，不改变已经运行的进程，也不授权实际下单。

## 2. 已确认的业务规则

1. 每笔普通奖励成交创建一个独立批次，不按市场合并成本或盈亏。
2. 普通成交可以是 YES 或 NO；补入的始终是互补 token，数量始终是原始成交数量的两倍。
3. 双倍 take 不设置价格上限、pair-cap 或亏损预算。盘口价格再差也必须继续尝试买满。
4. 只有交易所确认双倍 take 已全部成交，才能计算配对亏损并进入 SELL 阶段。
5. take 部分成交时，仅对剩余数量继续 take，不重复购买已经确认的数量。
6. take 完成后立即挂 maker SELL，不等待市场价格先达到目标。
7. maker SELL 的全部净收益必须至少覆盖剩余互补份额成本与配对锁定亏损。
8. 批次未关闭期间，该市场停止所有普通奖励报价。
9. 同一市场全部批次关闭后只解除锁定，不立即恢复报价。该市场必须在后续奖励扫描中重新进入 Top-N，才能恢复普通奖励报价。
10. 目标卖价高于市场合法最高价时，不允许用更低价格静默退出；批次进入人工持有状态并保持市场锁定。

## 3. 方案选择

采用独立的“奖励成交退出批次”，不复用现有 `RecoveryEpisode`。

现有 `RecoveryEpisode` 以市场净未配对库存为中心，每个 CID 同时只有一个 episode，适合在 `buy_complement`、`sell_original`、`manual_hold` 之间选择最低损失路径；它无法可靠表达同一市场多笔普通成交各自独立的 take 成本、配对亏损和目标 SELL。将新行为塞入现有 episode 会混淆成本归属，也会让某批次收益补贴另一批次亏损。

新批次与现有 recovery 并存，但在批次锁定市场上，新批次拥有排他的库存管理权。

## 4. 状态模型

### 4.1 批次状态

```text
TAKE_PENDING
    | 双倍互补份额全部确认成交
    v
SELL_PENDING
    | 剩余互补份额全部确认卖出，且无活动订单
    v
CLOSED

TAKE_PENDING / SELL_PENDING
    | 账本无法恢复、目标价非法或无法安全继续
    v
MANUAL_HOLD
```

- `TAKE_PENDING`：批次已由普通奖励 fill 创建，尚未确认买满 `2q` 份互补 token。
- `SELL_PENDING`：take 已全部确认，配对亏损和剩余成本已固化，maker SELL 正在挂单或等待恢复。
- `CLOSED`：take 与 SELL 成交事实完整，剩余数量为零，没有该批次活动订单。
- `MANUAL_HOLD`：自动流程不能在既定规则内安全完成。该状态不自动降级为 take SELL，也不解除市场锁定。

### 4.2 市场状态

任一批次处于 `TAKE_PENDING`、`SELL_PENDING` 或 `MANUAL_HOLD` 时，CID 处于 `reward_exit_locked`：

- 不生成普通奖励报价；
- 撤销仍在交易所存活的普通奖励订单；
- 不执行旧的被动 recovery、Phase 2 recovery、`sell_original` 或 forced hedge；
- 允许等额 YES/NO 执行 merge，但 merge 不能改写批次成交账本；
- 只允许该 CID 的批次 take、批次 SELL、订单核对和必要撤单。

同一 CID 所有批次均为 `CLOSED` 后解除 `reward_exit_locked`。解除锁定仅代表该 CID 可以重新参加奖励筛选；只有它在一次新的扫描结果中进入 Top-N，普通报价才恢复。

## 5. 数据模型

新增持久化实体 `RewardExitBatch`，每个普通奖励 fill 一行：

| 字段 | 含义 |
| --- | --- |
| `batch_id` | 稳定唯一批次标识 |
| `cid` | 市场 condition id |
| `origin_order_id` | 原始普通奖励订单 id |
| `origin_fill_id` | 原始普通奖励成交 id，唯一去重 |
| `origin_token_id` | 原始成交 token |
| `complement_token_id` | 互补 token |
| `origin_size` | 原始实际成交数量 `q` |
| `origin_notional_usd` | 原始实际成交金额 |
| `origin_fee_usd` | 原始实际费用 |
| `take_target_size` | 固定为 `2q` |
| `take_filled_size` | 已确认 take 数量 |
| `take_notional_usd` | 已确认 take 成交金额 |
| `take_fee_usd` | 已确认 take 费用 |
| `paired_size` | 固定为 `q`，take 完成后固化 |
| `paired_loss_usd` | 配对锁定亏损 |
| `exit_initial_size` | 固定为 `q` |
| `exit_filled_size` | 已确认 SELL 数量 |
| `exit_notional_usd` | 已确认 SELL 收入 |
| `exit_fee_usd` | 已确认 SELL 费用 |
| `exit_target_price` | 合法 tick 上的 maker SELL 目标价 |
| `status` | 批次状态 |
| `manual_reason` | 进入 `MANUAL_HOLD` 的机器可检索原因 |
| `created_ts` / `updated_ts` / `closed_ts` | 生命周期时间戳 |

新增批次订单表，至少包含：

- `order_id`、`batch_id`、`cid`；
- `intent`：`normal_reward`、`batch_take`、`batch_exit`、现有 recovery/hedge 用途；
- token、方向、请求价格、请求数量；
- 提交状态、确认状态和时间戳。

每笔 fill 以交易所 `fill_id` 去重，并保存其 `order_id`、`batch_id`、token、方向、实际价格、实际数量、实际费用和成交时间。只有已持久化为 `normal_reward` 的 BUY fill 可以创建批次；`batch_take`、`batch_exit` 和来源不明的 fill 均不能触发新批次。

## 6. 执行流程

### 6.1 普通奖励成交

1. 接收并去重普通奖励 BUY fill。
2. 在同一数据库事务中写入 fill、创建 `RewardExitBatch`、写入 `TAKE_PENDING` 和 CID 锁定事实。
3. 立即停止该 CID 的普通报价生成，并撤销其余普通奖励订单。
4. 原始订单可能在撤单确认前继续产生 fill；每个新增 fill 仍创建独立批次。

### 6.2 双倍互补 take

设原始成交数量为 `q`，目标互补数量为 `2q`。

1. 根据原始 token 选择互补 token：YES 对应 NO，NO 对应 YES。
2. 使用 FAK BUY 提交；接口必须填写限价时使用市场合法最高买价 `1 - tick`，不应用经济价格上限、pair-cap 或亏损预算。
3. 以交易所成交事实更新 `take_filled_size`，不能用请求数量或“提交成功”代替成交数量。
4. 若部分成交，下一次仅提交 `2q - take_filled_size`。
5. 网络失败、超时或交易所拒绝时使用有间隔的重试调度，不能在事件循环中无间隔忙重试，也不能因失败解除市场锁定。
6. 只有 `take_filled_size == 2q`，且所有成交明细均已落库，批次才能计算损失并进入 `SELL_PENDING`。

多个批次可以同时存在，但同一 CID 的订单提交与成交归账由市场级异步锁串行化。每张 take 订单都绑定唯一 `batch_id`，禁止仅凭市场净仓位向不同批次分摊成交。

### 6.3 Take 成本拆分

按 take fill 的成交时间和 fill id 稳定排序，使用 FIFO 拆分：

- 前 `q` 份互补 token 分配给配对部分；
- 后 `q` 份分配给待退出部分；
- 某个 fill 跨越分界点时，按数量拆分其成交金额和费用；
- 交易所未提供逐 fill 费用而只提供订单总费用时，按该订单各部分成交金额比例分摊。

拆分结果一旦批次进入 `SELL_PENDING` 即固化。后续 merge、持仓轮询或其他批次成交不得重算历史成本。

### 6.4 配对锁定亏损

定义：

```text
paired_cost =
    origin_notional_usd
    + origin_fee_usd
    + paired_complement_notional_usd
    + paired_complement_fee_usd

paired_loss_usd = max(0, paired_cost - q)
```

YES 与 NO 等量配对的到期或 merge 名义回收为每对 1 美元，因此 `q` 对的名义回收为 `q` 美元。若 `paired_cost < q`，本设计只把配对亏损记为零，不把配对利润抵扣其他批次目标。

### 6.5 Maker SELL 目标价

定义剩余互补份额：

```text
exit_size = q
exit_cost_usd =
    remaining_complement_notional_usd
    + remaining_complement_fee_usd

required_net_exit_usd = exit_cost_usd + paired_loss_usd
```

选择能够满足下式的最低合法价格 `p`：

```text
p * exit_size - estimated_sell_fee(p, exit_size)
    >= required_net_exit_usd
```

价格从理论值向上取到市场合法 tick。为避免提交时穿过买盘：

```text
exit_target_price = max(
    ceil_to_tick(theoretical_price),
    best_bid + tick
)
```

当前安装的 `py_clob_client_v2` 和现有 `_place_sell()` 路径未发现 post-only 参数；因此这里的 maker SELL 明确定义为“按提交前最新确认订单簿计算、提交时不穿价的 GTD 限价 SELL”，不能承诺交易所一定把它记为 maker。批次 SELL 必须使用提交前最新的 best bid，并用最坏情况下适用的卖出费率反解目标价。这样即使在读取盘口与提交之间发生竞价、订单意外立即成交，其成交限价仍不得低于覆盖成本所需价格。若未来 SDK 提供可靠 post-only，则可在不改变本设计价格公式的前提下附加该约束。

若 `exit_target_price` 高于该市场合法最高价格，批次进入 `MANUAL_HOLD`，记录完整公式输入并保持市场锁定。

### 6.6 SELL 生命周期

take 全部确认后立即对 `exit_size` 挂批次专用 GTD SELL：

- SELL 跟踪键为 `batch_id`，不能复用当前每市场单例的 `_exit_orders[cid]`；
- 部分成交后只保留或替换剩余数量；
- 未改变价格和剩余数量且未进入 GTD 刷新窗口时保留原订单，避免丢失队列优先级；
- 数量变化或即将过期时采用 cancel-before-post，防止同一批次两张 SELL 同时成交而超卖；
- SELL 的实际成交额和费用持续入账，但目标价格不因部分成交向下重算；
- 只有剩余数量为零、活动订单为零且成交账本完整时才能关闭批次。

## 7. 与现有模块的边界

### 7.1 `pmbot/userfeed.py`

负责尽快接收真实成交事件，但不在 WebSocket 回调内直接执行整套交易流程。回调只完成规范化、来源识别、去重和事件投递，避免网络回调阻塞。

### 7.2 `pmbot/brokers.py`

负责订单提交、撤销、交易所核对和批次订单跟踪。新增接口必须返回“已确认成交数量”或可供后续核对的订单 id，不能把请求量当作成交量。批次 SELL 需要按 `batch_id` 管理，不能覆盖同市场其他批次的订单。

### 7.3 新的纯计算模块

批次成本拆分、配对亏损和目标 SELL 价格应放在独立纯函数中，不依赖 broker、数据库或全局状态，以便覆盖多档成交、费用分摊、tick 取整和非法目标价测试。现有 `pmbot/recovery.py` 保持其市场级 recovery 语义，不把两套决策混在同一个函数中。

### 7.4 `pmbot/metrics.py`

负责批次、批次订单和批次 fill 的 SQLite 持久化与幂等更新。一次状态转换及其关键经济字段必须在同一事务中提交。

### 7.5 `pmbot/main.py`

负责市场锁定、批次调度、旧 recovery 隔离、Top-N 恢复条件和中文状态输出。对 `reward_exit_locked` CID，普通报价路径和 `_manage_market_inventory()` 的旧动作都必须显式跳过。

## 8. 重启与核对

启动恢复顺序固定为：

```text
读取未关闭批次
-> 立即恢复 CID 锁定
-> 查询交易所订单和成交
-> 补记本地缺失的 fill
-> 按 batch_id 重算批次状态
-> 撤销重复或归属冲突的订单
-> 继续未完成 take 或恢复 maker SELL
-> 允许无锁定市场进入普通报价循环
```

安全规则：

- 本地“已提交”不能证明 take 已成交；
- WebSocket 重放和 REST 核对看到同一 `fill_id` 时只入账一次；
- 来源不明的历史 fill 不触发无价格上限的双倍 take；
- 订单用途或成交归属无法证明时进入 `MANUAL_HOLD` 并报警；
- 程序崩溃发生在“交易所成交、本地落库之前”时，必须先通过交易所成交历史恢复，不能直接重发目标全量；
- 临近结算不会自动把 maker SELL 改成 take SELL。

## 9. 配置与发布方式

新增一个行为开关：

```yaml
risk:
  reward_exit_batch_mode: shadow
```

- `off`：完全保持现有行为，不创建或执行新批次。
- `shadow`：识别普通 fill、生成批次决策、计算 take 目标和 SELL 价格并记录日志，但不撤普通订单、不 take、不挂 SELL，也不阻断现有 recovery。
- `active`：执行本设计的锁定、撤单、双倍 take 和 maker SELL。

初始配置使用 `shadow`。从 `shadow` 切换到 `active` 属于单独的实盘配置与进程操作，不由本设计文档自动执行。

不新增 take 经济价格上限、pair-cap、最大亏损或恢复预算。FAK 接口所需的技术限价固定使用市场合法最高买价 `1 - tick`。重试调度复用现有网络重试节奏，批次 SELL 复用现有退出订单 TTL；新增的批次订单跟踪只改变订单归属键，不改变这些时间参数。

## 10. 日志与审计事件

关键事件采用中文说明并保留稳定英文标识：

- `REWARD_EXIT_BATCH_OPENED`
- `MARKET_REWARD_EXIT_LOCKED`
- `BATCH_TAKE_SUBMITTED`
- `BATCH_TAKE_PARTIAL`
- `BATCH_TAKE_COMPLETED`
- `BATCH_LOSS_LOCKED`
- `BATCH_SELL_PLACED`
- `BATCH_SELL_PARTIAL`
- `REWARD_EXIT_BATCH_CLOSED`
- `BATCH_MANUAL_HOLD`
- `MARKET_REWARD_EXIT_UNLOCKED`

每条适用事件包含 `cid`、`batch_id`、`order_id`、`fill_id`、token、方向、价格、数量、费用、剩余数量和 `reason=`。`BATCH_LOSS_LOCKED` 额外记录原始成本、配对互补成本、配对回收、锁定亏损、剩余成本、预计卖出费率、理论目标价和最终 tick 价格，使目标 SELL 可以事后复算。

## 11. 验收标准

### 11.1 纯计算

- `BUY YES 20` 生成 `BUY NO 40`；`BUY NO 20` 对称生成 `BUY YES 40`。
- 多档 take 成交使用真实成交明细，FIFO 正确拆成前后各 `q` 份。
- 跨分界 fill 的成交额和费用按数量正确拆分。
- 配对亏损严格按实际成本减 `q` 计算，负值归零。
- SELL 目标覆盖剩余成本、配对亏损和卖出费用，并向上取 tick。
- 最终挂价不低于提交前 `best_bid + tick`。
- 高于合法最高价时返回明确的人工持有结果。

### 11.2 事件与幂等

- 只有 `normal_reward` BUY fill 创建批次。
- take、SELL、现有 recovery/hedge 和来源不明 fill 不递归创建批次。
- 重复 `fill_id` 不重复创建批次或累计数量。
- 同市场多笔普通 fill 创建独立批次，成本和订单互不混用。

### 11.3 执行状态

- take 部分成交时不计算配对亏损、不挂 SELL。
- take 全部确认后只创建一张有效的批次 SELL。
- SELL 部分成交后只管理剩余数量，且 cancel-before-post 不产生超卖窗口。
- 批次期间普通报价、旧 recovery、Phase 2 和 forced hedge 均被阻断。
- 所有批次关闭后只解除锁定；未重新进入 Top-N 时不恢复报价。
- `MANUAL_HOLD` 和临近结算场景保持锁定，不自动 take SELL。

### 11.4 重启与模式

- 崩溃发生在 take 提交前、部分成交后、全成交未落库、SELL 挂出后和 SELL 部分成交后时，均能从交易所事实恢复而不重复下单。
- `off` 模式保持现有行为不变。
- `shadow` 模式不调用真实撤单、take 或 SELL 接口。
- `active` 模式按批次状态执行，且所有订单可追溯至 `batch_id`。

### 11.5 回归验证

完成对应 focused tests 后，运行完整测试套件、Python `compileall` 和 `git diff --check`。测试只证明本地实现行为；在未获得明确授权前，不启动或重启 live 进程，不修改 live 配置，不用实盘订单验证设计。
