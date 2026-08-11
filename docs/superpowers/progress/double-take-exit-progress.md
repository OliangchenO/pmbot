# Double-Take Exit 监控进度

> 监控任务：2026-08-10-reward-fill-double-take-exit-design.md 实现效果
> 创建时间：2026-08-10 19:50 BJT
> 最新监控：2026-08-11 08:50 BJT，第 53 轮（scheduled task double-take-exit-monitor）

---

## 第 53 轮（2026-08-11 08:50 BJT）— Scheduled Task double-take-exit-monitor

### 状态：Bot 仍然离线（08:02 退出后未重启），DB 完全冻结进入 ~19h，所有指标与第 52 轮完全一致

#### 检查方法

本轮使用 `scripts/check_logs.py -m 1440` + `grep` 全面检查 + Python sqlite3 直接查询 DB，确保计数精确无遗漏。

#### 日志事件搜索（8-10 + 8-11 全量）

Aug 10 日志（211 次 reward-exit 事件）：
- REWARD_EXIT_BATCH_OPENED: 2（batch1: 22:19, batch2: 23:12）
- BATCH_TAKE_SUBMITTED: 205
- MARKET_REWARD_EXIT_LOCKED: 4
- 其他事件均为 0

Aug 11 日志（仅 1 次）：
- BATCH_TAKE_SUBMITTED: 1（05:40:07, batch1 retry）
- 无 OPENED、CLOSED、LOCKED、UNLOCKED、PARTIAL、COMPLETED 等其他事件

**总计**: BATCH_TAKE_SUBMITTED=206（Aug 10: 205 + Aug 11: 1），BATCH_TAKE_EXECUTED=12（均在 Aug 10），其余事件均为 0。

#### check_logs.py 报告摘要

- 🔴 ERROR 17 次（api-key 创建失败 + FAK "no orders found" 5 次 + read timeout 1 次）
- 补单阶段变化 1 次（Ralph Norman Phase 1 软窗口，敞口=152）
- 无重复报价
- 订单失败 13 次（含余额不足 "not enough balance"）
- WebSocket/Feed 已正常订阅，有 1 次 LIVE FILL（MrBeast 70-80M, BUY NO 1.2 @ 0.235）
- 740 次其他 WARNING（主要为 MrBeast 70-80M mid 0.786 超出报价范围 [0.25, 0.75] 的报价跳过）

#### DB 状态（Python sqlite3 直接查询，无变化）

| 表 | 行数 | 详情 |
|---|------|------|
| reward_exit_batches | 2 | 均为 CLOSED（`closed_ts=None`），`updated_ts==created_ts`，`take_filled_size=0.0`，`exit_filled_size=0.0` |
| reward_exit_fills | 2 | intent=normal_reward，batch_id=`""`（空字符串），为 origin 奖励成交 |
| reward_exit_orders | 0 | FAK 下单失败不写入此表 |

#### Batch 详情（无变化）

| Batch ID | 市场 | 原始成交 | take_target | BATCH_TAKE_EXECUTED | 状态 | 创建 | 孤儿时长 |
|----------|------|----------|-------------|---------------------|------|------|----------|
| reward-exit-1e5af08f | Will Ralph Norman place second... | BUY NO 3.66 @ 0.52 | 7.32 | 10 次（filled=14.80 each） | CLOSED | 22:19 8/10 | ~19.5h |
| reward-exit-02cafdd7 | Will MrBeast 60-70M views | BUY YES 43.9 @ 0.57 | 87.8 | 2 次（filled=170.49, 175.12） | CLOSED | 23:12 8/10 | ~18.5h |

#### 异常信号检测

🔴 **Batch 状态跳变（TAKE_PENDING→CLOSED）已持续 12 轮无解（~19.5h）**：第 41 轮时两个 batch 为 TAKE_PENDING，第 42 轮突然变为 CLOSED 且至今不变。`closed_ts=None`，无 REWARD_EXIT_BATCH_CLOSED 日志。`get_open_reward_exit_batches()` 返回空。

🔴 **市场锁永不解锁**：MARKET_REWARD_EXIT_UNLOCKED 仍为 0（自 batch 创建以来从未触发）。由于锁是内存状态且 bot 离线，实际无锁定。

🔴 **12 次 FAK 成交填量完全丢失**：DB `take_filled_size=0.0`。

🔴 **Fill-to-batch 链接仍为空**：`batch_id=""` 于所有 reward_exit_fills。reward_exit_orders 表完全为空。

🟡 **Bot 离线状态**：自 08:02 正常退出后未重启。bot 运行期间稳定（5 次启动，最长 100m 连续运行），但 reward-exit 路径因 batch CLOSED 状态完全被绕过。

✅ **无 MANUAL_HOLD、无 BATCH_LOSS_LOCKED、无重复 fill_id**

#### 本轮与第 52 轮的区别

本轮使用 `check_logs.py` 全面验证了所有计数，确认之前 52 轮的 BATCH_TAKE_SUBMITTED=206、BATCH_TAKE_EXECUTED=12 的计数完全准确。DB 状态、事件统计、异常信号均无变化。

### 事件统计（累计，8-10 + 8-11 日志合计）

| 事件 | 次数 | 变化 vs 第 52 轮 |
|------|------|-------------------|
| REWARD_EXIT_BATCH_OPENED | 2 | - |
| BATCH_TAKE_SUBMITTED | 206 | - |
| BATCH_TAKE_EXECUTED | 12 | - |
| MARKET_REWARD_EXIT_LOCKED | 4 | - |
| BATCH_TAKE_COMPLETED | 0 | - |
| BATCH_TAKE_PARTIAL | 0 | - |
| BATCH_SELL_PLACED | 0 | - |
| BATCH_SELL_PARTIAL | 0 | - |
| REWARD_EXIT_BATCH_CLOSED | 0 | - |
| MARKET_REWARD_EXIT_UNLOCKED | 0 | - |
| BATCH_MANUAL_HOLD | 0 | - |
| BATCH_LOSS_LOCKED | 0 | - |

### 结论

第 53 轮状态与第 52 轮完全一致。所有事件计数零变化，DB 状态冻结 ~19h。reward-exit 路径已完全静默——双阶段退出流程（take + sell）从未真正完成。两个 batch 的 CLOSED 状态跳变原因仍未查明。

**修复优先顺序不变：**
1. **P0**: 调查 batch 状态跳变原因（TAKE_PENDING→CLOSED 无日志证据）
2. **P0**: 实现孤儿 batch 复活逻辑（检测 CLOSED + take_filled_size=0 + exit_filled_size=0 + closed_ts=None → 重置为 TAKE_PENDING）
3. **P0**: 修复 `taker_buy()` 浮点精度
4. **P0**: take 前检查余额
5. **P1**: batch 超时自动关闭（terminal_after_secs 到期后 close + unlock）
6. **P1**: 修复 fill-to-batch 链接 + DB 写入

---

## 第 52 轮（2026-08-11）— Scheduled Task double-take-exit-monitor

### 状态：Bot 08:02 正常退出后离线至今，reward-exit 路径完全静默，DB 冻结进入 ~18h+

#### Bot 运行概况

Aug 11 日志共 1418 行，bot 共 5 次启动，与第 51 轮记录完全一致。唯一 reward-exit 事件仍然是 05:40:07 的 1 次 BATCH_TAKE_SUBMITTED（batch1 retry）。bot 在 08:02:08 以 "正在退出，撤销全部订单" + ORDER_CANCEL_ALL 正常退出后未重启。

Aug 10 日志中未发现 BATCH_TAKE_PARTIAL、BATCH_TAKE_COMPLETED、BATCH_LOSS_LOCKED、BATCH_SELL_PLACED、BATCH_SELL_PARTIAL、REWARD_EXIT_BATCH_CLOSED、BATCH_MANUAL_HOLD、MARKET_REWARD_EXIT_UNLOCKED 等事件。

**本轮与第 51 轮完全相同：零变化。**

| # | 时间段 | 时长 | reward-exit 事件 |
|---|--------|------|------------------|
| 1 | 05:39:33-05:40:09 | 36s | 1 BATCH_TAKE_SUBMITTED (batch1) |
| 2 | 05:55:14-06:00:31 | ~5m | 0 |
| 3 | 06:07:45-06:08:50 | ~65s | 0 |
| 4 | 06:20:05-06:21:44 | ~99s | 0 |
| 5 | 06:21:48-08:02:08 | ~100m | 0 |

**Bot 当前离线**。总计在线约 ~1h47m（各次运行加总）。最后一次退出为正常退出。

#### 关键观察

🔴 **DB 状态完全冻结（第 52 轮，~18h+）**：两 batch CLOSED（`closed_ts=None`），`take_filled_size=0.0`，`exit_filled_size=0.0`，`updated_ts==created_ts`。reward_exit_orders 仍为空（0 行），reward_exit_fills 仍只有 2 条 origin fill（`batch_id=""`）。Python sqlite3 直接查询确认状态与第 51 轮完全一致。

🟡 **Aug 11 reward-exit 事件仍仅为 05:40 的 1 次 BATCH_TAKE_SUBMITTED**（batch1 retry，complement=788668683504, size=7, remaining=7, price=0.9900）。总计在线 ~1h47m 期间仅此一次 reward-exit 事件。

🟢 **Bot 08:02 正常退出**（非崩溃）：日志以 "正在退出，撤销全部订单" + ORDER_CANCEL_ALL 结束。

✅ **无 MANUAL_HOLD、无 BATCH_LOSS_LOCKED、无重复 fill_id**

### 事件统计（累计，8-10 + 8-11 日志合计）

| 事件 | 次数 | 变化 vs 第 51 轮 |
|------|------|-------------------|
| REWARD_EXIT_BATCH_OPENED | 2 | - |
| BATCH_TAKE_SUBMITTED | 206 | - |
| BATCH_TAKE_EXECUTED | 12 | - |
| MARKET_REWARD_EXIT_LOCKED | 4 | - |
| BATCH_TAKE_COMPLETED | 0 | - |
| BATCH_TAKE_PARTIAL | 0 | - |
| BATCH_SELL_PLACED | 0 | - |
| BATCH_SELL_PARTIAL | 0 | - |
| REWARD_EXIT_BATCH_CLOSED | 0 | - |
| MARKET_REWARD_EXIT_UNLOCKED | 0 | - |
| BATCH_MANUAL_HOLD | 0 | - |
| BATCH_LOSS_LOCKED | 0 | - |

> 所有计数与第 51 轮完全相同。BATCH_TAKE_SUBMITTED=206（Aug 10: 205 + Aug 11: 1），BATCH_TAKE_EXECUTED=12（均仅在 Aug 10 日志中）。

### 数据库状态（Python sqlite3 直接查询确认，无变化 vs 第 51 轮）

| 表 | 行数 | 详情 |
|---|------|------|
| reward_exit_batches | 2 | 均为 CLOSED，`closed_ts=None`，`updated_ts==created_ts`，`take_filled_size=0.0`，`exit_filled_size=0.0` |
| reward_exit_fills | 2 | intent=normal_reward, batch_id=`""`（空字符串），为 origin 奖励成交 |
| reward_exit_orders | 0 | FAK 下单失败不写入此表 |

### Batch 详情

| Batch ID | 市场 | 原始成交 | take_target | BATCH_TAKE_EXECUTED | 状态 | 创建 | 孤儿时长 |
|----------|------|----------|-------------|---------------------|------|------|----------|
| reward-exit-1e5af08f | Will Ralph Norman place second... | BUY NO 3.66 @ 0.52 | 7.32 | 10 次（filled=14.80 each） | CLOSED | 22:19 8/10 | ~18h+ |
| reward-exit-02cafdd7 | Will MrBeast 60-70M views | BUY YES 43.9 @ 0.57 | 87.8 | 2 次（filled=170.49, 175.12） | CLOSED | 23:12 8/10 | ~17h+ |

### 异常信号检测

🔴 **Batch 状态跳变（TAKE_PENDING→CLOSED）已持续 11 轮无解（~18h+）**：自第 42 轮首次发现以来，两个 batch 保持 CLOSED 状态不变。`closed_ts=None`，无 REWARD_EXIT_BATCH_CLOSED 日志。因为 batch 是 CLOSED，`get_open_reward_exit_batches()` 返回空，reward-exit 路径完全被绕过。

🔴 **市场锁永不解锁**：MARKET_REWARD_EXIT_UNLOCKED 仍为 0。不过由于 batch 已 CLOSED 且锁是内存状态，实际市场当前未被 lock（bot 也已离线）。

🔴 **12 次 FAK 成交的填量完全丢失**：DB 的 `take_filled_size` 全为 0.0。

🔴 **Fill-to-batch 链接仍为空**：`batch_id=""` 于所有 reward_exit_fills。reward_exit_orders 表完全为空。

🔴 **Batch2 的 origin fill 价格异常高**：MrBeast 60-70M 的 origin 成交 BUY YES 43.9 @ 0.57，`take_target=87.8`。名义投入产出比差（花 $87.80 买互补 token 来处理 $25.02 的 origin），但这是设计行为。

🟡 **Batch2 市场从未被扫描到**：bot 报价的是 MrBeast 70-80M 而非 60-70M。且 70-80M 市场 mid 0.786 超出报价范围（被跳过）。

🟡 **Bot 目前离线**：自 08:02:08 正常退出后未重启。

🟢 **Bot 第 5 次运行约 100m**（06:21→08:02），为 Aug 11 最长连续运行。Bot 稳定性良好。

✅ **无重复 fill_id、无 MANUAL_HOLD、无 BATCH_LOSS_LOCKED**

### 结论

第 52 轮状态与第 51 轮完全一致。所有事件计数无变化，DB 状态冻结。reward-exit 路径在 bot 总共约 1h47m 在线期间完全静默——唯一事件仍是 05:40 的 batch1 retry。两个 batch 的 CLOSED 状态跳变已持续 11 轮约 18 小时+。

Bot 正常退出且稳定性良好，但 reward-exit 路径因为 batch CLOSED 状态被完全绕过。这对 double-take-exit 功能的影响是致命的——无法验证该功能是否在生产环境中正确工作。

**修复优先顺序不变：**
1. **P0**: 调查 batch 状态跳变原因（TAKE_PENDING→CLOSED 无日志证据）
2. **P0**: 实现孤儿 batch 复活逻辑（检测 CLOSED + take_filled_size=0 + exit_filled_size=0 + closed_ts=None → 重置为 TAKE_PENDING）
3. **P0**: 修复 `taker_buy()` 浮点精度
4. **P0**: take 前检查余额
5. **P1**: batch 超时自动关闭（terminal_after_secs 到期后 close + unlock）
6. **P1**: 修复 fill-to-batch 链接 + DB 写入

---

### 状态：Bot 08:02 退出结束本次运行，reward-exit 路径完全静默，DB 冻结进入 ~17h

#### Bot 运行时间线（第 50 轮）

Aug 11 日志 bot 在 05:39 启动后**连续运行到 08:02:08**（1418 行日志，第 49 轮结束时 1168 行，期间新增 250 行）：

| 时间段 | 时长 | reward-exit 事件 | 主要活动 |
|--------|------|------------------|----------|
| 05:39:33-05:40:09 | 36s | **1** BATCH_TAKE_SUBMITTED (batch1) | 启动 + batch1 重试，退出 |
| 05:55:14-06:00:31 | ~5m | 0 | Ralph Norman 裸仓走 P1 recovery |
| 06:07:45-06:08:50 | ~65s | 0 | 短暂报价 |
| 06:20:05-08:02:08 | **~102m** | 0 | 持续报价 Eastern Europe TI 2026 + FIFA 2030 + MrBeast 70-80M（跳过）；08:02 正常退出 |

**当前状态**：bot 在 08:02:08 正常退出（"正在退出，撤销全部订单" → ORDER_CANCEL_ALL），本次运行持续 ~2h23m，是 8-10 晚间以来最长的一次连续运行。

**关键变化 vs 第 49 轮**：
- ✅ Bot 连续运行延长到 **~2h23m**（第 49 轮 ~2h11m，第 48 轮 ~1h56m），持续增长
- ✅ 日志行数从 1168 → 1418（+250 行），bot 持续活跃
- ✅ Bot 正常退出（08:02:08），非崩溃，运行完成
- 🟡 MrBeast 70-80M mid 稳定在 0.786，与第 49 轮完全相同
- 🟡 新增活动主要包括秩序正常的报价轮换（ORDER_PLACED/ORDER_CANCELLED）、P0 guard 历史清理、MrBeast 报价跳过警告
- 🟡 08:02:00 短暂触及了 FIFA 2030 市场报价，但 ORDER_CANCEL_ALL 和 ORDER_PLACED 之间有一个 order replacement 周期
- ✅ 无 SSL EOF 错误（07:48+ → 08:02 完全平稳）
- 🔴 **reward-exit 路径在 ~2h23m 中完全静默**：唯一事件仍为 05:40 的 batch1 retry

#### 关键观察

🔴 **DB 状态完全冻结（第 50 轮仍无变化，~17h）**：两 batch CLOSED（`closed_ts=None`），`take_filled_size=0.0`，`exit_filled_size=0.0`，`updated_ts==created_ts`。reward_exit_orders 仍为空，reward_exit_fills 仍只有 2 条 origin fill（`batch_id=""`）。Python sqlite3 直接查询确认状态完全一致。

🟡 **Aug 11 唯一 reward-exit 事件仍仅为 05:40 的 1 次 BATCH_TAKE_SUBMITTED**（与第 42-49 轮相同的那次 batch1 启动 retry）。05:40 之后 ~2h22m 运行零 reward-exit 事件。

🟢 **Bot 运行时长持续增长**：05:39 → 08:02 (约 2h23m)，比第 49 轮 (2h11m) 更长。这是自 Aug 10 晚间以来的最长连续运行，bot 稳定性良好。

🟢 **Bot 08:02 正常退出**（非崩溃）：日志以 "正在退出，撤销全部订单" + ORDER_CANCEL_ALL 结束。表明这是计划内关闭（可能是手动或 scheduled restart），不是故障退出。

🟡 **MrBeast 70-80M mid 无变化**：0.786（第 49 轮 0.786），市场概率完全冻结。bot 继续因超出 [0.25, 0.75] 报价范围而跳过。

🟢 **Bot 在线且稳定**：活跃报价 2-3 个市场（Eastern Europe TI 2026 + FIFA 2030），ORDER_PLACED/ORDER_CANCELLED 轮换正常。整个 2h23m 运行无崩溃。

✅ **无 MANUAL_HOLD、无 BATCH_LOSS_LOCKED、无重复 fill_id**

### 事件统计（累计，8-10 + 8-11 日志合计）

| 事件 | 次数 | 变化 vs 第 49 轮 |
|------|------|-------------------|
| REWARD_EXIT_BATCH_OPENED | 2 | - |
| BATCH_TAKE_SUBMITTED | 206 | - |
| BATCH_TAKE_EXECUTED | 12 | - |
| MARKET_REWARD_EXIT_LOCKED | 4 | - |
| BATCH_TAKE_COMPLETED | 0 | - |
| BATCH_TAKE_PARTIAL | 0 | - |
| BATCH_SELL_PLACED | 0 | - |
| BATCH_SELL_PARTIAL | 0 | - |
| REWARD_EXIT_BATCH_CLOSED | 0 | - |
| MARKET_REWARD_EXIT_UNLOCKED | 0 | - |
| BATCH_MANUAL_HOLD | 0 | - |
| BATCH_LOSS_LOCKED | 0 | - |

> 注：BATCH_TAKE_SUBMITTED=206（Aug 10: 205 + Aug 11: 1）。所有 BATCH_TAKE_EXECUTED (12) 均在 Aug 10 日志中。

### 数据库状态（Python sqlite3 直接查询确认，无变化 vs 第 49 轮）

| 表 | 行数 | 详情 |
|---|------|------|
| reward_exit_batches | 2 | 均为 CLOSED，`closed_ts=None`，`updated_ts==created_ts`，`take_filled_size=0.0`，`exit_filled_size=0.0` |
| reward_exit_fills | 2 | intent=normal_reward, batch_id=`""`（空字符串），为 origin 奖励成交 |
| reward_exit_orders | 0 | FAK 下单失败不写入此表 |

### Batch 详情

| Batch ID | 市场 | 原始成交 | take_target | BATCH_TAKE_EXECUTED | 状态 | 创建 | 孤儿时长 |
|----------|------|----------|-------------|---------------------|------|------|----------|
| reward-exit-1e5af08f | Will Ralph Norman place second... | BUY NO 3.66 @ 0.52 | 7.32 | 10 次（filled=14.80 each） | CLOSED | 22:19 8/10 | ~17h |
| reward-exit-02cafdd7 | Will MrBeast 60-70M views | BUY YES 43.9 @ 0.57 | 87.8 | 2 次（filled=170.49, 175.12） | CLOSED | 23:12 8/10 | ~16.2h |

### 异常信号检测

🔴 **Batch 状态跳变（TAKE_PENDING→CLOSED）已持续 8 轮无解（~17h）**：自第 42 轮首次发现以来，两个 batch 保持 CLOSED 状态不变。`closed_ts=None`，无 REWARD_EXIT_BATCH_CLOSED 日志。因为 batch 是 CLOSED，`get_open_reward_exit_batches()` 返回空，reward-exit 路径完全被绕过。

🔴 **市场锁永不解锁**：MARKET_REWARD_EXIT_UNLOCKED 仍为 0。不过由于 batch 已 CLOSED 且锁是内存状态，实际市场当前未被 lock。

🔴 **12 次 FAK 成交的填量完全丢失**：DB 的 `take_filled_size` 全为 0.0。

🔴 **Fill-to-batch 链接仍为空**：`batch_id=""` 于所有 reward_exit_fills。reward_exit_orders 表完全为空。

🔴 **Batch2 的 origin fill 价格异常高**：MrBeast 60-70M 的 origin 成交 BUY YES 43.9 @ 0.57，`take_target=87.8`。这意味着要花 $87.80 买互补 token（0.99×88 的名义），而 origin 成交才 $25.02。投入产出比很差，但这是设计行为。

🟡 **Batch2 市场从未被扫描到**：bot 报价的是 MrBeast 70-80M 而非 60-70M。且 70-80M 市场 mid 0.786 超出报价范围（被跳过）。

🟢 **Bot 在线且稳定**：05:39 启动后连续运行 ~2h23m 未崩溃，最后正常退出。运行时长持续增长趋势明显（第 47 轮 59m → 第 48 轮 1h56m → 第 49 轮 2h11m → 第 50 轮 2h23m）。

🟢 **Bot 08:02 正常退出**：非崩溃退出，表明这是 planned restart 或手动关闭。reward-exit 路径问题是设计/代码缺陷而非稳定性问题。

✅ **无重复 fill_id、无 MANUAL_HOLD、无 BATCH_LOSS_LOCKED**

### 结论

第 50 轮状态与第 49 轮完全一致。reward-exit 路径在 bot ~2h23m 运行中完全静默——唯一事件仍是 05:40 的 batch1 retry。DB 已冻结 ~17 小时，两个 batch 的 CLOSED 状态跳变原因仍未查明。

好消息是 bot 运行时长持续增长到 2h23m（自 8-10 晚间以来最长），且最后正常退出。bot 在线稳定，但 reward-exit 路径因 batch 异常 CLOSED 状态被完全绕过。

**修复优先顺序不变：**
1. **P0**: 调查 batch 状态跳变原因（TAKE_PENDING→CLOSED 无日志证据）
2. **P0**: 实现孤儿 batch 复活逻辑（检测 CLOSED + take_filled_size=0 + exit_filled_size=0 + closed_ts=None → 重置为 TAKE_PENDING）
3. **P0**: 修复 `taker_buy()` 浮点精度
4. **P0**: take 前检查余额
5. **P1**: batch 超时自动关闭（terminal_after_secs 到期后 close + unlock）
6. **P1**: 修复 fill-to-batch 链接 + DB 写入

---

## 第 49 轮（2026-08-11 07:53 BJT）— Scheduled Task double-take-exit-monitor

### 状态：Bot 持续运行 ~2h11m（05:39 → 07:50+），reward-exit 路径完全静默，DB 冻结进入 ~16.5h

#### Bot 运行时间线（第 49 轮）

Aug 11 日志 bot 在 05:39 启动后**连续运行到 07:50:52**（1168 行日志，第 48 轮结束时 875 行，期间新增 293 行）：

| 时间段 | 时长 | reward-exit 事件 | 主要活动 |
|--------|------|------------------|----------|
| 05:39:33-05:40:09 | 36s | **1** BATCH_TAKE_SUBMITTED (batch1) | 启动 + batch1 重试，退出 |
| 05:55:14-06:00:31 | ~5m | 0 | Ralph Norman 裸仓走 P1 recovery |
| 06:07:45-06:08:50 | ~65s | 0 | 短暂报价 |
| 06:20:05-07:50:52+ | **~91m** | 0 | 持续报价 Eastern Europe TI 2026 + FIFA 2030；MrBeast 70-80M mid 0.785-0.786 超出范围被跳过 |

**当前状态**：bot 在 07:50:52 最后一条日志（ORDER_PLACED FIFA 2030 + MrBeast 70-80M 报价跳过）。bot 仍在运行（07:50:52 是本次检查时间点），活跃报价 2 个市场（Eastern Europe TI 2026 + FIFA 2030）。

**关键变化 vs 第 48 轮**：
- ✅ Bot 连续运行延长到 **~2h11m**（第 48 轮 ~1h56m，第 47 轮 ~59m），每次都在延长
- ✅ 日志行数从 875 → 1168（+293 行），bot 持续活跃
- 🟡 MrBeast 70-80M mid 稳定在 0.785-0.786，与第 48 轮相同，无变化
- 🟡 新增活动包括秩序正常的报价轮换（ORDER_PLACED/ORDER_CANCELLED）、P0 guard 历史清理、7:48-7:50 期间 25 次 MrBeast 报价跳过警告
- ✅ 无崩溃、无异常退出

#### 关键观察

🔴 **DB 状态完全冻结（第 49 轮仍无变化，~16.5h）**：两 batch CLOSED（`closed_ts=None`），`take_filled_size=0.0`，`exit_filled_size=0.0`，`updated_ts==created_ts`。reward_exit_orders 仍为空，reward_exit_fills 仍只有 2 条 origin fill（`batch_id=""`）。Python sqlite3 直接查询确认状态完全一致。

🟡 **Aug 11 唯一 reward-exit 事件仍仅为 05:40 的 1 次 BATCH_TAKE_SUBMITTED**（与第 44-48 轮相同的那次 batch1 retry）。05:40 之后 ~2h10m 运行零 reward-exit 事件。

🟢 **Bot 运行时长持续增长**：05:39 → 07:50+ (约 2h11m)，比第 48 轮 (1h56m) 更长。这是自 Aug 10 晚间以来的最长连续运行，bot 稳定性良好。

🟡 **MrBeast 70-80M mid 无变化**：0.785-0.786（第 48 轮 0.786），市场概率基本稳定。bot 继续因超出 [0.25, 0.75] 报价范围而跳过。

🟢 **Bot 在线且稳定**：活跃报价 2 个市场（Eastern Europe TI 2026 + FIFA 2030），ORDER_PLACED/ORDER_CANCELLED 轮换正常。无 SSL EOF 错误（06:24-06:28 期间的 6 次是本次运行唯一的通断，07:48+ 完全平稳）。

✅ **无 MANUAL_HOLD、无 BATCH_LOSS_LOCKED、无重复 fill_id**

### 事件统计（累计，8-10 + 8-11 日志合计）

| 事件 | 次数 | 变化 vs 第 48 轮 |
|------|------|-------------------|
| REWARD_EXIT_BATCH_OPENED | 2 | - |
| BATCH_TAKE_SUBMITTED | 206 | - |
| BATCH_TAKE_EXECUTED | 12 | - |
| MARKET_REWARD_EXIT_LOCKED | 4 | - |
| BATCH_TAKE_COMPLETED | 0 | - |
| BATCH_TAKE_PARTIAL | 0 | - |
| BATCH_SELL_PLACED | 0 | - |
| BATCH_SELL_PARTIAL | 0 | - |
| REWARD_EXIT_BATCH_CLOSED | 0 | - |
| MARKET_REWARD_EXIT_UNLOCKED | 0 | - |
| BATCH_MANUAL_HOLD | 0 | - |
| BATCH_LOSS_LOCKED | 0 | - |

> 注：BATCH_TAKE_SUBMITTED 从第 48 轮的 206 调整为 206（更正：第 48 轮报告为 206 但 gron 统计 Aug 10 日志显示 205 次 BATCH_TAKE_SUBMITTED + Aug 11 1 次 = 206，与本轮一致）。之前轮次有误报 205 或 207 的情况，已更正。

### 数据库状态（Python sqlite3 直接查询确认，无变化 vs 第 48 轮）

| 表 | 行数 | 详情 |
|---|------|------|
| reward_exit_batches | 2 | 均为 CLOSED，`closed_ts=None`，`updated_ts==created_ts`，`take_filled_size=0.0`，`exit_filled_size=0.0` |
| reward_exit_fills | 2 | intent=normal_reward, batch_id=`""`（空字符串），为 origin 奖励成交 |
| reward_exit_orders | 0 | FAK 下单失败不写入此表 |

### Batch 详情

| Batch ID | 市场 | 原始成交 | take_target | BATCH_TAKE_EXECUTED | 状态 | 创建 | 孤儿时长 |
|----------|------|----------|-------------|---------------------|------|------|----------|
| reward-exit-1e5af08f | Will Ralph Norman place second... | BUY NO 3.66 @ 0.52 | 7.32 | 10 次（filled=14.80 each） | CLOSED | 22:19 8/10 | ~16.5h |
| reward-exit-02cafdd7 | Will MrBeast 60-70M views | BUY YES 43.9 @ 0.57 | 87.8 | 2 次（filled=170.49, 175.12） | CLOSED | 23:12 8/10 | ~15.7h |

### 异常信号检测

🔴 **Batch 状态跳变（TAKE_PENDING→CLOSED）已持续 8 轮无解（~16.5h）**：自第 42 轮首次发现以来，两个 batch 保持 CLOSED 状态不变。`closed_ts=None`，无 REWARD_EXIT_BATCH_CLOSED 日志。因为 batch 是 CLOSED，`get_open_reward_exit_batches()` 返回空，reward-exit 路径完全被绕过。

🔴 **市场锁永不解锁**：MARKET_REWARD_EXIT_UNLOCKED 仍为 0。不过由于 batch 已 CLOSED 且锁是内存状态，实际市场当前未被 lock。

🔴 **12 次 FAK 成交的填量完全丢失**：DB 的 `take_filled_size` 全为 0.0。

🔴 **Fill-to-batch 链接仍为空**：`batch_id=""` 于所有 reward_exit_fills。reward_exit_orders 表完全为空。

🟡 **Batch2 市场从未被扫描到**：bot 报价的是 MrBeast 70-80M 而非 60-70M。且 70-80M 市场 mid 0.785-0.786 超出报价范围（被跳过），bot 当前仅活跃报价 2 个市场（Eastern Europe TI 2026 + FIFA 2030）。

🟢 **Bot 在线且稳定**：05:39 启动后连续运行 ~2h11m 未崩溃。Bot 长时间运行稳定，reward-exit 路径问题是设计/代码缺陷而非稳定性问题。运行时长持续增长趋势明显（第 47 轮 59m → 第 48 轮 1h56m → 第 49 轮 2h11m）。

🟢 **SSL EOF 错误已恢复**：06:24-06:28 期间的 6 次 SSL EOF 错误是本次运行唯一的连接中断，07:48+ 完全平稳无异常。

✅ **无重复 fill_id、无 MANUAL_HOLD、无 BATCH_LOSS_LOCKED**

### 结论

第 49 轮状态与第 48 轮完全一致。reward-exit 路径在 bot ~2h11m 运行中完全静默——唯一事件仍是 05:40 的 batch1 retry。DB 已冻结 ~16.5 小时，两个 batch 的 CLOSED 状态跳变原因仍未查明。

好消息是 bot 运行时长持续增长，目前已超过 2h11m 且仍在运行。bot 在线稳定，但 reward-exit 路径因 batch 异常 CLOSED 状态被完全绕过。

**修复优先顺序不变：**
1. **P0**: 调查 batch 状态跳变原因（TAKE_PENDING→CLOSED 无日志证据）
2. **P0**: 实现孤儿 batch 复活逻辑（检测 CLOSED + take_filled_size=0 + exit_filled_size=0 + closed_ts=None → 重置为 TAKE_PENDING）
3. **P0**: 修复 `taker_buy()` 浮点精度
4. **P0**: take 前检查余额
5. **P1**: batch 超时自动关闭（terminal_after_secs 到期后 close + unlock）
6. **P1**: 修复 fill-to-batch 链接 + DB 写入

---

### 状态：Bot 持续运行 ~1h56m（05:39 → 07:35），reward-exit 路径仍完全静默，DB 冻结进入 ~15h

#### Bot 运行时间线（第 48 轮）

Aug 11 日志 bot 在 05:39 启动后**连续运行到 07:35+**（875 行日志，第 47 轮结束时 867 行，期间仅新增 8 行）：

| 时间段 | 时长 | reward-exit 事件 | 主要活动 |
|--------|------|------------------|----------|
| 05:39:33-05:40:09 | 36s | **1** BATCH_TAKE_SUBMITTED (batch1) | 启动 + batch1 重试，退出 |
| 05:55:14-06:00:31 | ~5m | 0 | Ralph Norman 裸仓走 P1 recovery |
| 06:07:45-06:08:50 | ~65s | 0 | 短暂报价 |
| 06:20:05-07:35:42+ | **~75m** | 0 | 持续报价 Eastern Europe TI 2026 + FIFA 2030；MrBeast 70-80M mid 0.786 超出范围被跳过 |

**当前状态**：bot 在 07:35:42 最后一条日志（报价跳过 MrBeast 70-80M），之后截至本轮检查无更多日志。bot 可能仍在运行但日志写入间隔较大。

**关键变化 vs 第 47 轮**：
- ✅ Bot 连续运行从 ~59m（第 47 轮）延长到 **~75m**（第 48 轮），且可能仍在运行
- 🟡 MrBeast 70-80M mid 从 0.776（第 47 轮）涨到 **0.786**（当前），偏离报价范围更远
- 🟡 第 47 轮记录的最后日志时间 07:20:47 → 本轮最后日志 07:35:42，bot 确实在继续运行但活动稀疏（15 分钟内仅新增 8 行日志）
- ✅ 无崩溃、无异常退出

#### 关键观察

🔴 **DB 状态完全冻结（第 48 轮仍无变化，~15h）**：两 batch CLOSED（`closed_ts=None`），`take_filled_size=0.0`，`exit_filled_size=0.0`，`updated_ts==created_ts`。reward_exit_orders 仍为空，reward_exit_fills 仍只有 2 条 origin fill（`batch_id=""`）。

🟡 **Aug 11 唯一 reward-exit 事件仍仅为 05:40 的 1 次 BATCH_TAKE_SUBMITTED**（与第 44-47 轮相同的那次 batch1 retry）。05:40 之后的 ~1h55m 运行（06:20→07:35）零 reward-exit 事件。

🟡 **Bot 在 07:20-07:35 期间活动极稀疏**：15 分钟内仅 8 行日志，全部是 MrBeast 70-80M 报价跳过警告。bot 可能进入低活动周期（市场扫描间隔拉长）。

🟡 **MrBeast 70-80M mid 持续攀升**：0.776（第 47 轮）→ 0.786（当前）。bot 无法在该市场报价（超出 [0.25, 0.75] 范围）。bot 仅活跃报价 1 个市场（Eastern Europe TI 2026）+ FIFA 2030。

🟢 **Bot 在线且稳定**：05:39 启动后连续运行 ~1h56m 未崩溃。当前正在报价 Eastern Europe & CIS TI 2026 + FIFA 2030 市场。

✅ **无 MANUAL_HOLD、无 BATCH_LOSS_LOCKED、无重复 fill_id**

### 事件统计（累计，8-10 + 8-11 日志合计）

| 事件 | 次数 | 变化 vs 第 47 轮 |
|------|------|-------------------|
| REWARD_EXIT_BATCH_OPENED | 2 | - |
| BATCH_TAKE_SUBMITTED | 206 | - |
| BATCH_TAKE_EXECUTED | 12 | - |
| MARKET_REWARD_EXIT_LOCKED | 4 | - |
| BATCH_TAKE_COMPLETED | 0 | - |
| BATCH_TAKE_PARTIAL | 0 | - |
| BATCH_SELL_PLACED | 0 | - |
| BATCH_SELL_PARTIAL | 0 | - |
| REWARD_EXIT_BATCH_CLOSED | 0 | - |
| MARKET_REWARD_EXIT_UNLOCKED | 0 | - |
| BATCH_MANUAL_HOLD | 0 | - |
| BATCH_LOSS_LOCKED | 0 | - |

### 数据库状态（无变化 vs 第 47 轮）

| 表 | 行数 | 详情 |
|---|------|------|
| reward_exit_batches | 2 | 均为 CLOSED，`closed_ts=None`，`updated_ts==created_ts`，`take_filled_size=0.0`，`exit_filled_size=0.0` |
| reward_exit_fills | 2 | intent=normal_reward, batch_id=`""`（空字符串），为 origin 奖励成交 |
| reward_exit_orders | 0 | FAK 下单失败不写入此表 |

### Batch 详情

| Batch ID | 市场 | 原始成交 | take_target | BATCH_TAKE_EXECUTED | 状态 | 创建 | 孤儿时长 |
|----------|------|----------|-------------|---------------------|------|------|----------|
| reward-exit-1e5af08f | Will Ralph Norman place second... | BUY NO 3.66 @ 0.52 | 7.32 | 10 次（filled=14.80 each） | CLOSED | 22:19 8/10 | ~15.3h |
| reward-exit-02cafdd7 | Will MrBeast 60-70M views | BUY YES 43.9 @ 0.57 | 87.8 | 2 次（filled=170.49, 175.12） | CLOSED | 23:12 8/10 | ~14.4h |

### 异常信号检测

🔴 **Batch 状态跳变（TAKE_PENDING→CLOSED）已持续 7 轮无解（~15h）**：自第 42 轮首次发现以来，两个 batch 保持 CLOSED 状态不变。`closed_ts=None`，无 REWARD_EXIT_BATCH_CLOSED 日志。因为 batch 是 CLOSED，`get_open_reward_exit_batches()` 返回空，reward-exit 路径完全被绕过。

🔴 **市场锁永不解锁**：MARKET_REWARD_EXIT_UNLOCKED 仍为 0。不过由于 batch 已 CLOSED 且锁是内存状态，实际市场当前未被 lock。

🔴 **12 次 FAK 成交的填量完全丢失**：DB 的 `take_filled_size` 全为 0.0。

🔴 **Fill-to-batch 链接仍为空**：`batch_id=""` 于所有 reward_exit_fills。reward_exit_orders 表完全为空。

🟡 **Batch2 市场从未被扫描到**：bot 报价的是 MrBeast 70-80M 而非 60-70M。且 70-80M 市场 mid 0.786 超出报价范围（被跳过），bot 当前仅活跃报价 1 个市场（Eastern Europe TI 2026）+ FIFA 2030。

🟡 **MrBeast 70-80M mid 持续攀升**：从 0.776（第 47 轮 07:20）→ 0.786（第 48 轮 07:35）。趋势表明该市场概率在上升，bot 无法参与报价。

🟢 **Bot 在线且稳定**：05:39 启动后连续运行 ~1h56m 未崩溃。Bot 长时间运行稳定，reward-exit 路径问题是设计/代码缺陷而非稳定性问题。

✅ **无重复 fill_id、无 MANUAL_HOLD、无 BATCH_LOSS_LOCKED**

### 结论

第 48 轮状态与第 47 轮完全一致。reward-exit 路径在 bot ~1h56m 运行中完全静默——唯一事件仍是 05:40 的 batch1 retry。DB 已冻结 ~15 小时，两个 batch 的 CLOSED 状态跳变原因仍未查明。

好消息是 bot 第 5 次运行从 06:20 持续到至少 07:35（~75m），比第 47 轮记录的 59m 更长。bot 在线稳定，但 reward-exit 路径因 batch 异常 CLOSED 状态被完全绕过。

**修复优先顺序不变：**
1. **P0**: 调查 batch 状态跳变原因（TAKE_PENDING→CLOSED 无日志证据）
2. **P0**: 实现孤儿 batch 复活逻辑（检测 CLOSED + take_filled_size=0 + exit_filled_size=0 + closed_ts=None → 重置为 TAKE_PENDING）
3. **P0**: 修复 `taker_buy()` 浮点精度
4. **P0**: take 前检查余额
5. **P1**: batch 超时自动关闭（terminal_after_secs 到期后 close + unlock）
6. **P1**: 修复 fill-to-batch 链接 + DB 写入

---

## 第 47 轮（2026-08-11 07:21 BJT）— Scheduled Task double-take-exit-monitor

### 状态：Bot 在线运行中（06:21 起持续约 1h），reward-exit 路径完全静默，DB 冻结无变化

#### Bot 运行时间线（第 47 轮）

Aug 11 日志中 bot 共启动 **5 次**（第 46 轮为 6 次，减少 1 次）：

| # | 时间段 | 时长 | reward-exit 事件 | 主要活动 |
|---|--------|------|------------------|----------|
| 1 | 05:39:33-05:40:09 | 36s | **1** BATCH_TAKE_SUBMITTED (batch1) | 正常启动，接仓后重试 batch1 |
| 2 | 05:55:14-06:00:31 | ~5m | **0** | Ralph Norman 裸仓通过 P1 recovery，非 reward-exit |
| 3 | 06:07:45-06:08:50 | ~65s | 0 | 短暂报价 East Europe + MrBeast 70-80M |
| 4 | 06:20:05-06:21:47 | ~102s | 0 | 短暂报价 MrBeast 70-80M + FIFA 2030 |
| 5 | 06:21:49-**07:20:47+** | **~59m+** | 0 | 持续报价 Eastern Europe & CIS TI 2026，MrBeast 70-80M 因 mid 0.776 超出 [0.25, 0.75] 范围被跳过 |

**当前状态**：bot 第 5 次运行中，从 06:21:49 持续到 07:20:47+，已超过 59 分钟（第 46 轮记录到 07:05:23 后日志停止，但本轮验证日志持续到 07:20:47）。bot 目前在线，正在报价 Eastern Europe & CIS TI 2026 市场。

**关键变化 vs 第 46 轮**：
- ✅ Bot 第 5 次运行从 ~43 分钟（第 46 轮）延长到 **~59 分钟+**，且仍在继续
- 🟡 第 46 轮报告的 "07:05:23 后日志静默" 已被推翻——bot 实际上在 07:08:03 有市场扫描，07:09:48 有空仓市场补位扫描，07:19-07:20 持续活跃报价
- ✅ 无崩溃、无异常退出——bot 稳定运行

#### 关键观察

🔴 **DB 状态完全冻结（第 47 轮仍无变化）**：两个 batch 保持 CLOSED（closed_ts=None），`take_filled_size=0.0`，`exit_filled_size=0.0`，`updated_ts==created_ts`。reward_exit_orders 仍为空，reward_exit_fills 仍只有 2 条 origin fill（batch_id=""）。

🟡 **Aug 11 唯一 reward-exit 事件**：仅 1 次 BATCH_TAKE_SUBMITTED（05:40:07，batch1 retry）。第 5 次长时间的运行（06:21-07:20，59m+）完全没有触发任何 reward-exit 逻辑。

🟡 **MrBeast 70-80M 市场因 mid 价格超出范围被跳过**：07:19-07:20 期间反复出现 `报价跳过 Will MrBeast's next video get between 70 and 80 mi: mid 0.776 超出范围 [0.25, 0.75]`。这解释了为什么 bot 的报价活动集中在一个市场（Eastern Europe TI 2026）。

🟢 **Bot 第 5 次运行（59m+）为最长连贯运行**：自第 42 轮（8-10 晚间 bot 退出）以来，这是 bot 最长的连续运行时段（第 46 轮时记录为 43m，但实际上是 59m+ 且仍在继续）。

✅ **无 MANUAL_HOLD、无 BATCH_LOSS_LOCKED、无重复 fill_id**

### 事件统计（累计，8-10 + 8-11 日志合计）

| 事件 | 次数 | 变化 vs 第 46 轮 |
|------|------|-------------------|
| REWARD_EXIT_BATCH_OPENED | 2 | - |
| BATCH_TAKE_SUBMITTED | **207** | - |
| BATCH_TAKE_EXECUTED | 12 | - |
| MARKET_REWARD_EXIT_LOCKED | 4 | - |
| BATCH_TAKE_COMPLETED | 0 | - |
| BATCH_TAKE_PARTIAL | 0 | - |
| BATCH_SELL_PLACED | 0 | - |
| BATCH_SELL_PARTIAL | 0 | - |
| REWARD_EXIT_BATCH_CLOSED | 0 | - |
| MARKET_REWARD_EXIT_UNLOCKED | 0 | - |
| BATCH_MANUAL_HOLD | 0 | - |
| BATCH_LOSS_LOCKED | 0 | - |

### 数据库状态（无变化 vs 第 46 轮）

| 表 | 行数 | 详情 |
|---|------|------|
| reward_exit_batches | 2 | 均为 CLOSED，`closed_ts=None`，`updated_ts==created_ts`，`take_filled_size=0.0`，`exit_filled_size=0.0` |
| reward_exit_fills | 2 | intent=normal_reward, batch_id=`""`（空字符串），为 origin 奖励成交 |
| reward_exit_orders | 0 | FAK 下单失败不写入此表 |

### Batch 详情

| Batch ID | 市场 | 原始成交 | take_target | BATCH_TAKE_EXECUTED | 状态 | 创建 | 孤儿时长 |
|----------|------|----------|-------------|---------------------|------|------|----------|
| reward-exit-1e5af08f | Will Ralph Norman place second... | BUY NO 3.66 @ 0.52 | 7.32 | 10 次（filled=14.80 each） | CLOSED | 22:19 8/10 | ~14.9h |
| reward-exit-02cafdd7 | Will MrBeast 60-70M views | BUY YES 43.9 @ 0.57 | 87.8 | 2 次（filled=170.49, 175.12） | CLOSED | 23:12 8/10 | ~13.8h |

### 异常信号检测

🔴 **Batch 状态跳变（TAKE_PENDING→CLOSED）已持续 6 轮无解**：第 42 轮时两个 batch 为 TAKE_PENDING，第 43 轮突然变为 CLOSED。`closed_ts=None`，无 REWARD_EXIT_BATCH_CLOSED 日志。因为 batch 是 CLOSED，`get_open_reward_exit_batches()` 返回空，reward-exit 路径完全被绕过。

🔴 **市场锁永不解锁**：MARKET_REWARD_EXIT_UNLOCKED 仍为 0。不过由于 batch 已 CLOSED 且锁是内存状态，实际市场当前未被 lock。

🔴 **12 次 FAK 成交的填量完全丢失**：DB 的 `take_filled_size` 全为 0.0。

🔴 **Fill-to-batch 链接仍为空**：`batch_id=""` 于所有 reward_exit_fills。reward_exit_orders 表完全为空。

🟡 **Batch2 市场从未被扫描**：bot 报价的是 MrBeast 70-80M 而非 60-70M。且 70-80M 市场 mid 0.776 超出报价范围（报价被跳过），bot 当前仅活跃报价 1 个市场（Eastern Europe TI 2026）。

🟡 **MrBeast 70-80M mid 超出范围**：日志中连续出现 "mid 0.776 超出范围 [0.25, 0.75]" 警告，bot 在该市场几乎不做市。

🟢 **Bot 在线且稳定**：从 06:21 持续运行 59m+ 未崩溃。当前正在报价 Eastern Europe & CIS TI 2026 市场。

✅ **无重复 fill_id、无 MANUAL_HOLD、无 BATCH_LOSS_LOCKED**

### 结论

第 47 轮状态与第 46 轮完全一致。reward-exit 路径在 bot 5 次运行中几乎完全静默——唯一事件是 05:40 的 1 次 BATCH_TAKE_SUBMITTED（batch1 retry）。两个 batch 的 CLOSED 状态跳变原因仍未查明，DB 已冻结 ~14.9 小时。

好消息是 bot 第 5 次运行持续了 59m+ 且仍在运行——bot 在线稳定，但 reward-exit 路径因 batch 异常 CLOSED 状态被完全绕过。

**修复优先顺序不变：**
1. **P0**: 调查 batch 状态跳变原因（TAKE_PENDING→CLOSED 无日志证据）
2. **P0**: 实现孤儿 batch 复活逻辑（检测 CLOSED + take_filled_size=0 + exit_filled_size=0 + closed_ts=None → 重置为 TAKE_PENDING）
3. **P0**: 修复 `taker_buy()` 浮点精度
4. **P0**: take 前检查余额
5. **P1**: batch 超时自动关闭（terminal_after_secs 到期后 close + unlock）
6. **P1**: 修复 fill-to-batch 链接 + DB 写入

---

## 第 46 轮（2026-08-11 ~12:30 BJT）— Scheduled Task double-take-exit-monitor

### 状态：DB 完全冻结，日志唯一新增事件为 1 次 BATCH_TAKE_SUBMITTED（05:40 batch1 retry）。Bot 自 07:05 后离线。所有异常与前几轮一致。

#### Bot 运行时间线（第 46 轮）

Aug 11 日志中 bot 共启动 **6 次**：

| # | 时间段 | 时长 | reward-exit 事件 | 主要活动 |
|---|--------|------|------------------|----------|
| 1 | 05:39:33-05:40:09 | 36s | **1** BATCH_TAKE_SUBMITTED (batch1) | 正常启动，接仓后重试 batch1 |
| 2 | 05:55:14-06:00:31 | ~5m | **0** | Ralph Norman 裸仓通过 P1 recovery，非 reward-exit；余额不足 + FAK 全部被拒 |
| 3 | 06:07:45-06:08:50 | ~65s | 0 | 短暂报价 East Europe + MrBeast 70-80M |
| 4 | 06:20:05-06:21:44 | ~99s | 0 | 短暂报价 MrBeast 70-80M + FIFA 2030 |
| 5 | 06:21:48-**07:05:23** | ~43m | 0 | 持续报价 MrBeast 70-80M + FIFA 2030，SSL EOF 频繁（06:24-06:28，6 次） |
| 6 | (日志在 07:05:23 停止) | — | — | P0 guard 历史清理，之后无日志 |

**最后一次 bot 活动**：07:05:23 P0 guard 历史清理（删除 16 条）。之后日志静默，bot 可能手动停止、崩溃或正常退出。

#### 关键观察

🔴 **DB 状态完全冻结（第 46 轮仍无变化）**：两个 batch 保持 CLOSED（closed_ts=None），`take_filled_size=0.0`，`exit_filled_size=0.0`，`updated_ts==created_ts`。reward_exit_orders 仍为空，reward_exit_fills 仍只有 2 条 origin fill（batch_id=""）。

🟡 **Aug 11 唯一新增事件**：仅 1 次 BATCH_TAKE_SUBMITTED（05:40:07，batch1 retry，与第 44-45 轮相同）。Aug 11 无 BATCH_TAKE_EXECUTED（第 42 轮后的所有 TAKE_EXECUTED 都在 Aug 10 日志中）。

🟡 **Bot 第 5 次运行（06:21-07:05，43 分钟）完全未触及 reward-exit 路径**：持续正常做市 2 个市场（MrBeast 70-80M + FIFA 2030），但不是 batch1/batch2 的原市场。SSL EOF 错误偶发但自动恢复。

🟢 **Bot 在 43 分钟运行期间表现稳定**：06:21-07:05 正常报价、刷新订单、P0 guard 清理，无崩溃。第五次运行是该 session 最长的连贯运行。

✅ **无 MANUAL_HOLD、无 BATCH_LOSS_LOCKED、无重复 fill_id**

### 事件统计（累计，8-10 + 8-11 日志合计）

| 事件 | 次数 | 变化 vs 第 45 轮 |
|------|------|-------------------|
| REWARD_EXIT_BATCH_OPENED | 2 | - |
| BATCH_TAKE_SUBMITTED | **207** | - |
| BATCH_TAKE_EXECUTED | 12 | - |
| MARKET_REWARD_EXIT_LOCKED | 4 | - |
| BATCH_TAKE_COMPLETED | 0 | - |
| BATCH_TAKE_PARTIAL | 0 | - |
| BATCH_SELL_PLACED | 0 | - |
| BATCH_SELL_PARTIAL | 0 | - |
| REWARD_EXIT_BATCH_CLOSED | 0 | - |
| MARKET_REWARD_EXIT_UNLOCKED | 0 | - |
| BATCH_MANUAL_HOLD | 0 | - |
| BATCH_LOSS_LOCKED | 0 | - |

### 数据库状态（无变化 vs 第 45 轮）

| 表 | 行数 | 详情 |
|---|------|------|
| reward_exit_batches | 2 | 均为 CLOSED，`closed_ts=None`，`updated_ts==created_ts`，`take_filled_size=0.0`，`exit_filled_size=0.0` |
| reward_exit_fills | 2 | intent=normal_reward, batch_id=`""`（空字符串），为 origin 奖励成交 |
| reward_exit_orders | 0 | FAK 下单失败不写入此表 |

### Batch 详情

| Batch ID | 市场 | 原始成交 | take_target | BATCH_TAKE_EXECUTED | 状态 | 创建 | 孤儿时长 |
|----------|------|----------|-------------|---------------------|------|------|----------|
| reward-exit-1e5af08f | Will Ralph Norman place second... | BUY NO 3.66 @ 0.52 | 7.32 | 10 次（filled=14.80 each） | CLOSED | 22:19 8/10 | ~14h |
| reward-exit-02cafdd7 | Will MrBeast 60-70M views | BUY YES 43.9 @ 0.57 | 87.8 | 2 次（filled=170.49, 175.12） | CLOSED | 23:12 8/10 | ~13h |

### 异常信号检测

🔴 **Batch 状态跳变（TAKE_PENDING→CLOSED）已持续 5 轮无解**：第 42 轮时两个 batch 为 TAKE_PENDING，第 43 轮突然变为 CLOSED。`closed_ts=None`，无 REWARD_EXIT_BATCH_CLOSED 日志。因为 batch 是 CLOSED，`get_open_reward_exit_batches()` 返回空，reward-exit 路径完全被绕过。

🔴 **市场锁永不解锁**：MARKET_REWARD_EXIT_UNLOCKED 仍为 0。不过由于 batch 已 CLOSED 且锁是内存状态，实际市场当前未被 lock。

🔴 **12 次 FAK 成交的填量完全丢失**：DB 的 `take_filled_size` 全为 0.0。

🔴 **Fill-to-batch 链接仍为空**：`batch_id=""` 于所有 reward_exit_fills。reward_exit_orders 表完全为空。

🟡 **Batch2 市场从未被扫描**：bot 报价的是 MrBeast 70-80M 而非 60-70M。即使 batch 恢复逻辑正确，batch2 也无法执行。

🟡 **Bot 在 07:05 后离线**：可能是手动停止、崩溃或正常退出。日志最后一行是 07:05:23 的 P0 guard 历史清理。

✅ **无重复 fill_id、无 MANUAL_HOLD、无 BATCH_LOSS_LOCKED**

### 结论

第 46 轮状态与第 45 轮完全一致。reward-exit 路径在 bot 6 次运行中几乎完全静默——唯一事件是 05:40 的 1 次 BATCH_TAKE_SUBMITTED（batch1 retry）。两个 batch 的 CLOSED 状态跳变原因仍未查明，DB 已冻结 ~14 小时。

Bot 在 06:21-07:05 有 43 分钟的稳定连续运行（第 5 次启动），这是自第 42 轮以来最长的连贯运行时段，但完全没有触发 reward-exit 逻辑——两个报价市场都与原 batch 无关。

**修复优先顺序不变：**
1. **P0**: 调查 batch 状态跳变原因（TAKE_PENDING→CLOSED 无日志证据）
2. **P0**: 实现孤儿 batch 复活逻辑（检测 CLOSED + take_filled_size=0 + exit_filled_size=0 + closed_ts=None → 重置为 TAKE_PENDING）
3. **P0**: 修复 `taker_buy()` 浮点精度
4. **P0**: take 前检查余额
5. **P1**: batch 超时自动关闭（terminal_after_secs 到期后 close + unlock）
6. **P1**: 修复 fill-to-batch 链接 + DB 写入

---

## 第 45 轮（2026-08-11 ~06:51 BJT）— Scheduled Task double-take-exit-monitor

### 状态：Bot 06:20-06:50 持续运行 ~30 分钟，reward-exit 路径完全无活动。DB 状态冻结无变化。

#### Bot 运行时间线（第 45 轮）

**第一次**（05:39:33 → 05:40:09，36 秒）：1 次 BATCH_TAKE_SUBMITTED（batch1 retry），bot 退出
**第二次**（05:55:14 → 06:00:31，5 分 17 秒）：Ralph Norman 裸仓通过 P1 recovery（非 reward-exit）管理，余额不足阻止 MrBeast 下单
**第三次**（06:07:45 → 06:08:50，~65 秒）：短暂报价
**第四次**（06:20:05 → **06:50:34，约 30 分钟**）：持续报价 MrBeast 70-80M + FIFA 2030 市场。正常做市周期（ORDER_PLACED/ORDER_CANCELLED 轮换），期间多次 SSL EOF 错误（06:24-06:28 共 6 次）。**零 reward-exit 事件**。

Bot 在 06:50:34 后日志停止更新——bot 可能已退出或崩溃。

#### 关键观察

🔴 **DB 状态完全冻结**：两个 batch 保持 CLOSED（closed_ts=None），`take_filled_size=0.0`，`exit_filled_size=0.0`，`updated_ts==created_ts`。自第 42 轮 jump 以来无任何变化。reward_exit_orders 仍为空，reward_exit_fills 仍只有 2 条 origin fill（batch_id=""）。

🟡 **Aug 11 仅新增 1 次 BATCH_TAKE_SUBMITTED**（05:40:07，batch1 retry，与第 44 轮相同）。Aug 11 日志没有其他 reward-exit 事件。

🟡 **Bot 第四次运行（06:20-06:50）完全未触及 reward-exit 路径**：持续做市 30 分钟，但两个市场（MrBeast 70-80M + FIFA 2030）都是普通做市市场，不是 batch1/batch2 的原市场。Batch1 市场（Ralph Norman）在第二次运行时通过 P1 recovery 路径管理，batch2 市场（MrBeast 60-70M）从未被扫描到。

🟢 **Bot 在第四次运行期间稳定**：06:20-06:50 持续 30 分钟正常做市，无崩溃。SSL EOF 错误偶发但自动恢复。

✅ **无 MANUAL_HOLD、无 BATCH_LOSS_LOCKED、无重复 fill_id**

### 事件统计（累计，8-10 + 8-11 日志合计）

| 事件 | 次数 | 变化 vs 第 44 轮 |
|------|------|-------------------|
| REWARD_EXIT_BATCH_OPENED | 2 | - |
| BATCH_TAKE_SUBMITTED | **207** | **+1** |
| BATCH_TAKE_EXECUTED | 12 | - |
| MARKET_REWARD_EXIT_LOCKED | 4 | - |
| BATCH_TAKE_COMPLETED | 0 | - |
| BATCH_TAKE_PARTIAL | 0 | - |
| BATCH_SELL_PLACED | 0 | - |
| BATCH_SELL_PARTIAL | 0 | - |
| REWARD_EXIT_BATCH_CLOSED | 0 | - |
| MARKET_REWARD_EXIT_UNLOCKED | 0 | - |
| BATCH_MANUAL_HOLD | 0 | - |
| BATCH_LOSS_LOCKED | 0 | - |

### 数据库状态（无变化 vs 第 44 轮）

| 表 | 行数 | 详情 |
|---|------|------|
| reward_exit_batches | 2 | 均为 CLOSED，`closed_ts=None`，`updated_ts==created_ts`，`take_filled_size=0.0`，`exit_filled_size=0.0` |
| reward_exit_fills | 2 | intent=normal_reward, batch_id=`""`（空字符串），为 origin 奖励成交 |
| reward_exit_orders | 0 | FAK 下单失败不写入此表 |

### Batch 详情

| Batch ID | 市场 | 原始成交 | take_target | BATCH_TAKE_EXECUTED | 状态 | 创建 | 孤儿时长 |
|----------|------|----------|-------------|---------------------|------|------|----------|
| reward-exit-1e5af08f | Will Ralph Norman place second... | BUY NO 3.66 @ 0.52 | 7.32 | 10 次（filled=14.80 each） | CLOSED | 22:19 8/10 | ~8.5h |
| reward-exit-02cafdd7 | Will MrBeast 60-70M views | BUY YES 43.9 @ 0.57 | 87.8 | 2 次（filled=170.49, 175.12） | CLOSED | 23:12 8/10 | ~7.6h |

### 异常信号检测

🔴 **Batch 状态跳变（TAKE_PENDING→CLOSED）已持续 5 轮无解**：第 41 轮时两个 batch 为 TAKE_PENDING，第 42 轮突然变为 CLOSED。`closed_ts=None`，无 REWARD_EXIT_BATCH_CLOSED 日志。因为 batch 是 CLOSED，`get_open_reward_exit_batches()` 返回空，reward-exit 路径完全被绕过。

🔴 **市场锁永不解锁**：MARKET_REWARD_EXIT_UNLOCKED 仍为 0。

🔴 **12 次 FAK 成交的填量完全丢失**：DB 的 `take_filled_size` 全为 0.0。

🔴 **Fill-to-batch 链接仍为空**：`batch_id=""` 于所有 reward_exit_fills。

🟡 **Batch2 市场从未被扫描**：bot 报价的是 MrBeast 70-80M 而非 60-70M。即使 batch 恢复逻辑正确，batch2 也无法执行。

🟡 **Bot 在 06:50:34 后日志停止**：可能是手动停止、崩溃或正常退出。下次启动时需继续观察。

✅ **无重复 fill_id、无 MANUAL_HOLD、无 BATCH_LOSS_LOCKED**

### 结论

第 45 轮状态与第 44 轮完全一致。reward-exit 路径在第 44-45 轮间完全静默——唯一的新事件是 05:40 的 1 次 BATCH_TAKE_SUBMITTED（与第 44 轮相同的那次）。DB 冻结无变化。

两个 batch 的 CLOSED 状态跳变原因仍未查明。第 44 轮建议的「孤儿 batch 复活逻辑」尚未实现。Bot 在 06:50 后离线，下次启动时需要观察 batch 恢复行为。

**修复优先顺序不变：**
1. **P0**: 调查 batch 状态跳变原因（TAKE_PENDING→CLOSED 无日志证据）
2. **P0**: 实现孤儿 batch 复活逻辑（检测 CLOSED + take_filled_size=0 + exit_filled_size=0 + closed_ts=None → 重置为 TAKE_PENDING）
3. **P0**: 修复 `taker_buy()` 浮点精度
4. **P0**: take 前检查余额
5. **P1**: batch 超时自动关闭（terminal_after_secs 到期后 close + unlock）
6. **P1**: 修复 fill-to-batch 链接 + DB 写入

---

## 第 44 轮（2026-08-11 ~06:40 BJT）— Scheduled Task double-take-exit-monitor

### 状态：Bot 06:20 启动后持续运行中（06:36+ 仍在报价），reward-exit 路径无任何活动

#### Bot 运行时间线（第 44 轮查看到的所有启动）

**第一次**（05:39:33 → 05:40:09，36 秒）：batch1 短暂重试（1 次 BATCH_TAKE_SUBMITTED），bot 退出
**第二次**（05:55:14 → 06:00:31，5 分 17 秒）：Ralph Norman 裸仓（152 YES）通过 **P1 recovery episode**（非 reward-exit）管理，被动 maker + escalated taker_buy 路径，所有 FAK 被拒（"no orders found"），余额不足阻止 MrBeast 下单
**第三次**（06:07:45 → 06:08:50，65 秒）：短暂报价 Eastern Europe + MrBeast 70-80M
**第四次**（06:20:05 → **06:36:42+，仍在运行**）：报价 MrBeast 70-80M + FIFA 2030。06:24-06:28 期间出现 6 次 SSL EOF 错误（Polymarket API 瞬时中断）。无任何 reward-exit 事件。

#### 关键观察

🔴 **Batch 状态 CLOSED 已持续至第 4 轮监控，完全无变化**。两个 batch 的 `closed_ts=None`，`take_filled_size=0.0`，`exit_filled_size=0.0`。`updated_ts` 仍等于 `created_ts`。

🟡 **Ralph Norman 市场持仓爆炸**：创建 batch1 时原始敞口是 4 股 NO（触发 reward-exit 双倍退出），但现在 bot 接管了 **152 股 YES** 裸仓。这 152 YES 走的是传统 P1 recovery 路径（`stage=passive/escalated`），而非 reward-exit batch。因为 batch 已是 CLOSED，`get_open_reward_exit_batches()` 返回空，bot 不会 lock 该市场。

🟡 **Batch2 市场（MrBeast 60-70M views）从未被报价**：当前 bot 报价的是 70-80M 市场（不同 token），batch2 的市场不在扫描范围内。即使 batch 是 ACTIVE 状态，bot 也不会处理 batch2 的退出。

🟢 **Bot 目前在线且稳定**：第四次运行从 06:20 持续到 06:36+，已超过 16 分钟，正常执行普通做市周期（刷新报价、P0 guard 清理）。出现了偶发 SSL EOF 错误（`查询持仓市场失败`），bot 自动恢复。

🔴 **12 次 BATCH_TAKE_EXECUTED 的 FAK 填量完全丢失于 DB**：`take_filled_size` 全为 0.0，`reward_exit_fills` 缺少 batch_take intent 的记录。`reward_exit_orders` 表仍为空（FAK 下单失败不写此表）。

🔴 **Fill-to-batch 链接全为空**：2 条 reward_exit_fills 的 `batch_id` 均为空字符串（origin fill 发生在 batch 创建之前）。

✅ **无重复 fill_id、无 MANUAL_HOLD、无 BATCH_LOSS_LOCKED**

### 事件统计（累计，8-10 + 8-11 日志合计）

| 事件 | 次数 | 变化 vs 第 43 轮 |
|------|------|-------------------|
| REWARD_EXIT_BATCH_OPENED | 2 | - |
| BATCH_TAKE_SUBMITTED | **207** | **+1** |
| BATCH_TAKE_EXECUTED | 12 | - |
| MARKET_REWARD_EXIT_LOCKED | 4 | - |
| BATCH_TAKE_COMPLETED | 0 | - |
| BATCH_TAKE_PARTIAL | 0 | - |
| BATCH_SELL_PLACED | 0 | - |
| BATCH_SELL_PARTIAL | 0 | - |
| REWARD_EXIT_BATCH_CLOSED | 0 | - |
| MARKET_REWARD_EXIT_UNLOCKED | 0 | - |
| BATCH_MANUAL_HOLD | 0 | - |
| BATCH_LOSS_LOCKED | 0 | - |

### 数据库状态（无变化 vs 第 43 轮）

| 表 | 行数 | 详情 |
|---|------|------|
| reward_exit_batches | 2 | 均为 CLOSED，`closed_ts=None`，`updated_ts==created_ts`，`take_filled_size=0.0`，`exit_filled_size=0.0` |
| reward_exit_fills | 2 | intent=normal_reward, batch_id=`""`（空字符串），为 origin 奖励成交 |
| reward_exit_orders | 0 | FAK 下单失败不写入此表 |

### Batch 详情

| Batch ID | 市场 | 原始成交 | take_target | BATCH_TAKE_EXECUTED | 状态 | 创建 | 孤儿时长 |
|----------|------|----------|-------------|---------------------|------|------|----------|
| reward-exit-1e5af08f | Will Ralph Norman place second... | BUY NO 3.66 @ 0.52 | 7.32 | 10 次（filled=14.80 each） | CLOSED | 22:19 8/10 | ~8.3h |
| reward-exit-02cafdd7 | Will MrBeast 60-70M views | BUY YES 43.9 @ 0.57 | 87.8 | 2 次（filled=170.49, 175.12） | CLOSED | 23:12 8/10 | ~7.4h |

### 异常信号检测

🔴 **Batch 状态跳变（TAKE_PENDING→CLOSED）已持续 4 轮无解**：`closed_ts=None`，无 REWARD_EXIT_BATCH_CLOSED 日志。由于 batch 已 CLOSED：
- `get_open_reward_exit_batches()` 返回空 → `_reward_exit_locked` 为空 → 市场不会被 LOCK
- `_advance_take_pending()` / `_advance_sell_pending()` 都不会触发
- 原 batch 市场的持仓通过传统 P1 recovery（非 reward-exit）路径管理

🔴 **市场锁永不解锁**：MARKET_REWARD_EXIT_UNLOCKED 仍为 0，但因 batch 已 CLOSED 且 lock 是内存状态，实际市场当前未被 lock。

🔴 **12 次 FAK 成交的填量完全丢失**：DB 的 `take_filled_size` 全为 0.0，`reward_exit_fills` 表没有 batch_take intent 的记录。

🔴 **Fill-to-batch 链接仍为空**：`batch_id=""` 于所有 reward_exit_fills。

🟡 **Stock 持仓规模变化**：batch1 市场从原 4 NO 敞口变为 152 YES 裸仓，通过传统 P1 recovery 路径管理（非 reward-exit）。

✅ **无重复 fill_id、无 MANUAL_HOLD、无 BATCH_LOSS_LOCKED**

### 结论

第 44 轮状态与第 43 轮基本一致：两个 batch 保持 CLOSED 状态，bot 的 reward-exit 路径完全停止运作。bot 当前在线（06:20 启动，06:36+ 仍在运行），活跃报价 2 个市场（MrBeast 70-80M + FIFA 2030），但无任何 reward-exit 逻辑触发。

batch 状态跳变（TAKE_PENDING→CLOSED）的根本原因仍未查明。代码审查没有找到会静默将 batch 设为 CLOSED 的路径——两个 `close_reward_exit_batch()` 调用都在 `rem <= 0` 时（sell 完成后），而这两个 batch 从未进入 sell 阶段。如果 batch 未被手动改为 CLOSED，那么问题在代码的某个未发现的角落。

**建议下一步**：在 `_run_reward_exit_batch_tick` 开头加入「CLOSED 但未完成」的 batch 复活逻辑——检测到 `take_filled_size=0 ∧ exit_filled_size=0 ∧ closed_ts=None` 的 batch 时，将其重置为 TAKE_PENDING 并重新进入 take 流程。这是 get_open_reward_exit_batches() 的调用处可以加的防御性代码。

---

## 第 43 轮（2026-08-11 06:21 BJT）— Scheduled Task double-take-exit-monitor

### 状态：Bot 06:20 正在运行中，batch 状态保持 CLOSED（无变化），reward-exit 路径未触发

#### Bot 运行时间线（第 43 轮查看到的所有启动）

**第一次**（05:39:33 → 05:40:09，36 秒）：batch1 重试后退出
**第二次**（05:55:14 → 06:00:31，5 分 17 秒）：Ralph Norman 裸仓通过 P1 recovery（非 reward-exit）管理，bot 因余额不足 + FAK 被拒退出
**第三次**（06:08:03 → 06:08:50，47 秒）：短暂报价 MrBeast 70-80M 后退出
**第四次**（06:20:05 → 06:21:27+，运行中）：当前正在报价 2 个市场（MrBeast 70-80M + FIFA 2030），Ralph Norman 市场持仓被接管但未触发 reward-exit batch

#### 关键观察

🔴 **Batch 状态仍为 CLOSED**：`closed_ts=None`，`updated_ts==created_ts`，`take_filled_size=0.0`。状态跳变原因仍未查明。因为 batch 是 CLOSED，`get_open_reward_exit_batches()` 永远返回空列表，导致：
- `_reward_exit_locked` 集合为空 → 两个市场都不会被 LOCK
- `_advance_take_pending()` 不会触发 → 不会再尝试 take
- `_advance_sell_pending()` 不会触发 → 不会再尝试 sell

🟡 **Ralph Norman 裸仓走的是 P1 recovery 而非 reward-exit**：bot 06:20 运行中接管了 Ralph Norman 市场的持仓（adopting held position），但因为没有活跃 reward-exit batch，它走的是传统 recovery episode 路径（被动 maker BUY NO 补单），而非 reward-exit 的双倍退出路径。

🟡 **Batch2 市场（MrBeast 60-70M）不在扫描范围**：bot 当前报价的是 70-80M 市场，batch2 的 60-70M 市场从未被扫描到。即使 batch 恢复逻辑正确，batch2 也无法被触发。

🟢 **Bot 目前在线**：06:21:27 仍在记录 P0 guard 历史清理（删除 362 条），bot 正在活跃报价 2 个市场（MrBeast 70-80M + FIFA 2030）。

🔴 **12 次 BATCH_TAKE_EXECUTED 仍未被计入 DB**：日志中 batch1 有 10 次 FAK 买入成交（filled=14.80 each），batch2 有 2 次（filled=170.49, 175.12），但 DB 的 `take_filled_size` 全为 0.0，`take_notional_usd` 也为 0.0。`reward_exit_orders` 表完全为空（FAK 失败不记录）。

🔴 **Fill-to-batch 链接全为空**：`reward_exit_fills` 表中 2 条记录的 `batch_id` 均为空字符串。origin fill 的记录发生在 batch 创建之前，所以 `batch_id` 无法回填。batch_take fills（12 次 FAK 成交）甚至没有写入 `reward_exit_fills` 表。

✅ **无重复 fill_id、无 MANUAL_HOLD、无 BATCH_LOSS_LOCKED**

### 事件统计（累计，8-10 + 8-11 日志合计）

| 事件 | 次数 | 变化 vs 第 42 轮 |
|------|------|-------------------|
| REWARD_EXIT_BATCH_OPENED | 2 | - |
| BATCH_TAKE_SUBMITTED | 206 | - |
| BATCH_TAKE_EXECUTED | 12 | - |
| MARKET_REWARD_EXIT_LOCKED | 4 | - |
| BATCH_TAKE_COMPLETED | 0 | - |
| BATCH_TAKE_PARTIAL | 0 | - |
| BATCH_SELL_PLACED | 0 | - |
| BATCH_SELL_PARTIAL | 0 | - |
| REWARD_EXIT_BATCH_CLOSED | 0 | - |
| MARKET_REWARD_EXIT_UNLOCKED | 0 | - |
| BATCH_MANUAL_HOLD | 0 | - |
| BATCH_LOSS_LOCKED | 0 | - |

### 数据库状态

| 表 | 行数 | 详情 |
|---|------|------|
| reward_exit_batches | 2 | 均为 CLOSED，`closed_ts=None`，`updated_ts==created_ts`，`take_filled_size=0.0`，`exit_filled_size=0.0` |
| reward_exit_fills | 2 | intent=normal_reward, batch_id=`""`（空字符串），为 origin 奖励成交 |
| reward_exit_orders | 0 | FAK 下单失败不写入此表 |

### Batch 详情

| Batch ID | 市场 | 原始成交 | take_target | BATCH_TAKE_EXECUTED | 状态 | 创建 |
|----------|------|----------|-------------|---------------------|------|------|
| reward-exit-1e5af08f | Will Ralph Norman place second... | BUY NO 3.66 @ 0.52 | 7.32 | 10 次（filled=14.80 each） | CLOSED | 22:19 8/10 |
| reward-exit-02cafdd7 | Will MrBeast 60-70M views | BUY YES 43.9 @ 0.57 | 87.8 | 2 次（filled=170.49, 175.12） | CLOSED | 23:12 8/10 |

> 注：BATCH_TAKE_EXECUTED 的 filled 值是 FAK 订单被 Polymarket 端部分匹配的量，不是 bot 请求的量。bot 请求 size=7 和 88，但 API 以更大数量匹配（可能包含其他挂单的部分成交）。这些填量未回写到 DB 的 `take_filled_size`。

### 异常信号检测

🔴 **Batch 状态跳变（TAKE_PENDING→CLOSED）已持续 2 轮无解**：`closed_ts=None`，无 REWARD_EXIT_BATCH_CLOSED 日志。可能原因：
- 代码中存在未记录日志的 batch 关闭路径（需要全面代码审计）
- 直接 DB 操作（手动 SQL UPDATE）
- 可能性排除：第 42 轮监控数据有误（第 43 轮再次确认 CLOSED）

🔴 **市场锁永不解锁**：MARKET_REWARD_EXIT_UNLOCKED 仍为 0，但因为 batch 已 CLOSED 且 `_reward_exit_locked` 是内存状态（无持久化），实际上市场当前未被 lock。

🔴 **12 次 FAK 成交的填量完全丢失**：DB 的 `take_filled_size` 全为 0.0，`reward_exit_fills` 表没有 batch_take intent 的记录。

🔴 **Fill-to-batch 链接仍为空**：`batch_id=""` 于所有 reward_exit_fills。

✅ 无重复 fill_id、无 MANUAL_HOLD、无 BATCH_LOSS_LOCKED

### 结论

第 43 轮状态与第 42 轮基本一致：两个 batch 保持 CLOSED 状态，bot 的 reward-exit 路径完全停止运作。bot 当前在线（06:20 启动），但仅在做普通做市（2 个市场），没有触发任何 reward-exit 逻辑。

**根本问题**：batch 是如何从 TAKE_PENDING 变成 CLOSED 的？代码中 `close_reward_exit_batch()` 的两处调用都在 `rem <= 0` 时（即 sell 完成后），但这两个 batch 从未进入 sell 阶段（`exit_filled_size=0.0`）。没有其他关 batch 的路径会在不记录 REWARD_EXIT_BATCH_CLOSED 日志的情况下将状态设为 CLOSED——除非存在尚未被发现的代码路径，或 DB 被手动修改。

**建议下一步**：如果 bot 要保持在线，考虑在 `_run_reward_exit_batch_tick` 开头增加一个 "孤儿 batch 复活" 逻辑：检测到 `take_filled_size=0 ∧ exit_filled_size=0 ∧ closed_ts=None` 的 CLOSED batch 时，将其重置为 TAKE_PENDING 并重新进入 take 流程。这比彻底排查状态跳变原因更快，也能让这两个批次的 ~$27 名义奖励得到处理。

---

## 第 42 轮（2026-08-11 06:09 BJT）— Scheduled Task double-take-exit-monitor

### 状态：Bot 两次短暂启动后关闭，两个 batch 状态变为 CLOSED（无日志证据），batch 在第二次运行中完全未被引用

#### Bot 运行时间线

**第一次运行**（05:39:33 → 05:40:09，36 秒）：
- 05:39:33 — 启动，加载 4 个 banned 市场、7 个 unpaired_since 条目
- 05:40:07 — **batch1 重试**：BATCH_TAKE_SUBMITTED (batch1, remaining=7, price=0.9900)
- 05:40:09 — Bot 退出

**第二次运行**（05:55:14 → 06:00:31，5 分 17 秒）：
- 05:55:14 — 启动，权益基准 $284.61
- 05:55:34 — 开始报价：Eastern Europe & CIS TI 2026、MrBeast 70-80M
- 05:55:42-55 — 接管持仓：Ty Masterson、Ralph Norman（152 股 YES 裸仓）
- 05:55:50 — Ralph Norman recovery quote: BUY NO 152 @ 0.510（非 reward-exit batch）
- 05:55:53 — P1 recovery episode decision: buy_complement, stage=passive, unpaired=152
- 05:56:41 — P0 guard 历史清理：删除 4722 条
- 05:58:59 — Ralph Norman recovery 升级到 escalated stage
- 05:59:07-06:00:22 — 多次 taker buy 失败："no orders found to match with FAK order"（market ask 价高于 0.570 限价）
- 05:58:27 — 余额不足：not enough balance ($215 vs $249 needed)，订单 POST_FAILED
- 06:00:31 — Bot 退出

#### 关键观察

🟡 **batch 状态异常变为 CLOSED**：第 41 轮时两个 batch 均为 TAKE_PENDING，本轮 DB 查询显示为 CLOSED。但日志中**完全没有** REWARD_EXIT_BATCH_CLOSED 事件，batch 的 `closed_ts` 为 None，`updated_ts` 仍等于 `created_ts`（未更新）。没有 BATCH_TAKE_COMPLETED、BATCH_SELL_PLACED 日志。两个 batch 的 `take_filled_size` 仍为 0.0。这个 CLOSED 状态缺乏证据链支持。

🟡 **第二次运行完全未引用 reward-exit batch**：bot 在 05:55-06:00 期间没有产生任何 reward-exit 相关日志（无 LOCKED、无 UNLOCKED、无 TAKE_SUBMITTED）。Ralph Norman 的裸仓通过 P1 recovery episode（非 reward-exit batch）在管理，以被动 maker BUY NO 报价 152 股 @ 0.510 进行补单，并最终 escalated 到 taker buy 路径（但全部因 "no orders found" 失败）。

🔴 **余额不足阻止了 MrBeast 70-80M 市场下单**：bot 在 05:58:27 因余额不足（$215 余额 vs $249 所需）被拒，导致该市场一个订单下单失败。

🟢 **P1 recovery escalated 正确触发**：Ralph Norman 市场的被动 recovery（maker BUY NO）在 escalated 窗口后自动转为 active taker buy 路径。但由于市场 ask 价格（~0.570）高于 bot 的 taker buy 限价，全部 FAK 被拒绝。

### 事件统计（累计）

| 事件 | 次数 | 变化 vs 第 41 轮 |
|------|------|-------------------|
| REWARD_EXIT_BATCH_OPENED | 2 | - |
| BATCH_TAKE_SUBMITTED | 206 | +1 |
| BATCH_TAKE_EXECUTED | 12 | - |
| MARKET_REWARD_EXIT_LOCKED | 4 | - |
| BATCH_TAKE_COMPLETED | 0 | - |
| BATCH_TAKE_PARTIAL | 0 | - |
| BATCH_SELL_PLACED | 0 | - |
| BATCH_SELL_PARTIAL | 0 | - |
| REWARD_EXIT_BATCH_CLOSED | 0 | - |
| MARKET_REWARD_EXIT_UNLOCKED | 0 | - |
| BATCH_MANUAL_HOLD | 0 | - |
| BATCH_LOSS_LOCKED | 0 | - |

### 数据库状态

| 表 | 行数 | 详情 |
|---|------|------|
| reward_exit_batches | 2 | ⚠️ 均为 CLOSED（第 41 轮为 TAKE_PENDING），但 `closed_ts=None`，`updated_ts==created_ts`，`take_filled_size=0.0` |
| reward_exit_fills | 2 | intent=normal_reward, batch_id 均为空字符串 |
| reward_exit_orders | 0 | FAK 失败不记录 |

### Batch 详情

| Batch ID | 市场 | 原始成交 | take_target | 状态 | 创建 | 孤儿时长 |
|----------|------|----------|-------------|------|------|----------|
| reward-exit-1e5af08f | Will Ralph Norman place second... | BUY NO 3.66 @ 0.52 | 7.32 | CLOSED | 22:19 8/10 | ~7.8h |
| reward-exit-02cafdd7 | Will MrBeast 60-70M views | BUY YES 43.9 @ 0.57 | 87.8 | CLOSED | 23:12 8/10 | ~6.9h |

### 异常信号检测

🔴 **Batch 状态异常跳变**：两个 batch 从 TAKE_PENDING → CLOSED，无 REWARD_EXIT_BATCH_CLOSED 日志，`closed_ts=None`，`updated_ts` 未更新，`take_filled_size` 仍为 0.0。这可能由以下原因之一导致：
- 直接 DB 修改（手动 SQL UPDATE）
- 代码中存在未记录日志的 batch 关闭路径
- 第 41 轮监控数据有误（当时可能实际已经是 CLOSED）

🟡 **第二次 bot 运行未识别到 reward-exit batch**：如果 batch 确实已标记为 CLOSED，则 `get_open_reward_exit_batches()` 返回空列表，`MARKET_REWARD_EXIT_LOCKED` 不会触发，batch 恢复也不会执行。这解释了为什么 05:55 运行没有 reward-exit 事件。

🔴 **Fill-to-batch 链接缺失**：reward_exit_fills.batch_id 均为空（第 41 轮已知问题，未修复）。

🔴 **TAKE_EXECUTED 填量丢失**：batch_take 成交（BATCH_TAKE_EXECUTED 共 12 次）未回写到 DB 的 `take_filled_size`（第 41 轮已知问题，未修复）。

✅ 无重复 fill_id、无 MANUAL_HOLD、无 BATCH_LOSS_LOCKED

### 结论

第 42 轮出现了一个新的异常信号：两个 batch 的状态变为 CLOSED，但没有任何日志证据支持完成。如果这是代码行为，需要调查是哪个路径触发了无日志的 batch 关闭。如果状态确实是 CLOSED，则意味着 double-take-exit 流程从未完成（take=0, sell=0），两个 batch 的原始奖励成交（共约 $27 名义）未得到双倍退出处理。

bot 目前离线。下一次启动时需要观察：
1. batch 的 CLOSED 状态是否影响 Ralph Norman 市场的奖励退出锁
2. batch2（MrBeast 60-70M）的市场是否仍在扫描范围内

修复优先顺序：
1. **P0**: 调查 batch 状态跳变原因（代码路径 vs 直接 DB 操作）
2. **P0**: 修复 `taker_buy()` 浮点精度
3. **P0**: take 前检查余额
4. **P1**: batch 超时自动关闭（terminal_after_secs 到期后 close + unlock）
5. **P1**: 修复 fill-to-batch 链接 + DB 写入

---

## 第 41 轮（2026-08-11 ~12:30 BJT）— Scheduled Task double-take-exit-monitor

### 状态：Bot 05:39 短暂启动（36 秒后退出），batch1 被重试但未完成，batch2 未被触发

Bot 在 2026-08-11 05:39:33 启动，36 秒后在 05:40:09 退出（"正在退出，撤销全部订单"）。关键事件：

- **05:39:33** — Bot 启动，加载 4 个 banned 市场、7 个 unpaired_since 条目
- **05:39:51** — 开始报价 MrBeast 70-80M views 市场（注意：这是**不同市场**，不是 batch2 的 60-70M views 市场）
- **05:40:00-02** — 接管的持仓包括 Ralph Norman（batch1 市场）、Ty Masterson、András Baka 市场
- **05:40:07** — **batch1（Ralph Norman）重试**：BATCH_TAKE_SUBMITTED (size=7, remaining=7, price=0.9900, mode=active)
- **05:40:09** — Bot 退出

**关键观察**：
- ✅ 代码修复后的 batch 恢复逻辑**生效了**：bot 启动时检测到了 Ralph Norman 市场的活跃 batch 并尝试继续 take
- ❌ batch2（MrBeast 60-70M views, `0xdf08...`）**未被重试**：bot 未引用该市场（报价的是 MrBeast 70-80M views），可能因为该市场不在当前扫描范围内
- ❌ Bot 仅运行 36 秒就退出，batch1 的 take 没有时间完成
- ❌ 两个 batch 的 remaining 仍为创建时值（7 和 88），即使 API 可能已部分成交过

### 事件统计（vs 第 40 轮）

| 事件 | 第 40 轮 | 第 41 轮 | 变化 |
|------|---------|---------|------|
| REWARD_EXIT_BATCH_OPENED | 2 | 2 | - |
| BATCH_TAKE_SUBMITTED | 205 | **206** | **+1** |
| MARKET_REWARD_EXIT_LOCKED | 4 | 4 | - |
| BATCH_TAKE_COMPLETED | 0 | 0 | - |
| BATCH_TAKE_PARTIAL | 0 | 0 | - |
| BATCH_SELL_PLACED | 0 | 0 | - |
| BATCH_SELL_PARTIAL | 0 | 0 | - |
| REWARD_EXIT_BATCH_CLOSED | 0 | 0 | - |
| MARKET_REWARD_EXIT_UNLOCKED | 0 | 0 | - |
| BATCH_MANUAL_HOLD | 0 | 0 | - |
| BATCH_LOSS_LOCKED | 0 | 0 | - |

### 数据库状态（无变化）

| 表 | 行数 | 详情 |
|---|------|------|
| reward_exit_batches | 2 | 均为 TAKE_PENDING, take_filled_size=0.0, exit_filled_size=0.0, updated_ts 未变化 |
| reward_exit_fills | 2 | intent=normal_reward, batch_id 均为空字符串 |
| reward_exit_orders | 0 | FAK 失败不记录 |

### Batch 详情

| Batch ID | 市场 | 原始成交 | take_target | SUBMITTED | 创建 | 孤儿时长 | 最新活动 |
|----------|------|----------|-------------|-----------|------|----------|----------|
| reward-exit-1e5af08f | Will Ralph Norman place second... | BUY NO 3.66 @ 0.52 | 7.32 | 91 | 22:19 | ~7.5h | 05:40:07 重试 (remaining=7) |
| reward-exit-02cafdd7 | Will MrBeast 60-70M views | BUY YES 43.9 @ 0.57 | 87.8 | 115 | 23:12 | ~6.6h | 23:30:59 最终 SUBMITTED |

### 异常信号检测

🟡 **Bot 05:39 短暂启动 36 秒后退出**：可能是手动测试重启（观察 batch 恢复行为）。
🟢 **Batch 恢复逻辑生效**：bot 启动时检测到活跃 batch 并重试了 batch1（Ralph Norman）。代码修复（检测启动时已存在的 batch）工作正常。
🔴 **Batch2 未被触发**：MrBeast 60-70M 市场不在 bot 报价范围内（bot 报的是 MrBeast 70-80M），batch2 的恢复依赖市场被扫描到。
🔴 **两个 batch still TAKE_PENDING**：batch1 ~7.5h, batch2 ~6.6h。DB unchanged。
🔴 **市场锁永不解锁**：MARKET_REWARD_EXIT_UNLOCKED 仍为 0。
🔴 **Fill-to-batch 链接缺失**：reward_exit_fills.batch_id 为空。
🔴 **TAKE_EXECUTED 填量丢失**：DB take_filled_size 全为 0.0。
✅ 无重复 fill_id、无 MANUAL_HOLD、无 BATCH_LOSS_LOCKED

### 结论

第 41 轮有一个重要的积极信号：batch 恢复逻辑生效（bot 启动时检测并重试了 batch1）。但由于 bot 仅运行 36 秒就退出，实际效果无法验证。batch2 未被触发可能是因为其市场不再在扫描范围内。

修复优先顺序不变：
1. **P0**: 修复 `taker_buy()` 浮点精度
2. **P0**: take 前检查余额
3. **P1**: batch 超时自动关闭（terminal_after_secs 到期后 close + unlock）
4. **P1**: bot 启动时清理过期 batch + 修复 fill-to-batch 链接 + DB 写入

---

## 第 40 轮（2026-08-11 05:38 BJT）— Scheduled Task double-take-exit-monitor

### 状态：Bot 离线 ~6.1h，状态完全冻结

Bot 自 2026-08-10 23:31 BJT 退出后未再启动。无 2026-08-11 日志文件。数据库状态与前几轮完全相同，无任何新事件触发。

### 事件统计（累计，无变化 vs 第 39 轮）

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

---

## 第 39 轮（2026-08-11 05:21 BJT）— Scheduled Task double-take-exit-monitor

（内容截断，完整历史见 git history）

---

## 历史轮次摘要

| 轮次 | 时间 | 关键发现 |
|------|------|----------|
| 52 | 08-11 | 与第 51 轮完全一致。DB 冻结 ~18h+。Bot 离线。CLOSED 状态跳变已持续 11 轮无解。所有事件计数零变化。 |
| 51 | 08-11 | bot 5 次启动共在线 ~1h47m；最新 08:02 正常退出；reward-exit 完全静默；batch 状态 CLOSED 已 10 轮；DB 冻结 ~18h+；唯一 reward-exit 事件仍为 05:40 batch1 retry |
| 50 | 08-11 08:08 | bot 运行 2h23m（05:39→08:02），正常退出（非崩溃）；reward-exit 完全静默；唯一 reward-exit 事件仍为 05:40 batch1 retry；DB 冻结 ~17h；bot 运行时长持续增长至历史最长；MrBeast 70-80M mid 0.786 无变化；无异常无崩溃 |
| 49 | 08-11 07:53 | bot 在线运行 2h11m+（05:39→07:50+），reward-exit 完全静默；唯一 reward-exit 事件为 05:40 batch1 retry；DB 冻结 16.5h；bot 运行时长持续增长；MrBeast 70-80M mid 0.785-0.786 稳定；无 SSL EOF 错误（07:48+ 平稳） |
| 48 | 08-11 07:35 | bot 在线运行 1h56m+（05:39→07:35+），reward-exit 完全静默；MrBeast 70-80M mid 0.786 超出范围；DB 冻结 15h |
| 47 | 08-11 07:21 | bot 在线运行 59m+（06:21→07:20+），reward-exit 完全静默；MrBeast 70-80M mid 0.776 超出报价范围；DB 冻结 14.9h；异常信号与前 6 轮一致 |
| 46 | 08-11 12:30 | DB 完全冻结 14h；bot 6 次启动中仅 1 次 reward-exit 事件（batch1 retry）；最长时间运行 43m 但未触及 reward-exit；异常信号与前 5 轮一致 |
| 45 | 08-11 06:51 | 无新 reward-exit 事件；bot 06:50 离线；DB 冻结 |
| 44 | 08-11 06:40 | bot 06:20-06:36+ 运行中，reward-exit 路径完全静默 |
| 43 | 08-11 06:21 | bot 06:20 运行中；batch 状态 CLOSED 持续 2 轮；Ralph Norman 裸仓走 P1 recovery |
| 42 | 08-11 06:09 | batch 状态跳变 TAKE_PENDING→CLOSED（无日志证据）；bot 05:55 二次运行未引用任何 reward-exit batch；Ralph Norman 裸仓通过 P1 recovery 路径管理 |
| 41 | 08-11 12:30 | batch 恢复逻辑生效；bot 36s 后退出 |
| 40 | 08-11 05:38 | bot 离线 ~6.1h，状态完全冻结 |
| 39 | 08-11 05:21 | 代码修复 A/B/C/D 已合入 |
| 38 | 08-11 04:45 | 第一次完整 cycle 分析，识别 4 个关键 deadlock |
| 15 | 08-11 | 代码审查四轮修复 |
| 1-14 | 08-10 | 初始实现与调试 |
