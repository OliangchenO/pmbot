# Reward-exit 无订单 ID take 成交归因

## 目标

避免奖励退出批次在交易所已成交互补 token 后，因 WebSocket 成交事件缺少订单 ID 而没有更新批次进度，继而反复提交 FAK 买单。

## 已确认事实

- 2026-08-12 的 MrBeast 批次目标为 YES 100 股；交易所实际回报了约 252 股 YES。
- 这些成交的事件没有订单 ID，当前代码将它们标成 `forced_hedge`，且没有写入该批次的 `reward_exit_fills`。
- 批次因此持续显示 `take_filled_size=0`，反复提交同一 100 股 FAK。

## 设计

1. `LiveBroker` 在提交 batch take 时登记短暂、单批次的待归因上下文：CID、互补 token、batch ID 和提交时间。
2. 接收无订单 ID 的 taker BUY 成交时，只有它匹配唯一的待归因上下文，才将其标记为 `batch_take`、附上 `batch_id`；否则保留原有 forced-hedge 归因。
3. `batch_take` 成交以交易所 `fill_id` 写入 `reward_exit_fills`。已有 fill ID 不重复计入。
4. 编排器只从这组持久化成交重建批次进度。尚有未结清的待归因 take 时，不再次提交 FAK；完成目标或确认零成交后才解除。

## 边界

- 不回补这次历史多买成交，也不取消、重启或改变运行中进程。
- 不把不同批次、不同 token 或普通 forced hedge 的成交混入。
- 仍由每笔普通奖励成交独立创建 2x take 目标，并保留既有 8¢ 成本门控。

## 验收

- 无订单 ID 的匹配 take fill 会以真实价格、手续费和 `fill_id` 记入对应批次。
- 相同 fill 重放不增加进度。
- 待归因期间重复 tick 不调用第二次 `taker_buy`。
- 不匹配上下文的 taker fill 仍不进入奖励退出批次。
