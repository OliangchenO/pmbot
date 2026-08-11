
最紧急的两个修复仍是 P1-6（自适应降价）和 P1-4（超时自动关闭）。同时需解决 metrics.db 损坏问题（停止 bot → `.recover` 命令恢复）。

---

## 第 133 轮（2026-08-12 14:20 BJT）— Scheduled Task double-take-exit-monitor

### 监控结论：Bot 离线中（自 05:05 退出后未重启），修正了前两轮 BATCH_TAKE_EXECUTED 计数错误

**关键修正**：R131/R132 中 BATCH_TAKE_EXECUTED 计数因 grep 正则写错（`TAKE_COMPLETED` 而非 `TAKE_EXECUTED`），实际 Aug 10 有 12 次 EXECUTED 事件（batch 1: 10, batch 2: 2），Aug 12 有 1 次。总计 13 次，非之前报告的 1 次。

Bot 于 Aug 12 05:05:41 优雅退出，之后无新活动。Aug 12 日志 2446 行，末行为 `正在退出，撤销全部订单`。

#### 本轮日志事件统计（Aug 10-12 全部日志）— 修正版

| 事件 | Aug 10 | Aug 11 | Aug 12 | 总计 | 修正说明 |
|------|--------|--------|--------|------|----------|
| REWARD_EXIT_BATCH_OPENED | 2 | 0 | 1 | 3 | — |
| MARKET_REWARD_EXIT_LOCKED | 4 | 0 | 1 | 5 | — |
| BATCH_TAKE_SUBMITTED | 205 | 1 | 1 | 207 | — |
| BATCH_TAKE_EXECUTED | 12 | 0 | 1 | 13 | ⚠️ 修正：R131/R132 误报为 0/0/1 |
| BATCH_SEALED | 0 | 0 | 1 | 1 | — |
| BATCH_TIMING | 0 | 0 | 1 | 1 | — |
| BATCH_SELL_PLACED | 0 | 0 | 21 | 21 | — |
| BATCH_SELL_PARTIAL | 0 | 0 | 0 | 0 | — |
| REWARD_EXIT_BATCH_CLOSED | 0 | 0 | 0 | 0 | — |
| BATCH_LOSS_LOCKED | 0 | 0 | 0 | 0 | — |
| BATCH_MANUAL_HOLD | 0 | 0 | 0 | 0 | — |
| MARKET_REWARD_EXIT_UNLOCKED | 0 | 0 | 0 | 0 | — |

#### 三个 Batch 状态（修正版）

| Batch ID | 市场 | 状态 | TAKE EXECUTED | TAKE filled 金额 | 盈亏 | 冻结时长 | 详细 |
|----------|------|------|---------------|-----------------|------|---------|------|
| 1e5af08f | Ralph Norman (0x3925...) | **BANNED** (banned_markets.json) | 10 次 | $14.80 × 10 = $148 | $0 (banned, 无 SELL) | ~16h (Aug 10 22:19 → 05:05 exit) | FAK 买了 complement 但 EXECUTED 后 remaining_before=7 不变 — 说明 FAK 成交的是额外流动性，未推进 TAKE 完成。从未 SEALED，最终被 LOSS_BAN 禁入 |
| 02cafdd7 | MrBeast 60-70M (0xdf08...) | **BANNED** (banned_markets.json) | 2 次 | $170.49 + $175.12 = $345.61 | $0 (banned, 无 SELL) | ~6h (Aug 10 23:12 → 05:05 exit) | 同样 FAK 成交了大量但 remaining_before=88 不变，从未 SEALED，最终 LOSS_BAN |
| bdbee396 | SSI public model (0x3ed4...) | **SELL_PENDING** (被中断) | 1 次 | $80.00 (remaining_after=0 ✅) | -$3.20 (paired_loss) | ~4h (Aug 12 01:23 → 05:05 exit) | **唯一成功的 TAKE**：SEALED + TIMING 正常，21 次 SELL_PLACED 重复挂单，bot 退出时 ORDER_CANCEL_ALL 撤销了最后的卖单 |

#### 异常分析

1. **🔴 核心 Bug：Batch 1 和 2 的 BATCH_TAKE_EXECUTED 未推进 remaining**：两个 batch 都出现了 FAK 成交（filled 金额巨大：$14.80 和 $170-175），但 `remaining_before` 始终保持初始值（7 和 88），说明 EXECUTED 事件来自用户 feed 推送的**该 market 上的其他成交**（非 reward-exit FAK 订单），被错误归因为 batch 的成交。batch 从未 SEALED，最终因累计损失超限被 LOSS_BAN。

2. **🔴 0 个 batch 成功 CLOSED**：3 个 batch 中 2 个 BANNED（TAKE 未完成），1 个 SELL_PENDING 被中断（bot 退出时 ORDER_CANCEL_ALL）。重启后 bdbee396 需要恢复 SELL 挂单。

3. **🔴 metrics.db 持续损坏**（第 4 轮）：「database disk image is malformed」，无法通过 PRAGMA integrity_check 或任何 SQL 读取。必须停止 bot 后使用 `sqlite3 data/metrics.db ".recover" > recovered.sql` 恢复。

4. **⚠️ Batch 1 和 2 TAKE_EXECUTED 来源疑点**：
   - Batch 1: TAKE_SUBMITTED 在 complement=788668683504 上出价 0.99，但 EXECUTED 显示的 filled=14.80（而 remaining_before=7，这意味着 filled 大于 remaining_before 是预期行为吗？），存在两种可能：FAK 额外成交了流动性，或用户 feed 将 market 上的其他买单成交误匹配到了 batch
   - Batch 2: 同理，filled=$170.49 和 $175.12 远大于 complement token 的理论最大成本（88 × 0.99 = $87.12），且 remaining_before=88 始终不变，强烈指示这些 EXECUTED 事件不属于 reward-exit FAK

5. **⚠️ Bot 离线 ~9h+**：Aug 12 05:05 退出后未重启。3 个有 reward-exit 锁定的市场在这期间无做市。当前已 4 个市场被 banned（banned_markets.json 含 4 个 cid）。

6. **ℹ️ Aug 12 新信号**：Bot 在 Aug 12 00:00-05:05 期间正常运行，触发了一次完整的 batch 过程（bdbee396），TAKE 成功（2.5s 端到端）、SELL 阶段正常循环挂单（每 ~10.5 分钟刷新一次卖单）。

#### 长期趋势

| 指标 | R129 | R130 | R131 | R132 | R133 | 趋势 |
|------|------|------|------|------|------|------|
| 总 batch 数 | 3 | 3 | 3 | 3 | 3 | → |
| TAKE 成功 (SEALED) | 1/3 | 1/3 | 1/3 | 1/3 | 1/3 | → |
| SELL 成功 | 0/3 | 0/3 | 0/3 | 0/3 | 0/3 | → |
| Bot 在线 | ✅ | ❌ 离线 | ❌ 离线 | ❌ 离线 | ❌ 离线 | → |
| metrics.db | ✅ | ❌ corrupted | ❌ corrupted | ❌ corrupted | ❌ corrupted | → |
| UNLOCK 事件 | 0 | 0 | 0 | 0 | 0 | → |

### 结论

Bot 持续离线。BAKE_TAKE_EXECUTED 计数已修正（R131/R132 的 grep 正则错误导致漏报）。Batch 1 和 2 的 EXECUTED 事件可能来自非 reward-exit 的普通成交被误归因——这解释了为什么它们有 EXECUTED 但从未 SEALED。Batch 3 (bdbee396) 是唯一真正完成了 TAKE→SEAL 流程的批次，其 SELL 阶段被 bot 退出中断，重启后需恢复卖单。Bot 重启前需先恢复 metrics.db。
