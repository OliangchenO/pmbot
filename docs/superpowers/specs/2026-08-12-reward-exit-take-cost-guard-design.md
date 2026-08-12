# Reward-exit 双倍 take 成本保护

## 目标

防止普通奖励成交后，自动双倍买入互补 token 时以明显亏损的价格成交；同时保持未完成 batch 的市场锁和可恢复性。

## 已确认的规则

- 每笔 `normal_reward` 成交仍独立创建一个 batch，目标 take 数量仍为原始成交数量的两倍。
- 自动 take 的每对最大总成本为 `$1.08`，包含原始成交价、互补 token 最优 ask 及预计 taker 手续费。
- 超过上限时不得提交自动 taker 买单。

## 状态机

1. 普通奖励成交创建 `TAKE_PENDING` batch。
2. 每次尝试下单前计算最大可接受 take 价格。若当前最优 ask 超过该上限，batch 转为 `TAKE_BLOCKED`，保持 CID lock，不提交订单。
3. 后续 tick 重新评估 `TAKE_BLOCKED`；成本重新回到 `$1.08` 以内时转回 `TAKE_PENDING` 并继续。
4. 只能用交易所确认的 `fill_id`、成交价与手续费累计 take。订单意图和下单限价不能计入实际成本或完成数量。
5. 累计确认 take 满 `2q` 后，按真实成交重建批次：配对 `q`，剩余 `q` 转为 `SELL_PENDING`。
6. `TAKE_BLOCKED` 和 `SELL_PENDING` 不能因为批次年龄或启动清理而关闭；它们持续保持 CID lock，直至完成退出或明确人工处置。

## 配置

在 `risk` 下新增 `reward_exit_max_pair_loss_cents: 8.0`。未提供该值时沿用 8.0 的安全默认值。

## 验收标准

- 原始成交 0.59、互补 ask 0.86 时，不提交 take，batch 为 `TAKE_BLOCKED` 且市场仍被锁定。
- 原始成交 0.59、互补 ask 0.41 时，可进入正常 take。
- 实际成交 0.86 不得被下单限价 0.75 记为成本。
- take 完成后必须先生成正确的 paired/exit 数量再进入 `SELL_PENDING`；超时不关闭该残余仓 batch。
