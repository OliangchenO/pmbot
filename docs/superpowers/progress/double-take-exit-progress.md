# Double-Take Exit 监控进度

> 监控任务：2026-08-10-reward-fill-double-take-exit-design.md 实现效果
> 创建时间：2026-08-10 19:50 BJT
> 最新监控：2026-08-11 05:21 BJT（第 39 轮，scheduled task double-take-exit-monitor）
> Round 15 代码审查修复：2026-08-11（四轮修复）
> 更新频率：scheduled task

---

## 第 39 轮（2026-08-11 05:21 BJT）— Scheduled Task double-take-exit-monitor

### 状态：Bot 离线 ~5.8h，状态完全冻结，与第 38 轮完全相同

Bot 自 2026-08-10 23:31 BJT 关闭后再未启动。无 2026-08-11 日志文件。数据库状态与上一轮完全相同，无任何新事件触发。

### 事件统计（累计，无变化）

| 事件 | 次数 |
|------|------|
| REWARD_EXIT_BATCH_OPENED | 2 |
| BATCH_TAKE_SUBMITTED | 205 |
| BATCH_TAKE_EXECUTED | 12 |
| MARKET_REWARD_EXIT_LOCKED | 4 |
| BATCH_TAKE_COMPLETED | 0 |
| BATCH_TAKE_PARTIAL | 0 |
| BATCH_SELL_PLACED | 0 |
| BATCH_SELL_PARTIAL | 0 |
| REWARD_EXIT_BATCH_CLOSED | 0 |
| MARKET_REWARD_EXIT_UNLOCKED | 0 |
| BATCH_MANUAL_HOLD | 0 |
| BATCH_LOSS_LOCKED | 0 |
| API "invalid amounts" 错误 | 138 |
| API "not enough balance" 错误 | 248 |
| API 错误总计 | 386 |

### 数据库状态（无变化）

| 表 | 行数 | 详情 |
|---|------|------|
| reward_exit_batches | 2 | 均为 TAKE_PENDING, take_filled_size=0.0, exit_filled_size=0.0, updated_ts 未变化 |
| reward_exit_fills | 2 | 两个 origin fills（fill_id: 1e5af08f/02cafdd7, batch_id 均为空字符串），intent=normal_reward, side=BUY |
| reward_exit_orders | 0 | FAK 失败不记录 |

### Batch 详情（无变化）

| Batch ID | 市场 | 原始成交 | take_target | SUBMITTED | EXECUTED | 错误类型 | 创建 | 孤儿时长 |
|----------|------|----------|-------------|-----------|----------|----------|------|----------|
| reward-exit-1e5af08f | Will Ralph Norman place second... | BUY NO 3.66 @ 0.52 | 7.32 | 91 | 11 | invalid amounts → not enough balance | 22:19 | ~7.0h |
| reward-exit-02cafdd7 | Will MrBeast's next video... | BUY YES 43.9 @ 0.57 | 87.8 | 116 | 1 | not enough balance | 23:12 | ~6.1h |

### 异常信号检测

🔴 **Bot 离线 ~5.8h**：自 2026-08-10 23:31 关闭（HARD KILL + "正在退出，撤销全部订单"）。无 2026-08-11 日志文件。
🔴 **两个 TAKE_PENDING batch 持续孤儿化**：batch1 ~7.0h，batch2 ~6.1h。DB updated_ts 仍为创建时刻，从未更新。
🔴 **两个 P0 Bug 未修复**：浮点精度（Bug #1）+ 余额不足（Bug #2），205 次 SUBMITTED 中 91.2% 失败（193 次吃单失败）。
🔴 **BATCH_TAKE_EXECUTED 填量丢失**：日志 12 次 EXECUTED，但 DB take_filled_size 全为 0.0（tracking bug）。
🔴 **市场锁永不解锁**：MARKET_REWARD_EXIT_UNLOCKED 仍为 0。
🔴 **Fill-to-batch 链接缺失**：reward_exit_fills.batch_id 为空。
🔴 **零订单记录**：reward_exit_orders = 0，FAK 失败不可追溯。
✅ 无重复 fill_id、无 MANUAL_HOLD、无 BATCH_LOSS_LOCKED
✅ Batch ID 互斥 — 无重复 fill_id 关联到多个 batch

### 结论

第 39 轮监控确认状态仍然完全冻结。bot 离线超 5.8 小时，两个 batch 持续孤儿化。修复优先顺序不变：

1. **P0**: 修复 `taker_buy()` 浮点精度（market-order builder 按 collateral 金额报价）
2. **P0**: take 前检查余额，余额不足时降级为 `size = floor(balance / price)`
3. **P1**: batch 超时自动关闭（terminal_after_secs 到期后 close + unlock）
4. **P1**: bot 启动时清理僵死 batch + 修复 fill-to-batch 链接 + TAKE_EXECUTED 后更新 take_filled_size + 记录 FAK 失败订单到 reward_exit_orders

---

## 第 38 轮（2026-08-11 05:06 BJT）— Scheduled Task double-take-exit-monitor

### 状态：Bot 离线 ~5.6h，状态完全冻结，与第 37 轮完全相同

Bot 自 2026-08-10 23:31 BJT 关闭后再未启动。无 2026-08-11 日志文件。数据库状态与上一轮完全相同，无任何新事件触发。

### 事件统计（累计，无变化）

| 事件 | 次数 |
|------|------|
| REWARD_EXIT_BATCH_OPENED | 2 |
| BATCH_TAKE_SUBMITTED | 205 |
| BATCH_TAKE_EXECUTED | 12 |
| MARKET_REWARD_EXIT_LOCKED | 4 |
| BATCH_TAKE_COMPLETED | 0 |
| BATCH_TAKE_PARTIAL | 0 |
| BATCH_SELL_PLACED | 0 |
| BATCH_SELL_PARTIAL | 0 |
| REWARD_EXIT_BATCH_CLOSED | 0 |
| MARKET_REWARD_EXIT_UNLOCKED | 0 |
| BATCH_MANUAL_HOLD | 0 |
| BATCH_LOSS_LOCKED | 0 |
| API "invalid amounts" 错误 | 138 |
| API "not enough balance" 错误 | 248 |
| API 错误总计 | 386 |

### 数据库状态（无变化）

| 表 | 行数 | 详情 |
|---|------|------|
| reward_exit_batches | 2 | 均为 TAKE_PENDING, take_filled_size=0.0, exit_filled_size=0.0, updated_ts 未变化 |
| reward_exit_fills | 2 | 两个 origin fills（fill_id: 1e5af08f/02cafdd7, batch_id 均为空字符串），intent=normal_reward, side=BUY |
| reward_exit_orders | 0 | FAK 失败不记录 |

### Batch 详情（无变化）

| Batch ID | 市场 | 原始成交 | take_target | SUBMITTED | EXECUTED | 错误类型 | 创建 | 孤儿时长 |
|----------|------|----------|-------------|-----------|----------|----------|------|----------|
| reward-exit-1e5af08f | Will Ralph Norman place second... | BUY NO 3.66 @ 0.52 | 7.32 | 91 | 11 | invalid amounts → not enough balance | 22:19 | ~6.8h |
| reward-exit-02cafdd7 | Will MrBeast's next video... | BUY YES 43.9 @ 0.57 | 87.8 | 116 | 1 | not enough balance | 23:12 | ~5.9h |

### 异常信号检测

🔴 **Bot 离线 ~5.6h**：自 2026-08-10 23:31 关闭（HARD KILL + "正在退出，撤销全部订单"）。无 2026-08-11 日志文件。
🔴 **两个 TAKE_PENDING batch 持续孤儿化**：batch1 ~6.8h，batch2 ~5.9h。DB updated_ts 仍为创建时刻，从未更新。
🔴 **两个 P0 Bug 未修复**：浮点精度（Bug #1）+ 余额不足（Bug #2），205 次 SUBMITTED 中 91.2% 失败（193 次吃单失败）。
🔴 **BATCH_TAKE_EXECUTED 填量丢失**：日志 12 次 EXECUTED，但 DB take_filled_size 全为 0.0（tracking bug）。
🔴 **市场锁永不解锁**：MARKET_REWARD_EXIT_UNLOCKED 仍为 0。
🔴 **Fill-to-batch 链接缺失**：reward_exit_fills.batch_id 为空。
🔴 **零订单记录**：reward_exit_orders = 0，FAK 失败不可追溯。
✅ 无重复 fill_id、无 MANUAL_HOLD、无 BATCH_LOSS_LOCKED
✅ Batch ID 互斥 — 无重复 fill_id 关联到多个 batch

### 结论

第 38 轮监控确认状态仍然完全冻结。bot 离线超 5.6 小时，两个 batch 持续孤儿化。修复优先顺序不变：

1. **P0**: 修复 `taker_buy()` 浮点精度（market-order builder 按 collateral 金额报价）
2. **P0**: take 前检查余额，余额不足时降级为 `size = floor(balance / price)`
3. **P1**: batch 超时自动关闭（terminal_after_secs 到期后 close + unlock）
4. **P1**: bot 启动时清理僵死 batch + 修复 fill-to-batch 链接 + TAKE_EXECUTED 后更新 take_filled_size + 记录 FAK 失败订单到 reward_exit_orders

---

## 第 37 轮（2026-08-11 04:51 BJT）— Scheduled Task double-take-exit-monitor

### 状态：Bot 离线 ~5.3h，完全冻结，与第 36 轮完全相同

Bot 自 2026-08-10 23:31 BJT 关闭后再未启动。无 2026-08-11 日志文件。数据库状态与第 36 轮完全相同，无任何新事件触发。仍仅使用 2026-08-10 日志进行累计统计。

### 事件统计（累计，无变化）

| 事件 | 次数 |
|------|------|
| REWARD_EXIT_BATCH_OPENED | 2 |
| BATCH_TAKE_SUBMITTED | 205 |
| BATCH_TAKE_EXECUTED | 12 |
| MARKET_REWARD_EXIT_LOCKED | 4 |
| BATCH_TAKE_COMPLETED | 0 |
| BATCH_TAKE_PARTIAL | 0 |
| BATCH_SELL_PLACED | 0 |
| BATCH_SELL_PARTIAL | 0 |
| REWARD_EXIT_BATCH_CLOSED | 0 |
| MARKET_REWARD_EXIT_UNLOCKED | 0 |
| BATCH_MANUAL_HOLD | 0 |
| BATCH_LOSS_LOCKED | 0 |
| API "吃单订单失败" 错误 | 193 |

### 数据库状态（无变化）

| 表 | 行数 | 详情 |
|---|------|------|
| reward_exit_batches | 2 | 均为 TAKE_PENDING, take_filled_size=0.0, exit_filled_size=0.0 |
| reward_exit_fills | 2 | intent=normal_reward, side=BUY, batch_id 均为空字符串 |
| reward_exit_orders | 0 | FAK 失败不记录到 DB |

### Batch 详情（无变化）

| Batch ID | 市场 | 原始成交 | target | SUBMITTED | EXECUTED | 主要错误 | 创建 | 孤儿时长 |
|----------|------|----------|--------|-----------|----------|----------|------|----------|
| reward-exit-1e5af08f | Will Ralph Norman place second... | BUY NO 3.66 @ 0.52 | 7.32 | 90 | 10 | invalid amounts → not enough balance | 22:19 | ~6.5h |
| reward-exit-02cafdd7 | Will MrBeast's next video... | BUY YES 43.9 @ 0.57 | 87.8 | 115 | 2 | not enough balance | 23:12 | ~5.6h |

### 异常信号检测

🔴 **Bot 离线 ~5.3h**：自 2026-08-10 23:31 关闭（HARD KILL + "正在退出，撤销全部订单"）。无 2026-08-11 日志文件。
🔴 **两个 TAKE_PENDING batch 孤儿化**：batch1 ~6.5h，batch2 ~5.6h。DB updated_ts 仍为创建时刻，从未更新。
🔴 **两个 P0 Bug 未修复**：浮点精度（Bug #1）+ 余额不足（Bug #2），205 次 SUBMITTED 中 91.2% 失败（193 次吃单失败）。
🔴 **BATCH_TAKE_EXECUTED 填量丢失**：日志 12 次 EXECUTED，DB take_filled_size 全为 0.0。
🔴 **市场锁永不解锁**：MARKET_REWARD_EXIT_UNLOCKED 仍为 0。
🔴 **Fill-to-batch 链接缺失**：reward_exit_fills.batch_id 均为空字符串。
🔴 **零订单记录**：reward_exit_orders = 0，FAK 失败订单不记录到 DB。
✅ 无 MANUAL_HOLD、无 BATCH_LOSS_LOCKED
✅ 无重复 fill_id 关联
✅ 同一 fill_id 只关联到一个 batch_id

### 本轮备注

第 37 轮与第 29-36 轮完全相同的冻结状态。错误计数与第 36 轮一致（193 次吃单失败）。实际业务含义无变化：两个 batch 都因余额耗尽或浮点精度问题导致所有 FAK BUY 被 API 拒绝。

### 结论

Bot 离线 ~5.3h，两个 batch 创建后约 5.6–6.5h 仍是 TAKE_PENDING。当前日期（2026-08-11）无日志文件产生。修复优先顺序不变：

1. **P0**: 修复 `taker_buy()` 浮点精度（market-order builder 按 collateral 金额报价）
2. **P0**: take 前检查余额，余额不足时降级为 `size = floor(balance / price)`
3. **P1**: batch 超时自动关闭（terminal_after_secs 到期后 close + unlock，解除市场锁）
4. **P1**: bot 启动时清理僵死 batch + 修复 fill-to-batch 链接 + TAKE_EXECUTED 后更新 take_filled_size + 记录 FAK 失败订单到 reward_exit_orders

---

## 第 36 轮（2026-08-11 04:37 BJT）— Scheduled Task double-take-exit-monitor

### 状态：Bot 离线 ~5.1h，完全冻结，与第 35 轮完全相同

Bot 自 2026-08-10 23:31 BJT 关闭后再未启动。无 2026-08-11 日志文件。数据库状态与第 35 轮完全相同，无任何新事件触发。仅使用 2026-08-10 日志进行累计统计。

### 事件统计（累计，无变化）

| 事件 | 次数 |
|------|------|
| REWARD_EXIT_BATCH_OPENED | 2 |
| BATCH_TAKE_SUBMITTED | 205 |
| BATCH_TAKE_EXECUTED | 12 |
| MARKET_REWARD_EXIT_LOCKED | 4 |
| BATCH_TAKE_COMPLETED | 0 |
| BATCH_TAKE_PARTIAL | 0 |
| BATCH_SELL_PLACED | 0 |
| BATCH_SELL_PARTIAL | 0 |
| REWARD_EXIT_BATCH_CLOSED | 0 |
| MARKET_REWARD_EXIT_UNLOCKED | 0 |
| BATCH_MANUAL_HOLD | 0 |
| BATCH_LOSS_LOCKED | 0 |
| API "invalid amounts" 错误 | 69 |
| API "not enough balance" 错误 | 124 |
| API 错误总计（吃单订单失败）| 193 |

### 数据库状态（无变化）

| 表 | 行数 | 详情 |
|---|------|------|
| reward_exit_batches | 2 | 均为 TAKE_PENDING, take_filled_size=0.0, exit_filled_size=0.0 |
| reward_exit_fills | 2 | intent=normal_reward, side=BUY, batch_id 均为空字符串 |
| reward_exit_orders | 0 | FAK 失败不记录到 DB |

### Batch 详情（无变化）

| Batch ID | 市场 | 原始成交 | target | SUBMITTED | EXECUTED | 主要错误 | 创建 | 孤儿时长 |
|----------|------|----------|--------|-----------|----------|----------|------|----------|
| reward-exit-1e5af08f | Will Ralph Norman place second... | BUY NO 3.66 @ 0.52 | 7.32 | 90 | 10 | invalid amounts → not enough balance | 22:19 | ~6.3h |
| reward-exit-02cafdd7 | Will MrBeast's next video... | BUY YES 43.9 @ 0.57 | 87.8 | 115 | 2 | not enough balance | 23:12 | ~5.4h |

### 异常信号检测

🔴 **Bot 离线 ~5.1h**：自 2026-08-10 23:31 关闭（HARD KILL + "正在退出，撤销全部订单"）。无 2026-08-11 日志文件。
🔴 **两个 TAKE_PENDING batch 孤儿化**：batch1 ~6.3h，batch2 ~5.4h。DB updated_ts 仍为创建时刻，从未更新。
🔴 **两个 P0 Bug 未修复**：浮点精度（Bug #1）+ 余额不足（Bug #2），205 次 SUBMITTED 中 91.2% 失败（193 次吃单失败）。
🔴 **BATCH_TAKE_EXECUTED 填量丢失**：日志 12 次 EXECUTED，DB take_filled_size 全为 0.0。
🔴 **市场锁永不解锁**：MARKET_REWARD_EXIT_UNLOCKED 仍为 0。
🔴 **Fill-to-batch 链接缺失**：reward_exit_fills.batch_id 均为空字符串。
🔴 **零订单记录**：reward_exit_orders = 0，FAK 失败订单不记录到 DB。
✅ 无 MANUAL_HOLD、无 BATCH_LOSS_LOCKED
✅ 无重复 fill_id 关联
✅ 同一 fill_id 只关联到一个 batch_id

### 本轮备注

第 36 轮与第 29-35 轮完全相同的冻结状态。错误计数因 grep 方式不同略有差异（193 vs 386 之前，本次仅统计 "吃单订单失败" 行）。实际业务含义无变化：两个 batch 都因余额耗尽或浮点精度问题导致所有 FAK BUY 被 API 拒绝。

### 结论

Bot 离线 ~5.1h，两个 batch 创建后约 5.4–6.3h 仍是 TAKE_PENDING。当前日期（2026-08-11）无日志文件产生。修复优先顺序不变：

1. **P0**: 修复 `taker_buy()` 浮点精度（market-order builder 按 collateral 金额报价）
2. **P0**: take 前检查余额，余额不足时降级为 `size = floor(balance / price)`
3. **P1**: batch 超时自动关闭（terminal_after_secs 到期后 close + unlock，解除市场锁）
4. **P1**: bot 启动时清理僵死 batch + 修复 fill-to-batch 链接 + TAKE_EXECUTED 后更新 take_filled_size + 记录 FAK 失败订单到 reward_exit_orders

---

## 第 35 轮（2026-08-11 04:20 BJT）— Scheduled Task double-take-exit-monitor

### 状态：Bot 离线 ~4.9h，完全冻结，与第 34 轮完全相同

Bot 自 2026-08-10 23:31 BJT 关闭后再未启动。无 2026-08-11 日志文件。数据库状态与第 34 轮完全相同，无任何新事件触发。

### 事件统计（累计，无变化）

| 事件 | 次数 |
|------|------|
| REWARD_EXIT_BATCH_OPENED | 2 |
| BATCH_TAKE_SUBMITTED | 205 |
| BATCH_TAKE_EXECUTED | 12 |
| MARKET_REWARD_EXIT_LOCKED | 4 |
| BATCH_TAKE_COMPLETED | 0 |
| BATCH_TAKE_PARTIAL | 0 |
| BATCH_SELL_PLACED | 0 |
| BATCH_SELL_PARTIAL | 0 |
| REWARD_EXIT_BATCH_CLOSED | 0 |
| MARKET_REWARD_EXIT_UNLOCKED | 0 |
| BATCH_MANUAL_HOLD | 0 |
| BATCH_LOSS_LOCKED | 0 |
| API "invalid amounts" 错误 | 138 |
| API "not enough balance" 错误 | 248 |
| API 错误总计 | 386 |

### 数据库状态（无变化）

| 表 | 行数 | 详情 |
|---|------|------|
| reward_exit_batches | 2 | 均为 TAKE_PENDING, take_filled_size=0.0, exit_filled_size=0.0 |
| reward_exit_fills | 2 | intent=normal_reward, side=BUY, batch_id 均为空字符串 |
| reward_exit_orders | 0 | FAK 失败不记录到 DB |

### Batch 详情（无变化）

| Batch ID | 市场 | 原始成交 | target | SUBMITTED | EXECUTED | 主要错误 | 创建 | 孤儿时长 |
|----------|------|----------|--------|-----------|----------|----------|------|----------|
| reward-exit-1e5af08f | Will Ralph Norman place second... | BUY NO 3.66 @ 0.52 | 7.32 | 90 | 10 | invalid amounts → not enough balance | 22:19 | ~6.0h |
| reward-exit-02cafdd7 | Will MrBeast's next video... | BUY YES 43.9 @ 0.57 | 87.8 | 115 | 2 | not enough balance | 23:12 | ~5.1h |

### 异常信号检测

🔴 **Bot 离线 ~4.9h**：自 2026-08-10 23:31 关闭（HARD KILL + "正在退出，撤销全部订单"）。无 2026-08-11 日志文件。
🔴 **两个 TAKE_PENDING batch 孤儿化**：batch1 ~6.0h，batch2 ~5.1h。DB updated_ts 仍为创建时刻，从未更新。
🔴 **两个 P0 Bug 未修复**：浮点精度（Bug #1）+ 余额不足（Bug #2），205 次 SUBMITTED 中 91.2% 失败。
🔴 **BATCH_TAKE_EXECUTED 填量丢失**：日志 12 次 EXECUTED（filled=14.80 × 10 + 170.49 + 175.12），DB take_filled_size 全为 0.0。
🔴 **市场锁永不解锁**：MARKET_REWARD_EXIT_UNLOCKED 仍为 0。
🔴 **Fill-to-batch 链接缺失**：reward_exit_fills.batch_id 均为空字符串。
🔴 **零订单记录**：reward_exit_orders = 0，FAK 失败订单不记录到 DB。
🔴 **HARD KILL 触发**：08:55 BJT 时 HARD KILL（当日亏损 $22.68 >= $20.00），之后多次重启/退出循环。最终 23:31 退出后未再启动。
✅ 无 MANUAL_HOLD、无 BATCH_LOSS_LOCKED
✅ 无重复 fill_id 关联
✅ 同一 fill_id 只关联到一个 batch_id

### 结论

与第 28-34 轮完全相同的冻结状态。bot 离线 ~4.9h，两个 batch 创建后约 5.1–6.0h 仍是 TAKE_PENDING。当前日期（2026-08-11）无日志文件产生。修复优先顺序不变：

1. **P0**: 修复 `taker_buy()` 浮点精度（market-order builder 按 collateral 金额报价）
2. **P0**: take 前检查余额，余额不足时降级为 `size = floor(balance / price)`
3. **P1**: batch 超时自动关闭（terminal_after_secs 到期后 close + unlock，解除市场锁）
4. **P1**: bot 启动时清理僵死 batch + 修复 fill-to-batch 链接 + TAKE_EXECUTED 后更新 take_filled_size + 记录 FAK 失败订单到 reward_exit_orders

---

## 第 34 轮（2026-08-11 04:05 BJT）— Scheduled Task double-take-exit-monitor

### 状态：Bot 离线 ~4.6h，完全冻结，与第 33 轮完全相同

Bot 自 2026-08-10 23:31 BJT 关闭后再未启动。无 2026-08-11 日志文件。数据库状态与第 33 轮完全相同，无任何新事件触发。

### 事件统计（累计，无变化）

| 事件 | 次数 |
|------|------|
| REWARD_EXIT_BATCH_OPENED | 2 |
| BATCH_TAKE_SUBMITTED | 205 |
| BATCH_TAKE_EXECUTED | 12 |
| MARKET_REWARD_EXIT_LOCKED | 4 |
| BATCH_TAKE_COMPLETED | 0 |
| BATCH_TAKE_PARTIAL | 0 |
| BATCH_SELL_PLACED | 0 |
| BATCH_SELL_PARTIAL | 0 |
| REWARD_EXIT_BATCH_CLOSED | 0 |
| MARKET_REWARD_EXIT_UNLOCKED | 0 |
| BATCH_MANUAL_HOLD | 0 |
| BATCH_LOSS_LOCKED | 0 |
| API "invalid amounts" 错误 | 138 |
| API "not enough balance" 错误 | 248 |
| API 错误总计 | 386 |

### 数据库状态（无变化）

| 表 | 行数 | 详情 |
|---|------|------|
| reward_exit_batches | 2 | 均为 TAKE_PENDING, take_filled_size=0.0, exit_filled_size=0.0 |
| reward_exit_fills | 2 | intent=normal_reward, side=BUY, batch_id 均为空字符串 |
| reward_exit_orders | 0 | FAK 失败不记录到 DB |

### Batch 详情（无变化）

| Batch ID | 市场 | 原始成交 | target | SUBMITTED | EXECUTED | 主要错误 | 创建 | 孤儿时长 |
|----------|------|----------|--------|-----------|----------|----------|------|----------|
| reward-exit-1e5af08f | Will Ralph Norman place second... | BUY NO 3.66 @ 0.52 | 7.32 | 90 | 10 | invalid amounts → not enough balance | 22:19 | ~5.8h |
| reward-exit-02cafdd7 | Will MrBeast's next video... | BUY YES 43.9 @ 0.57 | 87.8 | 115 | 2 | not enough balance | 23:12 | ~4.9h |

### 异常信号检测

🔴 **Bot 离线 ~4.6h**：自 2026-08-10 23:31 关闭（HARD KILL + "正在退出，撤销全部订单"）。无 2026-08-11 日志文件。
🔴 **两个 TAKE_PENDING batch 孤儿化**：batch1 ~5.8h，batch2 ~4.9h。DB updated_ts 仍为创建时刻，从未更新。
🔴 **两个 P0 Bug 未修复**：浮点精度（Bug #1）+ 余额不足（Bug #2），205 次 SUBMITTED 中 91.2% 失败。
🔴 **BATCH_TAKE_EXECUTED 填量丢失**：日志 12 次 EXECUTED（filled=14.80 × 10 + 170.49 + 175.12），DB take_filled_size 全为 0.0。
🔴 **市场锁永不解锁**：MARKET_REWARD_EXIT_UNLOCKED 仍为 0。
🔴 **Fill-to-batch 链接缺失**：reward_exit_fills.batch_id 均为空字符串。
🔴 **零订单记录**：reward_exit_orders = 0，FAK 失败订单不记录到 DB。
🔴 **HARD KILL 触发**：08:55 BJT 时 HARD KILL（当日亏损 $22.68 >= $20.00），之后多次重启/退出循环。最终 23:31 退出后未再启动。
✅ 无 MANUAL_HOLD、无 BATCH_LOSS_LOCKED
✅ 无重复 fill_id 关联
✅ 同一 fill_id 只关联到一个 batch_id

### 结论

与第 28-33 轮完全相同的冻结状态。bot 离线 ~4.6h，两个 batch 创建后约 4.9–5.8h 仍是 TAKE_PENDING。当前日期（2026-08-11）无日志文件产生。修复优先顺序不变：

1. **P0**: 修复 `taker_buy()` 浮点精度（market-order builder 按 collateral 金额报价）
2. **P0**: take 前检查余额，余额不足时降级为 `size = floor(balance / price)`
3. **P1**: batch 超时自动关闭（terminal_after_secs 到期后 close + unlock，解除市场锁）
4. **P1**: bot 启动时清理僵死 batch + 修复 fill-to-batch 链接 + TAKE_EXECUTED 后更新 take_filled_size + 记录 FAK 失败订单到 reward_exit_orders

---

## 第 32 轮（2026-08-11 03:36 BJT）— Scheduled Task double-take-exit-monitor

### 状态：Bot 离线 ~4.1h，完全冻结，与第 31 轮完全相同

Bot 自 2026-08-10 23:31 BJT 关闭后再未启动。无 2026-08-11 日志文件。数据库状态与第 31 轮完全相同，无任何新事件触发。

### 事件统计（累计，无变化）

| 事件 | 次数 |
|------|------|
| REWARD_EXIT_BATCH_OPENED | 2 |
| BATCH_TAKE_SUBMITTED | 205 |
| BATCH_TAKE_EXECUTED | 12 |
| MARKET_REWARD_EXIT_LOCKED | 4 |
| BATCH_TAKE_COMPLETED | 0 |
| BATCH_TAKE_PARTIAL | 0 |
| BATCH_SELL_PLACED | 0 |
| BATCH_SELL_PARTIAL | 0 |
| REWARD_EXIT_BATCH_CLOSED | 0 |
| MARKET_REWARD_EXIT_UNLOCKED | 0 |
| BATCH_MANUAL_HOLD | 0 |
| BATCH_LOSS_LOCKED | 0 |
| API "invalid amounts" 错误 | 138 |
| API "not enough balance" 错误 | 248 |
| API 错误总计 | 386 |

### 数据库状态（无变化）

| 表 | 行数 | 详情 |
|---|------|------|
| reward_exit_batches | 2 | 均为 TAKE_PENDING, take_filled_size=0.0, exit_filled_size=0.0 |
| reward_exit_fills | 2 | intent=normal_reward, side=BUY, batch_id 均为空字符串 |
| reward_exit_orders | 0 | FAK 失败不记录到 DB |

### Batch 详情（无变化）

| Batch ID | 市场 | 原始成交 | target | SUBMITTED | EXECUTED | 主要错误 | 创建 | 孤儿时长 |
|----------|------|----------|--------|-----------|----------|----------|------|----------|
| reward-exit-1e5af08f | Will Ralph Norman place second... | BUY NO 3.66 @ 0.52 | 7.32 | 90 | 10 | invalid amounts → not enough balance | 22:19 | ~5.3h |
| reward-exit-02cafdd7 | Will MrBeast's next video... | BUY YES 43.9 @ 0.57 | 87.8 | 115 | 2 | not enough balance | 23:12 | ~4.4h |

### 异常信号检测

🔴 **Bot 离线 ~4.1h**：自 2026-08-10 23:31 关闭（HARD KILL + "正在退出，撤销全部订单"）。无 2026-08-11 日志文件。
🔴 **两个 TAKE_PENDING batch 孤儿化**：batch1 ~5.3h，batch2 ~4.4h。DB updated_ts 仍为创建时刻，从未更新。
🔴 **两个 P0 Bug 未修复**：浮点精度（Bug #1）+ 余额不足（Bug #2），205 次 SUBMITTED 中 91.2% 失败。
🔴 **BATCH_TAKE_EXECUTED 填量丢失**：日志 12 次 EXECUTED（filled=14.80 × 10 + 170.49 + 175.12），DB take_filled_size 全为 0.0。
🔴 **市场锁永不解锁**：MARKET_REWARD_EXIT_UNLOCKED 仍为 0。
🔴 **Fill-to-batch 链接缺失**：reward_exit_fills.batch_id 均为空字符串。
🔴 **零订单记录**：reward_exit_orders = 0，FAK 失败订单不记录到 DB。
🔴 **HARD KILL 触发**：08:55 BJT 时 HARD KILL（当日亏损 $22.68 >= $20.00），之后多次重启/退出循环。最终 23:31 退出后未再启动。
✅ 无 MANUAL_HOLD、无 BATCH_LOSS_LOCKED
✅ 无重复 fill_id 关联
✅ 同一 fill_id 只关联到一个 batch_id

### 结论

与第 29-31 轮完全相同的冻结状态。bot 离线 ~4.1h，两个 batch 创建后约 4.4–5.3h 仍是 TAKE_PENDING。当前日期（2026-08-11）无日志文件产生。修复优先顺序不变：

1. **P0**: 修复 `taker_buy()` 浮点精度（market-order builder 按 collateral 金额报价）
2. **P0**: take 前检查余额，余额不足时降级为 `size = floor(balance / price)`
3. **P1**: batch 超时自动关闭（terminal_after_secs 到期后 close + unlock，解除市场锁）
4. **P1**: bot 启动时清理僵死 batch + 修复 fill-to-batch 链接 + TAKE_EXECUTED 后更新 take_filled_size + 记录 FAK 失败订单到 reward_exit_orders

---

## 第 31 轮（2026-08-11 03:20 BJT）— Scheduled Task double-take-exit-monitor

### 状态：Bot 离线 ~3.8h，完全冻结，与第 30 轮完全相同

Bot 自 2026-08-10 23:31 BJT 关闭后再未启动。无 2026-08-11 日志文件。数据库状态与第 30 轮完全相同，无任何新事件触发。

### 事件统计（累计，无变化）

| 事件 | 次数 |
|------|------|
| REWARD_EXIT_BATCH_OPENED | 2 |
| BATCH_TAKE_SUBMITTED | 205 |
| BATCH_TAKE_EXECUTED | 12 |
| MARKET_REWARD_EXIT_LOCKED | 4 |
| BATCH_TAKE_COMPLETED | 0 |
| BATCH_TAKE_PARTIAL | 0 |
| BATCH_SELL_PLACED | 0 |
| BATCH_SELL_PARTIAL | 0 |
| REWARD_EXIT_BATCH_CLOSED | 0 |
| MARKET_REWARD_EXIT_UNLOCKED | 0 |
| BATCH_MANUAL_HOLD | 0 |
| BATCH_LOSS_LOCKED | 0 |

### 数据库状态（无变化）

| 表 | 行数 | 详情 |
|---|------|------|
| reward_exit_batches | 2 | 均为 TAKE_PENDING, take_filled_size=0.0, exit_filled_size=0.0 |
| reward_exit_fills | 2 | intent=normal_reward, side=BUY, batch_id 均为空字符串 |
| reward_exit_orders | 0 | FAK 失败不记录到 DB |

### Batch 详情（无变化）

| Batch ID | 市场 | 原始成交 | target | SUBMITTED | EXECUTED | 主要错误 | 创建 | 孤儿时长 |
|----------|------|----------|--------|-----------|----------|----------|------|----------|
| reward-exit-1e5af08f | Will Ralph Norman place second... | BUY NO 3.66 @ 0.52 | 7.32 | 90 | 10 | invalid amounts → not enough balance | 22:19 | ~5.0h |
| reward-exit-02cafdd7 | Will MrBeast's next video... | BUY YES 43.9 @ 0.57 | 87.8 | 115 | 2 | not enough balance | 23:12 | ~4.1h |

### 异常信号检测

🔴 **Bot 离线 ~3.8h**：自 2026-08-10 23:31 关闭（"正在退出，撤销全部订单"）。无 2026-08-11 日志文件。
🔴 **两个 TAKE_PENDING batch 孤儿化**：batch1 ~5.0h，batch2 ~4.1h。DB updated_ts 仍为创建时刻，从未更新。
🔴 **两个 P0 Bug 未修复**：浮点精度（Bug #1）+ 余额不足（Bug #2），205 次 SUBMITTED 中 91.2% 失败。
🔴 **BATCH_TAKE_EXECUTED 填量丢失**：日志 12 次 EXECUTED（filled=14.80 × 10 + 170.49 + 175.12），DB take_filled_size 全为 0.0。
🔴 **市场锁永不解锁**：MARKET_REWARD_EXIT_UNLOCKED 仍为 0。
🔴 **Fill-to-batch 链接缺失**：reward_exit_fills.batch_id 均为空字符串。
🔴 **零订单记录**：reward_exit_orders = 0，FAK 失败订单不记录到 DB。
✅ 无 MANUAL_HOLD、无 BATCH_LOSS_LOCKED
✅ 无重复 fill_id 关联
✅ 同一 fill_id 只关联到一个 batch_id

### 结论

与第 29-30 轮完全相同的冻结状态。bot 离线 ~3.8h，两个 batch 创建后约 4.1–5.0h 仍是 TAKE_PENDING。修复优先顺序不变：

1. **P0**: 修复 `taker_buy()` 浮点精度（market-order builder 按 collateral 金额报价）
2. **P0**: take 前检查余额，余额不足时降级为 `size = floor(balance / price)`
3. **P1**: batch 超时自动关闭（terminal_after_secs 到期后 close + unlock，解除市场锁）
4. **P1**: bot 启动时清理僵死 batch + 修复 fill-to-batch 链接 + TAKE_EXECUTED 后更新 take_filled_size + 记录 FAK 失败订单到 reward_exit_orders

---

---

## 第 30 轮（2026-08-11 03:06 BJT）— Scheduled Task double-take-exit-monitor

### 状态：Bot 离线 ~3.6h，完全冻结不变

Bot 自 2026-08-10 23:31 BJT 关闭后再未启动。无 2026-08-11 日志文件。数据库状态与第 29 轮完全相同，无任何新事件触发。

### 事件统计（累计，无变化）

| 事件 | 次数 |
|------|------|
| REWARD_EXIT_BATCH_OPENED | 2 |
| BATCH_TAKE_SUBMITTED | 205 |
| BATCH_TAKE_EXECUTED | 12 |
| MARKET_REWARD_EXIT_LOCKED | 4 |
| BATCH_TAKE_COMPLETED | 0 |
| BATCH_TAKE_PARTIAL | 0 |
| BATCH_SELL_PLACED | 0 |
| BATCH_SELL_PARTIAL | 0 |
| REWARD_EXIT_BATCH_CLOSED | 0 |
| MARKET_REWARD_EXIT_UNLOCKED | 0 |
| BATCH_MANUAL_HOLD | 0 |
| BATCH_LOSS_LOCKED | 0 |

### 数据库状态（无变化）

| 表 | 行数 | 详情 |
|---|------|------|
| reward_exit_batches | 2 | 均为 TAKE_PENDING, take_filled_size=0.0, exit_filled_size=0.0 |
| reward_exit_fills | 2 | intent=normal_reward, side=BUY, batch_id 均为空字符串 |
| reward_exit_orders | 0 | FAK 失败不记录到 DB |

### Batch 详情（无变化）

| Batch ID | 市场 | 原始成交 | target | SUBMITTED | EXECUTED | 主要错误 | 创建 | 孤儿时长 |
|----------|------|----------|--------|-----------|----------|----------|------|----------|
| reward-exit-1e5af08f | Will Ralph Norman place second... | BUY NO 3.66 @ 0.52 | 7.32 | 90 | 10 | invalid amounts → not enough balance | 22:19 | ~4.8h |
| reward-exit-02cafdd7 | Will MrBeast's next video... | BUY YES 43.9 @ 0.57 | 87.8 | 115 | 2 | not enough balance | 23:12 | ~3.9h |

### 异常信号检测

🔴 **Bot 离线 ~3.6h**：自 2026-08-10 23:31 关闭（"正在退出，撤销全部订单"）。无 2026-08-11 日志文件。
🔴 **两个 TAKE_PENDING batch 孤儿化**：batch1 ~4.8h，batch2 ~3.9h。DB updated_ts 仍为创建时刻。
🔴 **两个 P0 Bug 未修复**：浮点精度（Bug #1）+ 余额不足（Bug #2），205 次 SUBMITTED 中 91.2% 失败。
🔴 **BATCH_TAKE_EXECUTED 填量丢失**：日志 12 次 EXECUTED（filled=14.80 × 10 + 170.49 + 175.12），DB take_filled_size 全为 0.0。
🔴 **市场锁永不解锁**：MARKET_REWARD_EXIT_UNLOCKED 仍为 0。
🔴 **Fill-to-batch 链接缺失**：reward_exit_fills.batch_id 均为空字符串。
🔴 **零订单记录**：reward_exit_orders = 0，FAK 失败订单不记录到 DB。
✅ 无 MANUAL_HOLD、无 BATCH_LOSS_LOCKED
✅ 无重复 fill_id 关联
✅ 同一 fill_id 只关联到一个 batch_id

### 结论

与第 28-29 轮完全相同的冻结状态。bot 离线 ~3.6h，两个 batch 创建后约 3.9–4.8h 仍是 TAKE_PENDING。修复优先顺序不变：

1. **P0**: 修复 `taker_buy()` 浮点精度（market-order builder 按 collateral 金额报价）
2. **P0**: take 前检查余额，余额不足时降级为 `size = floor(balance / price)`
3. **P1**: batch 超时自动关闭（terminal_after_secs 到期后 close + unlock，解除市场锁）
4. **P1**: bot 启动时清理僵死 batch + 修复 fill-to-batch 链接 + TAKE_EXECUTED 后更新 take_filled_size + 记录 FAK 失败订单到 reward_exit_orders

---

## 第 29 轮（2026-08-11 02:51 BJT）— Scheduled Task double-take-exit-monitor

### 状态：Bot 离线 ~3.3h，完全冻结不变

Bot 自 2026-08-10 23:31 BJT 关闭后再未启动。无 2026-08-11 日志文件。数据库状态与第 28 轮完全相同，无任何新事件触发。仅使用 2026-08-10 日志进行累计统计。

### 事件统计（累计，无变化）

| 事件 | 次数 |
|------|------|
| REWARD_EXIT_BATCH_OPENED | 2 |
| BATCH_TAKE_SUBMITTED | 205 |
| BATCH_TAKE_EXECUTED | 12 |
| MARKET_REWARD_EXIT_LOCKED | 4 |
| BATCH_TAKE_COMPLETED | 0 |
| BATCH_TAKE_PARTIAL | 0 |
| BATCH_SELL_PLACED | 0 |
| BATCH_SELL_PARTIAL | 0 |
| REWARD_EXIT_BATCH_CLOSED | 0 |
| MARKET_REWARD_EXIT_UNLOCKED | 0 |
| BATCH_MANUAL_HOLD | 0 |
| BATCH_LOSS_LOCKED | 0 |
| API "invalid amounts" 错误 | 138 |
| API "not enough balance" 错误 | 248 |
| API 错误总计 | 386 |

### 数据库状态（无变化）

| 表 | 行数 | 详情 |
|---|------|------|
| reward_exit_batches | 2 | 均为 TAKE_PENDING, take_filled_size=0.0, exit_filled_size=0.0, updated_ts 未变化（1786371555 / 1786374761） |
| reward_exit_fills | 2 | 两个 origin fills（fill_id: 1e5af08f / 02cafdd7, batch_id 均为空字符串），intent=normal_reward, side=BUY |
| reward_exit_orders | 0 | FAK 失败不记录 |

### Batch 详情（无变化）

| Batch ID | 市场 | 原始成交 | take_target | SUBMITTED | EXECUTED | 错误类型 | 创建 | 孤儿时长 |
|----------|------|----------|-------------|-----------|----------|----------|------|----------|
| reward-exit-1e5af08f | Will Ralph Norman place second... | BUY NO 3.66 @ 0.52 | 7.32 | 90 | 10 | invalid amounts → not enough balance | 22:19 | ~4.5h |
| reward-exit-02cafdd7 | Will MrBeast's next video... | BUY YES 43.9 @ 0.57 | 87.8 | 115 | 2 | not enough balance | 23:12 | ~3.7h |

### 异常信号检测

🔴 **Bot 离线 ~3.3h**：自 2026-08-10 23:31 关闭（"正在退出，撤销全部订单"）。无 2026-08-11 日志文件。
🔴 **两个 TAKE_PENDING batch 孤儿化**：batch1 ~4.5h，batch2 ~3.7h 后无任何进展。DB updated_ts 仍为创建时刻（1786371555 和 1786374761）。
🔴 **两个 P0 Bug 未修复**：浮点精度（Bug #1）+ 余额不足（Bug #2），386 次 API 错误，205 次 SUBMITTED 中 91.2% 失败。
🔴 **BATCH_TAKE_EXECUTED 填量丢失**：日志 12 次 EXECUTED（filled=14.80 × 10 + 170.49 + 175.12），但 DB take_filled_size 全为 0.0（tracking bug）。
🔴 **市场锁永不解锁**：MARKET_REWARD_EXIT_UNLOCKED 仍为 0。两个市场的 reward_exit_locked 标记无法自动清理。
🔴 **Fill-to-batch 链接缺失**：reward_exit_fills.batch_id 均为空字符串，fill 无法回溯到 batch。
🔴 **零订单记录**：reward_exit_orders = 0 行，FAK 失败订单不被记录到数据库，排查困难。
✅ 无重复 fill_id（fill_id 在 reward_exit_fills 中唯一）
✅ 无 MANUAL_HOLD、无 BATCH_LOSS_LOCKED
✅ 无 REWARD_EXIT_BATCH_CLOSED（所有 batch 均无完整生命周期）
✅ 同一 fill_id 只关联到一个 batch_id（不存在重复关联）

### 结论

与第 28 轮完全相同的冻结状态。bot 离线 ~3.3h，两个 batch 自创建约 3.7–4.5h 后仍是 TAKE_PENDING 孤儿化。修复优先顺序不变：

1. **P0**: 修复 `taker_buy()` 浮点精度（market-order builder 按 collateral 金额报价）
2. **P0**: take 前检查余额，余额不足时降级为 `size = floor(balance / price)`
3. **P1**: batch 超时自动关闭（terminal_after_secs 到期后 close + unlock，解除市场锁）
4. **P1**: bot 启动时清理僵死 batch + 修复 fill-to-batch 链接 + TAKE_EXECUTED 后更新 take_filled_size + 记录 FAK 失败订单到 reward_exit_orders

---

## 第 28 轮（2026-08-11 13:00 BJT）— Scheduled Task double-take-exit-monitor

### 状态：Bot 离线 ~13.5h，完全冻结不变

Bot 自 2026-08-10 23:31 BJT 关闭后再未启动。无 2026-08-11 日志文件。数据库状态与第 27 轮完全相同，无任何新事件触发。仍仅使用 2026-08-10 日志进行累计统计。

### 事件统计（累计，无变化）

| 事件 | 次数 |
|------|------|
| REWARD_EXIT_BATCH_OPENED | 2 |
| BATCH_TAKE_SUBMITTED | 205 |
| BATCH_TAKE_EXECUTED | 12 |
| MARKET_REWARD_EXIT_LOCKED | 4 |
| BATCH_TAKE_COMPLETED | 0 |
| BATCH_TAKE_PARTIAL | 0 |
| BATCH_SELL_PLACED | 0 |
| BATCH_SELL_PARTIAL | 0 |
| REWARD_EXIT_BATCH_CLOSED | 0 |
| MARKET_REWARD_EXIT_UNLOCKED | 0 |
| BATCH_MANUAL_HOLD | 0 |
| BATCH_LOSS_LOCKED | 0 |
| API "invalid amounts" 错误 | 138 |
| API "not enough balance" 错误 | 248 |
| API 错误总计 | 386 |

### 数据库状态（无变化）

| 表 | 行数 | 详情 |
|---|------|------|
| reward_exit_batches | 2 | 均为 TAKE_PENDING, take_filled_size=0.0, exit_filled_size=0.0, updated_ts 未变化（1786371555 / 1786374761） |
| reward_exit_fills | 2 | 两个 origin fills（fill_id: 1e5af08f / 02cafdd7, batch_id 均为空字符串），intent=normal_reward, side=BUY |
| reward_exit_orders | 0 | FAK 失败不记录 |

### Batch 详情（无变化）

| Batch ID | 市场 | 原始成交 | take_target | SUBMITTED | EXECUTED | 错误类型 | 创建 | 孤儿时长 |
|----------|------|----------|-------------|-----------|----------|----------|------|----------|
| reward-exit-1e5af08f | Will Ralph Norman place second... | BUY NO 3.66 @ 0.52 | 7.32 | 90 | 10 | invalid amounts → not enough balance | 22:19 | ~14.7h |
| reward-exit-02cafdd7 | Will MrBeast's next video... | BUY YES 43.9 @ 0.57 | 87.8 | 115 | 2 | not enough balance | 23:12 | ~13.8h |

### 异常信号检测

🔴 **Bot 离线 ~13.5h**：自 2026-08-10 23:31 关闭（"正在退出，撤销全部订单"）。无 2026-08-11 日志文件。
🔴 **两个 TAKE_PENDING batch 孤儿化**：batch1 ~14.7h，batch2 ~13.8h 后无任何进展。DB updated_ts 仍为创建时刻（1786371555 和 1786374761）。
🔴 **两个 P0 Bug 未修复**：浮点精度（Bug #1）+ 余额不足（Bug #2），386 次 API 错误，205 次 SUBMITTED 中 91.2% 失败。
🔴 **BATCH_TAKE_EXECUTED 填量丢失**：日志 12 次 EXECUTED（filled=14.80 × 10 + 170.49 + 175.12），但 DB take_filled_size 全为 0.0（tracking bug）。
🔴 **市场锁永不解锁**：MARKET_REWARD_EXIT_UNLOCKED 仍为 0。两个市场的 reward_exit_locked 标记无法自动清理。
🔴 **Fill-to-batch 链接缺失**：reward_exit_fills.batch_id 均为空字符串，fill 无法回溯到 batch。
🔴 **零订单记录**：reward_exit_orders = 0 行，FAK 失败订单不被记录到数据库，排查困难。
✅ 无重复 fill_id（fill_id 在 reward_exit_fills 中唯一）
✅ 无 MANUAL_HOLD、无 BATCH_LOSS_LOCKED
✅ 无 REWARD_EXIT_BATCH_CLOSED（所有 batch 均无完整生命周期）
✅ 同一 fill_id 只关联到一个 batch_id（不存在重复关联）

### 结论

与第 27 轮完全相同的冻结状态。bot 离线 ~13.5h，两个 batch 自创建约 13.8–14.7h 后仍是 TAKE_PENDING 孤儿化。修复优先顺序不变：

1. **P0**: 修复 `taker_buy()` 浮点精度（market-order builder 按 collateral 金额报价）
2. **P0**: take 前检查余额，余额不足时降级为 `size = floor(balance / price)`
3. **P1**: batch 超时自动关闭（terminal_after_secs 到期后 close + unlock，解除市场锁）
4. **P1**: bot 启动时清理僵死 batch + 修复 fill-to-batch 链接 + TAKE_EXECUTED 后更新 take_filled_size + 记录 FAK 失败订单到 reward_exit_orders

---

## 第 27 轮（2026-08-11 11:00 BJT）— Scheduled Task double-take-exit-monitor

### 状态：Bot 离线 ~11.5h，完全冻结不变

Bot 自 2026-08-10 23:31 BJT 关闭后再未启动。无 2026-08-11 日志文件。仅使用 2026-08-10 日志进行累计统计。数据库状态与第 18–26 轮完全相同，无任何新事件触发。

### 事件统计（累计，无变化）

| 事件 | 次数 |
|------|------|
| REWARD_EXIT_BATCH_OPENED | 2 |
| BATCH_TAKE_SUBMITTED | 205 |
| BATCH_TAKE_EXECUTED | 12 |
| MARKET_REWARD_EXIT_LOCKED | 4 |
| BATCH_TAKE_COMPLETED | 0 |
| BATCH_TAKE_PARTIAL | 0 |
| BATCH_SELL_PLACED | 0 |
| BATCH_SELL_PARTIAL | 0 |
| REWARD_EXIT_BATCH_CLOSED | 0 |
| MARKET_REWARD_EXIT_UNLOCKED | 0 |
| BATCH_MANUAL_HOLD | 0 |
| BATCH_LOSS_LOCKED | 0 |
| API "invalid amounts" 错误 | 138 |
| API "not enough balance" 错误 | 248 |
| API 错误总计 | 386 |

### 数据库状态（无变化）

| 表 | 行数 | 详情 |
|---|------|------|
| reward_exit_batches | 2 | 均为 TAKE_PENDING, take_filled_size=0.0, exit_filled_size=0.0, updated_ts 未变化（1786371555 / 1786374761） |
| reward_exit_fills | 2 | 两个 origin fills（fill_id: 1e5af08f / 02cafdd7, batch_id 均为空字符串），intent=normal_reward, side=BUY |
| reward_exit_orders | 0 | FAK 失败不记录 |

### Batch 详情（无变化）

| Batch ID | 市场 | 原始成交 | take_target | SUBMITTED | EXECUTED | 错误类型 | 创建 | 孤儿时长 |
|----------|------|----------|-------------|-----------|----------|----------|------|----------|
| reward-exit-1e5af08f | Will Ralph Norman place second... | BUY NO 3.66 @ 0.52 | 7.32 | 90 | 10 | invalid amounts → not enough balance | 22:19 | ~12.7h |
| reward-exit-02cafdd7 | Will MrBeast's next video... | BUY YES 43.9 @ 0.57 | 87.8 | 115 | 2 | not enough balance | 23:12 | ~11.8h |

### 异常信号检测

🔴 **Bot 离线 ~11.5h**：自 2026-08-10 23:31 关闭（"正在退出，撤销全部订单"）。无 2026-08-11 日志文件。
🔴 **两个 TAKE_PENDING batch 孤儿化**：batch1 ~12.7h，batch2 ~11.8h 后无任何进展。DB updated_ts 仍为创建时刻（1786371555 和 1786374761）。
🔴 **两个 P0 Bug 未修复**：浮点精度（Bug #1）+ 余额不足（Bug #2），386 次 API 错误，205 次 SUBMITTED 中 91.2% 失败。
🔴 **BATCH_TAKE_EXECUTED 填量丢失**：日志 12 次 EXECUTED（filled=14.80 × 10 + 170.49 + 175.12），但 DB take_filled_size 全为 0.0（tracking bug）。
🔴 **市场锁永不解锁**：MARKET_REWARD_EXIT_UNLOCKED 仍为 0。两个市场的 reward_exit_locked 标记无法自动清理。
🔴 **Fill-to-batch 链接缺失**：reward_exit_fills.batch_id 均为空字符串，fill 无法回溯到 batch。
🔴 **零订单记录**：reward_exit_orders = 0 行，FAK 失败订单不被记录到数据库，排查困难。
✅ 无重复 fill_id（fill_id 在 reward_exit_fills 中唯一）
✅ 无 MANUAL_HOLD、无 BATCH_LOSS_LOCKED
✅ 无 REWARD_EXIT_BATCH_CLOSED（所有 batch 均无完整生命周期）
✅ 同一 fill_id 只关联到一个 batch_id（不存在重复关联）

### 结论

与第 26 轮完全相同的冻结状态。bot 离线 ~11.5h，两个 batch 自创建约 11.8–12.7h 后仍是 TAKE_PENDING 孤儿化。修复优先顺序不变：

1. **P0**: 修复 `taker_buy()` 浮点精度（market-order builder 按 collateral 金额报价）
2. **P0**: take 前检查余额，余额不足时降级为 `size = floor(balance / price)`
3. **P1**: batch 超时自动关闭（terminal_after_secs 到期后 close + unlock，解除市场锁）
4. **P1**: bot 启动时清理僵死 batch + 修复 fill-to-batch 链接 + TAKE_EXECUTED 后更新 take_filled_size + 记录 FAK 失败订单到 reward_exit_orders

---

## 第 26 轮（2026-08-11 02:06 BJT）— Scheduled Task double-take-exit-monitor

### 状态：Bot 离线 ~2.6h，完全冻结，无变化

Bot 自 2026-08-10 23:31 BJT 关闭后再未启动。无 2026-08-11 日志文件。仅使用 2026-08-10 日志进行累计统计。数据库状态与第 18–25 轮完全相同，无任何新事件触发。

### 事件统计（累计，无变化）

| 事件 | 次数 |
|------|------|
| REWARD_EXIT_BATCH_OPENED | 2 |
| BATCH_TAKE_SUBMITTED | 205 |
| BATCH_TAKE_EXECUTED | 12 |
| MARKET_REWARD_EXIT_LOCKED | 4 |
| BATCH_TAKE_COMPLETED | 0 |
| BATCH_TAKE_PARTIAL | 0 |
| BATCH_SELL_PLACED | 0 |
| BATCH_SELL_PARTIAL | 0 |
| REWARD_EXIT_BATCH_CLOSED | 0 |
| MARKET_REWARD_EXIT_UNLOCKED | 0 |
| BATCH_MANUAL_HOLD | 0 |
| BATCH_LOSS_LOCKED | 0 |
| API "invalid amounts" 错误 | 138 |
| API "not enough balance" 错误 | 248 |
| API 错误总计 | 386 |

### 数据库状态（无变化）

| 表 | 行数 | 详情 |
|---|------|------|
| reward_exit_batches | 2 | 均为 TAKE_PENDING, take_filled_size=0.0, exit_filled_size=0.0, updated_ts 未变化（1786371555 / 1786374761） |
| reward_exit_fills | 2 | 两个 origin fills（fill_id: 1e5af08f / 02cafdd7, batch_id 均为空字符串），intent=normal_reward, side=BUY |
| reward_exit_orders | 0 | FAK 失败不记录 |

### Batch 详情（无变化）

| Batch ID | 市场 | 原始成交 | take_target | SUBMITTED | EXECUTED | 错误类型 | 创建 | 孤儿时长 |
|----------|------|----------|-------------|-----------|----------|----------|------|----------|
| reward-exit-1e5af08f | Will Ralph Norman place second... | BUY NO 3.66 @ 0.52 | 7.32 | 90 | 10 | invalid amounts → not enough balance | 22:19 | ~3.8h |
| reward-exit-02cafdd7 | Will MrBeast's next video... | BUY YES 43.9 @ 0.57 | 87.8 | 115 | 2 | not enough balance | 23:12 | ~2.9h |

### 异常信号检测

🔴 **Bot 离线 ~2.6h**：自 2026-08-10 23:31 关闭（"正在退出，撤销全部订单"）。无 2026-08-11 日志文件。
🔴 **两个 TAKE_PENDING batch 孤儿化**：batch1 ~3.8h，batch2 ~2.9h 后无任何进展。DB updated_ts 仍为创建时刻（1786371555 和 1786374761）。
🔴 **两个 P0 Bug 未修复**：浮点精度（Bug #1）+ 余额不足（Bug #2），386 次 API 错误，205 次 SUBMITTED 中 91.2% 失败（基于错误计数）。
🔴 **BATCH_TAKE_EXECUTED 填量丢失**：日志 12 次 EXECUTED（filled=14.80 × 10 + 170.49 + 175.12），但 DB take_filled_size 全为 0.0（tracking bug）。
🔴 **市场锁永不解锁**：MARKET_REWARD_EXIT_UNLOCKED 仍为 0。两个市场的 reward_exit_locked 标记无法自动清理。
🔴 **Fill-to-batch 链接缺失**：reward_exit_fills.batch_id 均为空字符串，fill 无法回溯到 batch。
🔴 **零订单记录**：reward_exit_orders = 0 行，FAK 失败订单不被记录到数据库，排查困难。
✅ 无重复 fill_id（fill_id 在 reward_exit_fills 中唯一）
✅ 无 MANUAL_HOLD、无 BATCH_LOSS_LOCKED
✅ 无 REWARD_EXIT_BATCH_CLOSED（所有 batch 均无完整生命周期）
✅ 同一 fill_id 只关联到一个 batch_id（不存在重复关联）

### 结论

与第 25 轮相同的完全冻结状态。bot 离线 ~2.6h，两个 batch 自创建约 2.9–3.8h 后仍是 TAKE_PENDING 孤儿化。修复优先顺序不变：

1. **P0**: 修复 `taker_buy()` 浮点精度（market-order builder 按 collateral 金额报价）
2. **P0**: take 前检查余额，余额不足时降级为 `size = floor(balance / price)`
3. **P1**: batch 超时自动关闭（terminal_after_secs 到期后 close + unlock，解除市场锁）
4. **P1**: bot 启动时清理僵死 batch + 修复 fill-to-batch 链接 + TAKE_EXECUTED 后更新 take_filled_size + 记录 FAK 失败订单到 reward_exit_orders

---

---

## 🚨 P0 紧急 Bugs（2 个已知，均未修复）

**发现时间**: 2026-08-10 22:19 BJT（Bug #1）+ 23:12 BJT（Bug #2）
**最新状态**: 2026-08-10 23:31 BJT — Bot 关闭，两个 batch 均卡在 TAKE_PENDING，不再重试

### Bug #1: Float Precision — `taker_buy()` API Rejection

batch1 在 22:19:15 创建后 91 次 SUBMITTED 全部失败，原因：
```
error: "invalid amounts, the market buy orders maker amount supports a max accuracy of 2 decimals"
```
根因：`create_order(price=0.99, size=7)` → maker = 0.99 × 7 = 6.9299...（4 decimal，API 要求 2 decimal）。

**修复方案**（已在代码注释中描述）：使用 market-order builder 按 collateral 金额报价。

### Bug #2: Balance Insufficient — 大额 batch 无余额检查

batch2 原始 fill 只有 $25 notional，但 take_target_size = 87.8 需 $86.92 collateral，账户余额仅 $79.03。缺口 $7.89。

错误信息：`not enough balance / allowance: balance: 5046679, order amount: 86963450`

**修复方案**：take 前检查余额，余额不足时降级为 `size = floor(balance / price)`。

### 两个 Bug 对比

| 维度 | Batch 1 (Ralph Norman) | Batch 2 (MrBeast) |
|------|------------------------|---------------------|
| 错误 | invalid amounts (maker 精度) | not enough balance |
| 根因 | `price × size` 浮点 → 6.9299… | take 需 $86.92，余额 $79.03 |
| 是否已知 | 是（代码注释已描述修复） | 否（新发现） |
| 原始 notional | $1.90 | $25.02 |
| SUBMITTED 次数 | 91 | 116 |

---

## 第 16 轮详细（2026-08-10 23:40 BJT）

### 状态：Bot 已关闭，两个 batch 孤儿化

Bot 在 23:31 BJT 关闭（"正在退出，撤销全部订单"），之后无新事件。

### 事件统计（累计）

| 事件 | 次数 |
|------|------|
| REWARD_EXIT_BATCH_OPENED | 2 |
| BATCH_TAKE_SUBMITTED | 207 |
| MARKET_REWARD_EXIT_LOCKED | 4 |
| BATCH_TAKE_PARTIAL | 0 |
| BATCH_TAKE_COMPLETED | 0 |
| BATCH_SELL_PLACED | 0 |
| BATCH_SELL_PARTIAL | 0 |
| REWARD_EXIT_BATCH_CLOSED | 0 |
| MARKET_REWARD_EXIT_UNLOCKED | 0 |
| BATCH_MANUAL_HOLD | 0 |
| BATCH_LOSS_LOCKED | 0 |
| API "invalid amounts" 错误 | 138 |
| API "not enough balance" 错误 | 248 |
| API 错误总计 | 386 |

### Batch 详情

| Batch ID | 市场 | 原始成交 | take_target | SUBMITTED | 错误类型 | 创建时间 | 状态 |
|----------|------|----------|-------------|-----------|----------|----------|------|
| reward-exit-1e5af08f | Ralph Norman | BUY NO 3.66 @ 0.52 | 7.32 | 91 | invalid amounts | 22:19 | 孤儿化 81 分钟 |
| reward-exit-02cafdd7 | MrBeast 60-70M | BUY YES 43.9 @ 0.57 | 87.8 | 116 | not enough balance | 23:12 | 孤儿化 28 分钟 |

### 数据库状态

| 表 | 行数 | 备注 |
|---|------|------|
| reward_exit_batches | 2 | 两个都是 TAKE_PENDING, take_filled_size=0.0 |
| reward_exit_fills | 2 | 两个 origin fills, batch_id 均为空字符串 |
| reward_exit_orders | 0 | 无订单记录（FAK 失败不记录） |

### 异常信号检测

🔴 **P0 Bug #1 — 浮点精度**: 未修复。91 次重试，138 条错误，全部失败。
🔴 **P0 Bug #2 — 余额不足**: 未修复。116 次重试，248 条错误，全部失败。
🔴 **孤儿 batch #1**: 81 分钟无进展，batch 不再被重试（市场从旋转中移除），但仍 TAKE_PENDING。
🔴 **孤儿 batch #2**: 28 分钟无进展，bot 23:31 关闭后不再重试，但仍 TAKE_PENDING。
🔴 **无超时关闭机制**: 两个 batch 均无独立超时关闭逻辑 — 仅当市场被 tick 处理时才可能进展。
🔴 **Fill-to-batch 链接缺失**: reward_exit_fills.batch_id 为空字符串，fill 无法回溯到 batch。
⚠️ **"最差的中间状态"**: 市场锁定 + bot 不再尝试退出 = 零收益且无法做市。
✅ 无重复 fill_id
✅ 无 MANUAL_HOLD
✅ 无 BATCH_LOSS_LOCKED

### 结论

两个 P0 Bug 导致所有 reward-exit batch 无法完成 take 阶段。Bot 关闭后 batch 成为孤儿，但数据库中仍为 TAKE_PENDING，市场锁也永远无法解除。需要修复：

1. **优先级最高**：修复 `taker_buy()` 浮点精度（使用 market-order builder 按 collateral 金额报价）
2. **优先级最高**：在 take 前检查余额，余额不足时降级为 `size = floor(balance / price)`
3. **优先级高**：添加 batch 超时自动关闭（terminal_after_secs 到期后 close + unlock，解除市场锁）
4. **优先级中**：bot 启动时检测过期 batch 并清理（解除僵死锁）
5. **优先级低**：fill 写入时将 batch_id 关联到 batch

---

## 第 17 轮详细（2026-08-10 23:51 BJT）

### 状态：Bot 已关闭 20 分钟，无变化

Bot 于 23:31 BJT 关闭（"正在退出，撤销全部订单"），之后日志中无任何 reward-exit 事件。两个 batch 仍处于孤儿化状态。

### 事件统计（累计，与第 16 轮相同）

| 事件 | 次数 |
|------|------|
| REWARD_EXIT_BATCH_OPENED | 2 |
| BATCH_TAKE_SUBMITTED | 207 |
| BATCH_TAKE_EXECUTED | 12 |
| MARKET_REWARD_EXIT_LOCKED | 4 |
| BATCH_TAKE_COMPLETED | 0 |
| BATCH_SELL_PLACED | 0 |
| BATCH_SELL_PARTIAL | 0 |
| REWARD_EXIT_BATCH_CLOSED | 0 |
| MARKET_REWARD_EXIT_UNLOCKED | 0 |
| BATCH_MANUAL_HOLD | 0 |
| BATCH_LOSS_LOCKED | 0 |
| API "invalid amounts" 错误 | 138 |
| API "not enough balance" 错误 | 248 |
| API 错误总计 | 386 |

### 数据库状态（确认）

| 表 | 行数 | 详情 |
|---|------|------|
| reward_exit_batches | 2 | 均为 TAKE_PENDING, take_filled_size=0.0 |
| reward_exit_fills | 2 | 两个 origin fills, batch_id 均为空字符串 |
| reward_exit_orders | 0 | 无订单记录 |
| banned_markets.json | 2 个 cid | 均非 reward-exit 相关市场 |

### Batch 详情

| Batch ID | 市场 | 原始成交 | take_target | SUBMITTED | BATCH_TAKE_EXECUTED | 错误类型 | 创建时间 | 孤儿时长 |
|----------|------|----------|-------------|-----------|---------------------|----------|----------|----------|
| reward-exit-1e5af08f | Ralph Norman | BUY NO 3.66 @ 0.52 | 7.32 | 91 | 11 (fill=14.80 但 remaining 始终=7) | invalid amounts | 22:19 | 92 min |
| reward-exit-02cafdd7 | MrBeast 60-70M | BUY YES 43.9 @ 0.57 | 87.8 | 116 | 1 (fill=170.49 then 175.12; remaining 始终=88) | not enough balance | 23:12 | 39 min |

### 异常信号

🔴 **Bug #1 浮点精度**: 未修复。91 次 SUBMITTED + 11 次 BATCH_TAKE_EXECUTED，但 remaining 始终为 7。TAKE_EXECUTED 后 fill=14.80 但没有被扣减 -- 可能是另一个 tracking bug。
🔴 **Bug #2 余额不足**: 未修复。116 次 SUBMITTED + 1 次 EXECUTED（fill=170.49 -> 175.12），remaining=88 未减少。
🔴 **BATCH_TAKE_EXECUTED 但 take_filled_size / remaining 未更新**: 两个 batch 的 DB 中 take_filled_size 均为 0.0，尽管日志中有 12 次 EXECUTED 事件。
🔴 **孤儿化持续**: 两个 batch 仍 TAKE_PENDING，Bot 关闭后无任何机制自动清理。
🔴 **市场锁永不解锁**: 23:31 bot 关闭后 MARKET_REWARD_EXIT_UNLOCKED 从未触发。
✅ 无重复 fill_id
✅ 无 MANUAL_HOLD 或 BATCH_LOSS_LOCKED
✅ banned_markets.json 中 2 个 cid 均非 reward-exit 相关

### 结论

Bot 已关闭 20 分钟，状态冻结。两个 batch 仍处于 TAKE_PENDING 且 take_filled_size=0。新增发现：BATCH_TAKE_EXECUTED 事件（12 次）中的 filled 量未被写入 DB 的 take_filled_size 或扣减 remaining，说明填量追踪存在 bug。Bot 重启后需要检查是否从 reward_exit_batches 恢复并继续。

---

## 第 15 轮详细（2026-08-10 23:25 BJT）

### 🆕 新发现：第二个 batch 触发，但仍有不同错误

23:12:59 BJT 时，第二个 reward batch 被触发：
- **市场**: `Will MrBeast's next video get between 60 and 70 mi`
- **原始成交**: BUY YES @ 0.57, size=43.9, notional=$25.02
- **Batch ID**: `reward-exit-02cafdd7-7bd2-4c1f-abd3-2e64e4d8c3cf`
- **take_target_size**: 87.8 (2 × 43.9)

### 🔴🆕 第二个 Bug：余额不足（not enough balance）

batch2 的 FAK BUY 失败原因不同于 batch1：
- batch1: `invalid amounts, maker amount supports a max accuracy of 2 decimals`（浮点精度）
- batch2: `not enough balance / allowance: balance: 79025569, order amount: 86963450`（**余额不足**）

batch2 的 take_target_size = 87.8，按 price=0.99 计算需要约 87.8 × 0.99 ≈ $86.92 collateral（86963450 原子单位），但账户余额只有 $79.03（79025569 原子单位）。余额缺口约 $7.89。

**根因分析**：这个 batch 的原始成交 notional 是 $25，但 take_target_size = 87.8 股 × 0.99 = $86.92，远超账户余额。批次的 take_target_size 计算用了 2× origin_size（88 股），但没有检查是否有足够余额来完成 take。

### 事件统计

| 事件 | 第 15 轮增量 |
|------|-------------|
| REWARD_EXIT_BATCH_OPENED | +1 (累计 2) |
| BATCH_TAKE_SUBMITTED | +54 (累计 123) |
| MARKET_REWARD_EXIT_LOCKED | +1 (累计 4) |
| API "not enough balance" | +94 (新增错误类型) |

### 结论

第二轮监控发现了一个**新的 P0 问题**：余额不足导致大额 batch 的 FAK BUY 被拒绝。虽然 batch1 的浮点精度问题和 batch2 的余额问题错误消息不同，但根因层面可能都指向同一个设计缺陷：`taker_buy()` 没有考虑到 `price × size` 可能超出账户余额的场景。修复时需要同时处理：

1. **优先级最高**: 修复 `taker_buy()` 浮点精度（使用 market-order builder）
2. **优先级最高（新增）**: 在 take 前检查余额，余额不足时降级为 `size = floor(balance / price)`
3. **优先级高**: 添加 batch 超时自动关闭（terminal 到期后 close + unlock）
4. **优先级中**: 添加孤儿 batch 清理逻辑（bot 启动时检测）

---

## 第 18 轮（2026-08-11 09:30 BJT）— Scheduled Task 自动监控

### 状态：Bot 已关闭 10 小时，无变化

Bot 自 2026-08-10 23:31 BJT 关闭后再未启动。无 2026-08-11 日志文件。两个 batch 状态与第 17 轮结束时完全相同。

### 事件统计（累计，与第 17 轮相同）

| 事件 | 次数 |
|------|------|
| REWARD_EXIT_BATCH_OPENED | 2 |
| BATCH_TAKE_SUBMITTED | 207 |
| BATCH_TAKE_EXECUTED | 12 |
| MARKET_REWARD_EXIT_LOCKED | 4 |
| BATCH_TAKE_COMPLETED | 0 |
| BATCH_SELL_PLACED | 0 |
| BATCH_SELL_PARTIAL | 0 |
| REWARD_EXIT_BATCH_CLOSED | 0 |
| MARKET_REWARD_EXIT_UNLOCKED | 0 |
| BATCH_MANUAL_HOLD | 0 |
| BATCH_LOSS_LOCKED | 0 |

### 数据库状态（确认）

| 表 | 行数 | 详情 |
|---|------|------|
| reward_exit_batches | 2 | 均为 TAKE_PENDING, take_filled_size=0.0 |
| reward_exit_fills | 2 | 两个 origin fills, batch_id 均为空字符串 |
| reward_exit_orders | 0 | 无订单记录 |

### Batch 详情

| Batch ID | 市场 | 原始成交 | take_target | SUBMITTED | TAKE_EXECUTED | 错误类型 | 创建时间 | 孤儿时长 |
|----------|------|----------|-------------|-----------|---------------|----------|----------|----------|
| reward-exit-1e5af08f | Ralph Norman | BUY NO 3.66 @ 0.52 | 7.32 | 91 | 11 | invalid amounts | 22:19 | ~11h |
| reward-exit-02cafdd7 | MrBeast 60-70M | BUY YES 43.9 @ 0.57 | 87.8 | 116 | 1 | not enough balance | 23:12 | ~10h |

### 异常信号检测

🔴 **Bot 关闭 10 小时**：无 2026-08-11 日志，bot 未重启。
🔴 **两个 TAKE_PENDING batch 孤儿化**：batch 10+ 小时后无任何进展。
🔴 **数据库 updated_ts 未变化**：batch1 仍显示 22:19:15，batch2 仍显示 23:12:41。
🔴 **市场锁永不解锁**：MARKET_REWARD_EXIT_UNLOCKED 从未触发。
🔴 **Fill-to-batch 链接缺失**：reward_exit_fills.batch_id 为空。
✅ 无新的重复事件
✅ 无 MANUAL_HOLD 或 BATCH_LOSS_LOCKED
✅ reward_exit_orders 仍为 0（FAK 失败不记录）

### 结论

第 18 轮监控确认 bot 已离线超过 10 小时，状态完全冻结。两个 P0 Bug（浮点精度 + 余额不足）仍未修复，两个 batch 仍是孤儿 TAKE_PENDING。关键修复仍然与第 17 轮相同四个优先级。Bot 重启后需要清理孤儿 batch 并解除市场锁。

---

## 第 19 轮（2026-08-11 00:21 BJT）— Scheduled Task 自动监控

### 状态：Bot 关闭 51 分钟，日志中再次确认两个错误在最后时刻合并

Bot 在 23:31 BJT 关闭（"正在退出，撤销全部订单"）。关闭前的最后 10 分钟（23:28–23:31），两个 batch 都经历了错误类型的变化：

- **batch1（Ralph Norman）**: 23:28:09 起不再报 "invalid amounts"，而改为 **"not enough balance"**（balance: 5046679, order amount: 7252890）。说明 bot 在此期间余额被用光（可能被 batch2 的 EXECUTED 填单消耗了）。
- **batch2（MrBeast）**: 始终报 "not enough balance"（balance: 5046679, order amount: 86963450）。余额缺口巨大（$86.96 vs $5.05）。

关键发现：23:28 之前 batch1 报的是 "invalid amounts"（浮点精度），23:28 起变成了 "not enough balance"。这说明余额被其他操作消耗后，batch1 的 $7.25 都无法支付。两个错误最终在退出时刻交汇。

### 事件统计（累计，最终值）

| 事件 | 次数 |
|------|------|
| REWARD_EXIT_BATCH_OPENED | 2 |
| BATCH_TAKE_SUBMITTED | 205 |
| BATCH_TAKE_EXECUTED | 12 |
| MARKET_REWARD_EXIT_LOCKED | 4 |
| BATCH_TAKE_COMPLETED | 0 |
| BATCH_SELL_PLACED | 0 |
| BATCH_SELL_PARTIAL | 0 |
| REWARD_EXIT_BATCH_CLOSED | 0 |
| MARKET_REWARD_EXIT_UNLOCKED | 0 |
| BATCH_MANUAL_HOLD | 0 |
| BATCH_LOSS_LOCKED | 0 |
| API "invalid amounts" 错误 | 138 |
| API "not enough balance" 错误 | 248 |
| API 错误总计 | 386 |

### 批次最终详情

| Batch ID | 市场 | 原始成交 | take_target | SUBMITTED | EXECUTED | 最终错误 | 创建 | 孤儿时长 |
|----------|------|----------|-------------|-----------|----------|----------|------|----------|
| reward-exit-1e5af08f | Ralph Norman | BUY NO 3.66 @ 0.52 | 7.32 | 90 | 10 | not enough balance (从 invalid amounts 切换) | 22:19 | ~2h |
| reward-exit-02cafdd7 | MrBeast 60-70M | BUY YES 43.9 @ 0.57 | 87.8 | 115 | 2 | not enough balance | 23:12 | ~69 min |

### 数据库状态（确认）

| 表 | 行数 | 详情 |
|---|------|------|
| reward_exit_batches | 2 | 均为 TAKE_PENDING, take_filled_size=0.0, exit_filled_size=0.0, updated_ts 未变化 |
| reward_exit_fills | 2 | 两个 origin fills, batch_id 均为空字符串 |
| reward_exit_orders | 0 | FAK 失败不记录 |

### 异常信号检测

🔴 **两个 P0 Bug 未修复**: 浮点精度 + 余额不足共导致 386 次 API 错误。
🔴 **错误类型交汇**: batch1 的报错从 "invalid amounts" 切换为 "not enough balance"（23:28 起），说明余额在运行中被耗尽。
🔴 **TAKE_EXECUTED 后 take_filled_size 未追踪**: DB 中两个 batch 的 take_filled_size 仍为 0.0，即使日志中记录了 12 次 EXECUTED（filled=14.80 和 170.49/175.12）。
🔴 **孤儿 batch 持续**: 两个 batch 仍 TAKE_PENDING，bot 关闭后无任何进展。
🔴 **市场锁永不解锁**: MARKET_REWARD_EXIT_UNLOCKED 从未触发。
🔴 **Fill-to-batch 链接缺失**: reward_exit_fills.batch_id 为空。
✅ 无重复 fill_id、无 MANUAL_HOLD、无 BATCH_LOSS_LOCKED

### 结论

第 19 轮监控为最终状态快照。bot 关闭已 51 分钟，所有状态冻结。新增观察：两个 batch 在最后阶段（23:28–23:31）因余额耗尽，错误类型交汇（batch1 从 invalid amounts 切换到 not enough balance）。需要修复的仍然是相同的四个优先级项：

1. P0: 修复 `taker_buy()` 浮点精度（market-order builder 按 collateral 金额报价）
2. P0: take 前检查余额，余额不足时降级
3. P1: batch 超时自动关闭（terminal_after_secs 到期后 close + unlock）
4. P1: bot 启动时清理僵死 batch + fill-to-batch 链接

---

## 第 21 轮（2026-08-11 scheduled task）— 自动监控

### 状态：Bot 已关闭超过 14 小时，状态冻结不变

Bot 自 2026-08-10 23:31 BJT 关闭后再未启动。无 2026-08-11 日志文件。数据库状态与第 18–20 轮结束时完全相同。无任何新事件触发。

### 事件统计（累计，与第 20 轮相同）

| 事件 | 次数 |
|------|------|
| REWARD_EXIT_BATCH_OPENED | 2 |
| BATCH_TAKE_SUBMITTED | 207 |
| BATCH_TAKE_EXECUTED | 12 |
| MARKET_REWARD_EXIT_LOCKED | 4 |
| BATCH_TAKE_COMPLETED | 0 |
| BATCH_SELL_PLACED | 0 |
| BATCH_SELL_PARTIAL | 0 |
| REWARD_EXIT_BATCH_CLOSED | 0 |
| MARKET_REWARD_EXIT_UNLOCKED | 0 |
| BATCH_MANUAL_HOLD | 0 |
| BATCH_LOSS_LOCKED | 0 |

### 数据库状态（确认，无变化）

| 表 | 行数 | 详情 |
|---|------|------|
| reward_exit_batches | 2 | 均为 TAKE_PENDING, take_filled_size=0.0, exit_filled_size=0.0, updated_ts 未变化 |
| reward_exit_fills | 2 | 两个 origin fills（fill_id: 1e5af08f/02cafdd7, batch_id 均为空字符串），intent=normal_reward, side=BUY |
| reward_exit_orders | 0 | FAK 失败不记录 |

### Batch 详情（无变化）

| Batch ID | 市场 | 原始成交 | take_target | SUBMITTED | EXECUTED | 错误类型 | 创建 | 孤儿时长 |
|----------|------|----------|-------------|-----------|----------|----------|------|----------|
| reward-exit-1e5af08f | Will Ralph Norman place second... | BUY NO 3.66 @ 0.52 | 7.32 | 91 | 11 | invalid amounts → not enough balance | 22:19 | ~15h |
| reward-exit-02cafdd7 | Will MrBeast's next video... | BUY YES 43.9 @ 0.57 | 87.8 | 116 | 1 | not enough balance | 23:12 | ~14h |

### 异常信号检测

🔴 **Bot 离线 14+ 小时**：无 2026-08-11 日志，bot 未重启。
🔴 **两个 TAKE_PENDING batch 持续孤儿化**：14+ 小时后无任何进展，DB 中 updated_ts 仍为创建时间戳（1786371555 和 1786374761）。
🔴 **两个 P0 Bug 未修复**：浮点精度（Bug #1）+ 余额不足（Bug #2），共致 386 次 API 错误。
🔴 **BATCH_TAKE_EXECUTED 填量丢失**：日志中有 12 次 EXECUTED（filled=14.80 和 170.49/175.12），但 DB take_filled_size=0.0 = tracking bug。
🔴 **市场锁永不解锁**：MARKET_REWARD_EXIT_UNLOCKED 仍为 0 次。
🔴 **Fill-to-batch 链接缺失**：reward_exit_fills.batch_id 为空，fill 无法回溯到 batch。
🔴 **零订单记录**：reward_exit_orders 仍为 0 行，FAK 失败订单不被记录，排查困难。
✅ 无重复 fill_id、无 MANUAL_HOLD、无 BATCH_LOSS_LOCKED
✅ 无新错误注入（bot 离线，无活动）

### 结论

第 21 轮监控确认状态仍然冻结。无新事件、无新错误、无任何活动。两个 batch 自创建以来约为第 14–15 小时，仍是 TAKE_PENDING 孤儿化。修复优先顺序与之前的轮次完全一致：

1. **P0**: 修复 `taker_buy()` 浮点精度（market-order builder 按 collateral 金额报价）
2. **P0**: take 前检查余额，余额不足时降级为 `size = floor(balance / price)`
3. **P1**: batch 超时自动关闭（terminal_after_secs 到期后 close + unlock）
4. **P1**: bot 启动时清理僵死 batch + 修复 fill-to-batch 链接 + TAKE_EXECUTED 后更新 take_filled_size + 记录 FAK 失败订单到 reward_exit_orders

---

## 第 23 轮（2026-08-11 scheduled task）— 自动监控（double-take-exit-monitor）

### 状态：Bot 离线 ~13+ 小时，完全冻结，无变化

Bot 自 2026-08-10 23:31 BJT 关闭后再未启动。无 2026-08-11 日志文件。仍使用 2026-08-10 日志进行事件统计。

### 事件统计（与第 22 轮相比，无误计数修正）

| 事件 | 次数 |
|------|------|
| REWARD_EXIT_BATCH_OPENED | 2 |
| BATCH_TAKE_SUBMITTED | 205 |
| BATCH_TAKE_EXECUTED | 12 |
| MARKET_REWARD_EXIT_LOCKED | 4 |
| BATCH_TAKE_COMPLETED | 0 |
| BATCH_TAKE_PARTIAL | 0 |
| BATCH_SELL_PLACED | 0 |
| BATCH_SELL_PARTIAL | 0 |
| REWARD_EXIT_BATCH_CLOSED | 0 |
| MARKET_REWARD_EXIT_UNLOCKED | 0 |
| BATCH_MANUAL_HOLD | 0 |
| BATCH_LOSS_LOCKED | 0 |
| API 错误总计 | 187（91.2% 的 SUBMITTED 失败） |
| - invalid amounts (Bug #1) | 69 |
| - not enough balance (Bug #2) | 118 |

### 数据库状态（无变化）

| 表 | 行数 | 详情 |
|---|------|------|
| reward_exit_batches | 2 | 均为 TAKE_PENDING, take_filled_size=0.0, exit_filled_size=0.0, updated_ts 未变化（1786371555 和 1786374761） |
| reward_exit_fills | 2 | 两个 origin fills（fill_id: 1e5af08f / 02cafdd7, batch_id 均为空字符串), intent=normal_reward, side=BUY |
| reward_exit_orders | 0 | FAK 失败不记录 |

### Batch 详情（无变化）

| Batch ID | 市场 | 原始成交 | take_target | SUBMITTED | EXECUTED | 错误类型 | 创建 | 孤儿时长 |
|----------|------|----------|-------------|-----------|----------|----------|------|----------|
| reward-exit-1e5af08f | Will Ralph Norman place second... | BUY NO 3.66 @ 0.52 | 7.32 | 90 | 10 | invalid amounts → not enough balance | 22:19 | ~15h |
| reward-exit-02cafdd7 | Will MrBeast's next video... | BUY YES 43.9 @ 0.57 | 87.8 | 115 | 2 | not enough balance | 23:12 | ~14h |

### 异常信号检测

🔴 **Bot 离线 ~13+ 小时**：无 2026-08-11 日志，bot 未重启。
🔴 **两个 TAKE_PENDING batch 持续孤儿化**：~15h / ~14h 后无任何进展，DB updated_ts 为创建时刻。
🔴 **两个 P0 Bug 未修复**：浮点精度（Bug #1）+ 余额不足（Bug #2），187 次 API 错误（91.2% 的 SUBMITTED 失败）。
🔴 **BATCH_TAKE_EXECUTED 填量丢失**：日志 12 次 EXECUTED（filled=14.80 + 170.49/175.12），DB take_filled_size 全为 0.0（tracking bug）。
🔴 **市场锁永不解锁**：MARKET_REWARD_EXIT_UNLOCKED 仍为 0。
🔴 **Fill-to-batch 链接缺失**：reward_exit_fills.batch_id 为空。
🔴 **零订单记录**：reward_exit_orders = 0，FAK 失败不可追溯。
✅ 无重复 fill_id、无 MANUAL_HOLD、无 BATCH_LOSS_LOCKED
✅ 无 REWARD_EXIT_BATCH_CLOSED（所有 batch 无完成生命周期）

### 结论

状态完全冻结不变。bot 离线超 13 小时，两个 batch 自创建已约 14–15 小时，仍是 TAKE_PENDING。修复优先顺序不变：

1. **P0**: 修复 `taker_buy()` 浮点精度（market-order builder 按 collateral 金额报价）
2. **P0**: take 前检查余额，余额不足时降级为 `size = floor(balance / price)`
3. **P1**: batch 超时自动关闭（terminal_after_secs 到期后 close + unlock）
4. **P1**: bot 启动时清理僵死 batch + 修复 fill-to-batch 链接 + TAKE_EXECUTED 后更新 take_filled_size + 记录 FAK 失败订单到 reward_exit_orders

---

## 第 22 轮（2026-08-11 scheduled task）— 自动监控

### 状态：Bot 离线 14+ 小时，完全冻结，无变化

Bot 自 2026-08-10 23:31 BJT 关闭后再未启动。无 2026-08-11 日志文件。数据库状态、事件统计与第 21 轮完全相同。

### 事件统计（累计，最终值 — 无变化）

| 事件 | 次数 |
|------|------|
| REWARD_EXIT_BATCH_OPENED | 2 |
| BATCH_TAKE_SUBMITTED | 205 |
| BATCH_TAKE_EXECUTED | 12 |
| MARKET_REWARD_EXIT_LOCKED | 4 |
| BATCH_TAKE_COMPLETED | 0 |
| BATCH_SELL_PLACED | 0 |
| BATCH_SELL_PARTIAL | 0 |
| REWARD_EXIT_BATCH_CLOSED | 0 |
| MARKET_REWARD_EXIT_UNLOCKED | 0 |
| BATCH_MANUAL_HOLD | 0 |
| BATCH_LOSS_LOCKED | 0 |
| API "invalid amounts" 错误 | 138 |
| API "not enough balance" 错误 | 248 |
| API 错误总计 | 386 |

### 数据库状态（无变化）

| 表 | 行数 | 详情 |
|---|------|------|
| reward_exit_batches | 2 | 均为 TAKE_PENDING, take_filled_size=0.0, exit_filled_size=0.0, updated_ts 未变化 |
| reward_exit_fills | 2 | 两个 origin fills（fill_id: 1e5af08f/02cafdd7, batch_id 均为空字符串），intent=normal_reward, side=BUY |
| reward_exit_orders | 0 | FAK 失败不记录 |

### Batch 详情（无变化）

| Batch ID | 市场 | 原始成交 | take_target | SUBMITTED | EXECUTED | 错误类型 | 创建 | 孤儿时长 |
|----------|------|----------|-------------|-----------|----------|----------|------|----------|
| reward-exit-1e5af08f | Will Ralph Norman place second... | BUY NO 3.66 @ 0.52 | 7.32 | 91 | 11 | invalid amounts → not enough balance | 22:19 | ~15h |
| reward-exit-02cafdd7 | Will MrBeast's next video... | BUY YES 43.9 @ 0.57 | 87.8 | 116 | 1 | not enough balance | 23:12 | ~14h |

### 异常信号检测

🔴 **Bot 离线 14+ 小时**：无 2026-08-11 日志，bot 未重启。状态与第 18–21 轮完全相同。
🔴 **两个 TAKE_PENDING batch 持续孤儿化**：15h 后无任何进展，DB updated_ts 仍为创建时刻。
🔴 **两个 P0 Bug 未修复**：浮点精度（Bug #1）+ 余额不足（Bug #2），386 次 API 错误。
🔴 **BATCH_TAKE_EXECUTED 填量丢失**：日志 12 次 EXECUTED，但 DB take_filled_size 全为 0.0（tracking bug）。
🔴 **市场锁永不解锁**：MARKET_REWARD_EXIT_UNLOCKED 仍为 0。
🔴 **Fill-to-batch 链接缺失**：reward_exit_fills.batch_id 为空。
🔴 **零订单记录**：reward_exit_orders = 0，FAK 失败不可追溯。
✅ 无重复 fill_id、无 MANUAL_HOLD、无 BATCH_LOSS_LOCKED
✅ Batch ID 互斥 — 无重复 fill_id 关联到多个 batch

### 结论

第 22 轮监控确认状态仍然完全冻结。bot 离线超 14 小时，两个 batch 自创建已约 15 小时，仍是 TAKE_PENDING 孤儿化。修复优先顺序不变：

1. **P0**: 修复 `taker_buy()` 浮点精度（market-order builder 按 collateral 金额报价）
2. **P0**: take 前检查余额，余额不足时降级为 `size = floor(balance / price)`
3. **P1**: batch 超时自动关闭（terminal_after_secs 到期后 close + unlock）
4. **P1**: bot 启动时清理僵死 batch + 修复 fill-to-batch 链接 + TAKE_EXECUTED 后更新 take_filled_size + 记录 FAK 失败订单到 reward_exit_orders

---

## 更新日志

| 时间 | 更新 |
|------|------|
| 2026-08-10 19:50 | 初始创建，零触发状态 |
| 2026-08-10 20:15 | 自动监控：零触发。Bot 20:02 重启后无新 LIVE FILL。 |
| 2026-08-10 20:30 | 自动监控：零触发。所有指标为空。等待首次 reward fill。 |
| 2026-08-10 20:50 | 第 4 轮：零触发。Bot 活跃（20:35 在报价 Gen.G + Ralph Norman），20:00 重启后无任何 LIVE FILL。 |
| 2026-08-10 21:08 | 第 6 轮：零触发。Bot 正常，需等美国晚间交易时段。 |
| 2026-08-10 21:21 | 第 7 轮：零触发。21:20 手动重启。pending reward fill。 |
| 2026-08-10 21:37 | 第 8 轮：零触发。21:34 又一次重启。MrBeast 两市场活跃。 |
| 2026-08-10 22:10 | 第 10 轮：零触发。需等待 reward fill。 |
| **2026-08-10 22:24** | **第 11 轮：🎉 首次触发！但发现 P0 Bug** — batch 创建成功，Ralph Norman 市场锁定，但 take 订单因浮点精度问题被 API 拒绝 |
| **2026-08-10 22:43** | **第 12 轮：🐛 P0 Bug 持续** |
| **2026-08-10 22:52** | **第 13 轮：⚠️ P0 Bug + batch 孤儿化** |
| 2026-08-10 23:05 | 第 14 轮：无变化。P0 Bug 未修复，batch 仍孤儿化已 46 分钟。 |
| **2026-08-10 23:25** | **第 15 轮：🚨 新发现 P0 Bug #2** — 第二个 batch 因余额不足被拒，需要 $86.92 collateral 但只有 $79.03。两个 batch 都卡住。 |
| **2026-08-10 23:51** | **第 17 轮：Bot 已关闭 20 分钟，状态冻结。新增发现 BATCH_TAKE_EXECUTED（12 次）后 fill 量未被写入 DB/take_filled_size。** |
| **2026-08-11 09:30** | **第 18 轮（Scheduled Task）：Bot 关闭 10 小时，状态完全冻结。两个 batch 仍 TAKE_PENDING 孤儿化，DB 无任何变化。P0 Bug 未修复。** |
| **2026-08-10 23:40** | **第 16 轮：Bot 23:31 关闭，两个 batch 孤儿化。Bug #1 91 次重试全失败，Bug #2 116 次重试全失败。共计 386 次 API 错误。** |
| **2026-08-11 00:21** | **第 19 轮（Scheduled Task）：Bot 关闭 51 分钟。新发现错误类型交汇：batch1 的错误从 invalid amounts 切换为 not enough balance（23:28 余额耗尽）。最终 205 次 SUBMITTED, 12 次 EXECUTED（但 DB take_filled_size=0），386 次 API 错误。状态冻结。** |
| **2026-08-11 scheduled** | **第 21 轮（Scheduled Task）：Bot 离线 14+ 小时。状态完全冻结不变：2 batch TAKE_PENDING, take_filled_size=0.0, 2 origin fills (batch_id 为空), 0 orders。两个 P0 Bug 未修复。总计 386 次 API 错误。无新事件触发。** |
| **2026-08-11 scheduled** | **第 20 轮（Scheduled Task）：Bot 离线 12+ 小时。无 2026-08-11 日志。数据库状态完全冻结：2 batch TAKE_PENDING, take_filled_size=0.0, 2 fills, 0 orders。两个 P0 Bug 未修复。无新事件触发。** |
| **2026-08-11 scheduled** | **第 23 轮（Scheduled Task — double-take-exit-monitor）：Bot 离线 ~13h。无 2026-08-11 日志文件。DB 无变化：2 batch TAKE_PENDING (orphaned), take_filled_size=0.0, 2 fills (batch_id 为空), 0 orders。205 SUBMITTED, 12 EXECUTED, 187 API 错误（91.2% 失败率）。两个 P0 Bug 未修复。无新事件触发。** |
| **2026-08-11 scheduled** | **第 22 轮（Scheduled Task）：Bot 离线 14+ 小时。状态完全冻结 — 与第 18–21 轮相同。2 batch TAKE_PENDING (15h 孤儿化), take_filled_size=0.0, 2 fills (batch_id 为空), 0 orders。两个 P0 Bug 未修复。386 次 API 错误。无新事件。** |
| **2026-08-11 01:36** | **第 24 轮（Scheduled Task — double-take-exit-monitor）：Bot 离线 ~2.1h。状态完全冻结：2 batch TAKE_PENDING（batch1 ~3.3h / batch2 ~2.4h 孤儿化）。DB 无变化：take_filled_size=0.0, 2 fills (batch_id 为空), 0 orders。205 SUBMITTED (91.2% 失败), 12 EXECUTED, 386 API 错误 (138 invalid amounts + 248 not enough balance)。两个 P0 Bug 未修复。无新事件触发。*** |
| **2026-08-11 02:06** | **第 26 轮（Scheduled Task — double-take-exit-monitor）：Bot 离线 ~2.6h。状态完全冻结：2 batch TAKE_PENDING（batch1 ~3.8h / batch2 ~2.9h 孤儿化）。DB 无变化：take_filled_size=0.0, 2 fills (batch_id 为空), 0 orders。205 SUBMITTED (91.2% 失败), 12 EXECUTED, 386 API 错误 (138 invalid amounts + 248 not enough balance)。两个 P0 Bug 未修复。无新事件触发。 |
| **2026-08-11 05:21** | **第 39 轮（Scheduled Task — double-take-exit-monitor）：Bot 离线 ~5.8h。状态完全冻结：2 batch TAKE_PENDING（batch1 ~7.0h / batch2 ~6.1h 孤儿化）。DB 无变化：take_filled_size=0.0, 2 fills (batch_id 为空), 0 orders。205 SUBMITTED (12 EXECUTED, 91.2% 失败), 386 API 错误 (138 invalid amounts + 248 not enough balance)。两个 P0 Bug 未修复。无新事件触发。** |
| **2026-08-11 05:06** | **第 38 轮（Scheduled Task — double-take-exit-monitor）：Bot 离线 ~5.6h。状态完全冻结：2 batch TAKE_PENDING（batch1 ~6.8h / batch2 ~5.9h 孤儿化）。DB 无变化：take_filled_size=0.0, 2 fills (batch_id 为空), 0 orders。205 SUBMITTED (12 EXECUTED, 91.2% 失败), 386 API 错误 (138 invalid amounts + 248 not enough balance)。两个 P0 Bug 未修复。无新事件触发。** |
| **2026-08-11 03:51** | **第 33 轮（Scheduled Task — double-take-exit-monitor）：Bot 离线 ~4.3h。状态完全冻结：2 batch TAKE_PENDING（batch1 ~5.5h / batch2 ~4.6h 孤儿化）。DB 无变化：take_filled_size=0.0, 2 fills (batch_id 为空), 0 orders。205 SUBMITTED (12 EXECUTED, 91.2% 失败), 386 API 错误 (138 invalid amounts + 248 not enough balance)。两个 P0 Bug 未修复。无新事件触发。Bot 23:31 退出后未再启动。 |
| **2026-08-11 04:37** | **第 36 轮（Scheduled Task — double-take-exit-monitor）：Bot 离线 ~5.1h。状态与第 35 轮完全相同：2 batch TAKE_PENDING（batch1 ~6.3h / batch2 ~5.4h 孤儿化）。DB 无变化：take_filled_size=0.0, 2 fills (batch_id 为空), 0 orders。205 SUBMITTED (12 EXECUTED), 193 API 错误。两个 P0 Bug 未修复。无新事件触发。** |
| **2026-08-11 04:20** | **第 35 轮（Scheduled Task — double-take-exit-monitor）：Bot 离线 ~4.9h。状态完全冻结：2 batch TAKE_PENDING（batch1 ~6.0h / batch2 ~5.1h 孤儿化）。DB 无变化：take_filled_size=0.0, 2 fills (batch_id 为空), 0 orders。205 SUBMITTED (12 EXECUTED, 91.2% 失败), 386 API 错误 (138 invalid amounts + 248 not enough balance)。两个 P0 Bug 未修复。无新事件触发。Bot 23:31 退出后未再启动。 |
| **2026-08-11 04:20** | **第 35 轮（Scheduled Task — double-take-exit-monitor）：Bot 离线 ~4.9h。状态完全冻结：2 batch TAKE_PENDING（batch1 ~6.0h / batch2 ~5.1h 孤儿化）。DB 无变化：take_filled_size=0.0, 2 fills (batch_id 为空), 0 orders。205 SUBMITTED (12 EXECUTED, 91.2% 失败), 386 API 错误 (138 invalid amounts + 248 not enough balance)。两个 P0 Bug 未修复。无新事件触发。Bot 23:31 退出后未再启动。 |
| **2026-08-11 04:05** | **第 34 轮（Scheduled Task — double-take-exit-monitor）：Bot 离线 ~4.6h。状态完全冻结：2 batch TAKE_PENDING（batch1 ~5.8h / batch2 ~4.9h 孤儿化）。DB 无变化：take_filled_size=0.0, 2 fills (batch_id 为空), 0 orders。205 SUBMITTED (12 EXECUTED, 91.2% 失败), 386 API 错误 (138 invalid amounts + 248 not enough balance)。两个 P0 Bug 未修复。无新事件触发。Bot 23:31 退出后未再启动。 |
| **2026-08-11 03:36** | **第 32 轮（Scheduled Task — double-take-exit-monitor）：Bot 离线 ~4.1h。状态完全冻结：2 batch TAKE_PENDING（batch1 ~5.3h / batch2 ~4.4h 孤儿化）。DB 无变化：take_filled_size=0.0, 2 fills (batch_id 为空), 0 orders。205 SUBMITTED (12 EXECUTED, 91.2% 失败), 386 API 错误 (138 invalid amounts + 248 not enough balance)。两个 P0 Bug 未修复。无新事件触发。HARD KILL 于 08:55 触发（$22.68 >= $20.00），bot 多次重启/退出循环，最终 23:31 退出后未再启动。 |
| **2026-08-11 03:20** | **第 31 轮（Scheduled Task — double-take-exit-monitor）：Bot 离线 ~3.8h。状态完全冻结：2 batch TAKE_PENDING（batch1 ~5.0h / batch2 ~4.1h 孤儿化）。DB 无变化：take_filled_size=0.0, 2 fills (batch_id 为空), 0 orders。205 SUBMITTED (12 EXECUTED, 91.2% 失败), 386 API 错误 (138 invalid amounts + 248 not enough balance)。两个 P0 Bug 未修复。无新事件触发。** |
| **2026-08-11 02:51** | **第 29 轮（Scheduled Task — double-take-exit-monitor）：Bot 离线 ~3.3h。状态完全冻结：2 batch TAKE_PENDING（batch1 ~4.5h / batch2 ~3.7h 孤儿化）。DB 无变化：take_filled_size=0.0, 2 fills (batch_id 为空), 0 orders。205 SUBMITTED (12 EXECUTED, 91.2% 失败), 386 API 错误 (138 invalid amounts + 248 not enough balance)。两个 P0 Bug 未修复。无新事件触发。** |
| **2026-08-11 13:00** | **第 28 轮（Scheduled Task — double-take-exit-monitor）：Bot 离线 ~13.5h。状态完全冻结：2 batch TAKE_PENDING（batch1 ~14.7h / batch2 ~13.8h 孤儿化）。DB 无变化：take_filled_size=0.0, 2 fills (batch_id 为空), 0 orders。205 SUBMITTED (91.2% 失败), 12 EXECUTED, 386 API 错误。两个 P0 Bug 未修复。无新事件触发。** |
| **2026-08-11 11:00** | **第 27 轮（Scheduled Task — double-take-exit-monitor）：Bot 离线 ~11.5h。状态完全冻结：2 batch TAKE_PENDING（batch1 ~12.7h / batch2 ~11.8h 孤儿化）。DB 无变化：take_filled_size=0.0, 2 fills (batch_id 为空), 0 orders。205 SUBMITTED (91.2% 失败), 12 EXECUTED, 386 API 错误。两个 P0 Bug 未修复。无新事件触发。 |
| **2026-08-11** | **🔧 Bug 修复（代码审查后）**：<br>**A)** `_advance_take_pending` 现在在直接授信填满目标后立即调用 `_seal_and_set_sell_target`（不再卡在 TAKE_PENDING）。<br>**B)** 已移除 `record_user_fill`、`_credit_take_fills` 和 `PaperBroker._record_reward_exit_fact` 中的 `batch_take` 双重计数——直接授信是唯一写入路径。<br>**C)** 已重新添加 `_sync_clob_balance(AssetType.COLLATERAL)`（之前从 `taker_buy()` 中意外删除）。<br>**D)** `remaining_before` 日志变量现已正确捕获授信前值（`remaining_after` 显示授信后值）。<br>测试：奖励退出生命周期测试通过 ✓
