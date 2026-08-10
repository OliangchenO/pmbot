# pmbot 重启后监控报告 & 修复计划

**生成时间**: 2026-08-09 19:50 CST  
**项目**: D:\workspace\pmbot-guarder

---

## 1. 当前运行状态

| 指标 | 状态 | 说明 |
|------|------|------|
| Bot 进程 | ✅ 运行中 | 19:48 最新一次重启，使用 `config.yaml`（Live 模式） |
| 权益基准 | ✅ 正常 | 当前 $564.22（19:48:54 设置），上方 HARD KILL 前为 $565.59 |
| 报价循环 | ✅ 正常 | 正在为 Kai and Speed Minecraft、FIFA 2030 等市场报价 |
| WebSocket | ✅ 正常 | 8 个代币订单簿已订阅 |
| 日亏损上限 | $20.00 | 自适应控制器调整为 `daily_loss_limit_usd=20.0` |

**时间线**:  
- 14:32 启动 → 14:48-14:55 "权益未知"（531 条，启动初始化阶段，正常）
- 全天运行 → 17:45 首次退出+重启 → 18:54:38 HARD KILL
- 18:56 重启 → 全天继续运行 → 19:46 又一次退出/重启
- 19:48 最新重启，当前运行中

---

## 2. 发现的问题清单（按严重程度排序）

### 🔴 P0-1: Recovery Episode 运行在 `active` 模式

**根因**: `config.yaml` 中 `risk.recovery_episode_mode: active`，而 `config.debug.yaml` 中为 `shadow`。  
Live bot 使用 `config.yaml`，因此所有 recovery 决策都会实际执行 taker_buy 交易。

**影响**:
- 今日已执行 3 笔真实 taker_buy 恢复交易：

  | 时间 | 市场 | 操作 | 数量 | 价格 | 损失 |
  |------|------|------|------|------|------|
  | 17:46 | Kai and Speed Minecraft | BUY YES | 20 | 0.300 | $0.0080 |
  | 18:47 | Kai and Speed Minecraft | BUY NO | 40 | 0.690 | $0.4278 |
  | 18:54 | FIFA 2030 host city | BUY NO | 113 | 0.650 | **$8.0654** |

- 最后一笔 $8.0654 损失是导致日亏损飙至 $74.74（超过 $50 上限）的直接触发因素之一
- CLAUDE.md 文档明确要求 `shadow` 为默认模式，`active` 需用户授权

**修复计划**:
1. 将 `config.yaml` 中 `recovery_episode_mode` 改为 `shadow`
2. 确认用户是否需要 `active` 模式；如果是，需加入明确的确认提示

---

### 🔴 P0-2: HARD KILL 当日亏损 $74.74 >= $50.00

**根因**: 当日累计损失超过 daily_loss_limit_usd 上限。关键贡献来自 FIFA 2030 recovery 执行（$8.07）加上其他做市损失。

**影响**:
- Bot 在 18:54:38 被迫停止，已重启
- 权益从开盘的约 $640 降至 $564，当日跌幅约 $76

**修复计划**:
1. 重启后日亏损计数器已清零（新进程），当前正常运作
2. 恢复 `shadow` 模式可避免 recovery 交易贡献额外损失
3. 监控今日剩余时间是否还会触发 HARD KILL

---

### ⚠️ P1-1: `expected_loss_usd=unknown` 大面积出现

**根因**: 日志中 348 条 recovery 决策的 `expected_loss_usd=unknown`。  
在 `choose_recovery_action()` 中，当 `cost_basis` 无法获取时（例如 `broker.cost_basis(cid)` 返回 None），无法计算各路径的预期损失，决策器退化。

查看已有 `INVENTORY_RECOVERY_QUOTE` 日志，部分市场 cost_basis 是已知的（如 FURIA $0.510、Hormuz $0.680），说明 **并非所有市场都缺失 cost_basis**，而是 `choose_recovery_action()` 中获取 cost_basis 的方式与 `_manage_market_inventory()` 不一致。

**影响**:
- 决策器在 `expected_loss_usd=unknown` 时可能被迫走 `manual_hold`（无法计算损失会导致所有路径都因 `exceeds_loss_budget` 被排除）
- FURIA 和 Hormuz 两个市场被长期卡在 terminal + manual_hold 状态

**修复计划**:
1. 在 `pmbot/recovery.py` 的 `choose_recovery_action()` 中增加 debug 日志记录 `cost_basis` 的获取路径和结果
2. 确保 `cost_basis` 的获取方式与 `_market_recovery()` 中的方式一致（在 `_market_recovery()` 中 cost_basis 是已知的，如日志中的 `cost_basis=0.510`）
3. 短期 workaround：当 `expected_loss_usd=unknown` 时，使用 `_market_recovery()` 中显示的 cost_basis 作为 fallback

---

### ⚠️ P1-2: RECOVERY_SELL_ORIGINAL_FAILED 竞态

**时间**: 18:57:04  
**市场**: Will Amir Ohana win the 2026 Likud party primaries  
**详情**: 尝试 sell NO 107 @ 0.530，期望损失 $8.5248，但因为 "CLOB 条件代币余额不足 (balance=0)" 而失败

**根因**: 当 recovery episode 决定 `sell_original` 路径时，实际执行前仓位已被市场成交消耗（例如被动成交或对冲操作），导致 sell 时余额为 0。这是竞态条件。

**影响**:
- 该 episode 的 `sell_reserved_loss_usd` 未能实现
- 目前该市场的 unpaired 已从 live_state.json 中消失（从 6 条减至 5 条），说明仓位已通过其他途径平掉

**修复计划**:
1. `choose_recovery_action()` 决策后、执行 broker 调用前，应再检查一次当前持仓，避免已配平后的空操作
2. 或者将 `sell_original` 失败后的 episode 自动标记为 `closed_reason=flat`（仓位已平）

---

### ⚠️ P2-1: banned_markets.json 已累积 3 个市场

```json
{
  "banned_cids": [
    "0x60613b262912ce2e3138ec5610a31a2f932b6300843c094053cb33aaff9f2238",
    "0x8aa91fe3fe1ae8d1afc3d8c8b3fa9d1480ce14fb43e06d45137fd5ce3e965468",
    "0xcc2652557ae662b6cd110ca8c9ca5f16a741122b32ba5662791639480da48018"
  ]
}
```

**根因**: 这些市场某个 recovery episode 的 `actual_loss_usd` 累计超过 `recovery_loss_ban_threshold_usd: $15`。

**影响**:
- `0x8aa9...` 仍出现在 `live_state.json` 的 unpaired_since 列表中，但已被 banned
- 被禁市场永久排除在报价集合之外

**修复计划**:
1. 查看哪些市场被禁及其累计损失：`python -m pmbot.main recovery-episodes`
2. 评估是否应该手动清空或调整 `banned_markets.json`
3. 考虑增加 CLI 命令 `python -m pmbot.main unban <cid>` 用于手动解禁

---

### ⚠️ P2-2: API Key 创建失败（每次重启复现）

```
ERROR [py_clob_client_v2] request error: Could not create api key
```
出现于 17:28, 17:45, 18:53, 18:56, 19:48 — 每次 bot 重启时均出现。

**根因**: Polymarket API 的 create-api-key 端点返回 400。可能是短时间内重复创建导致限流，或 API 行为变更。

**影响**:
- 幸好 bot 后续仍能正常使用 Live 客户端操作，说明已有的 API key 仍有效
- 如果 API key 过期，bot 将无法下单

**修复计划**:
1. 确认当前使用的 API key 有效期
2. 增加 create-api-key 的重试和指数退避
3. 如果 API key 只需创建一次，考虑持久化并跳过重复创建

---

### ℹ️ P3-1: 两个市场长期卡在 Phase 2 terminal + manual_hold

| 市场 | 敞口 | cost_basis | 已等待 | 当前报价 | 预期配对 PnL |
|------|------|-----------|--------|---------|-------------|
| FURIA CBLOL | YES 29 | $0.510 | ~29h | BUY NO 29 @ $0.600 | -$0.110/股 |
| Strait of Hormuz | YES 45 | $0.680 | ~12h | BUY NO 45 @ $0.450 | -$0.130/股 |

两个市场的 **预期配对损失 > recovery_max_loss_usd_per_market ($3.00)**：
- FURIA：29 × $0.11 = $3.19 > $3.00
- Hormuz：45 × $0.13 = $5.85 > $3.00 (远远超出)

因此 `choose_recovery_action()` 返回 `manual_hold`（`exceeds_loss_budget`），同时旧 recovery 逻辑仍在报价（INVENTORY_RECOVERY_QUOTE）。

**修复计划**:
1. 如果采用 `active` 模式，recovery 模块应该覆盖旧的 INVENTORY_RECOVERY_QUOTE 逻辑（避免双重报价）
2. 如果保持 `shadow` 模式，旧逻辑正常运行，新的 episode 只做日志记录
3. 对于 $3/market 的 budget 是否过低，需要根据实际做市策略评估。Hormuz 的 $5.85 远超预算，可能确实是经济上不合理的补单

---

## 3. 综合修复优先级

| 优先级 | 编号 | 问题 | 行动 |
|--------|------|------|------|
| **立即** | P0-1 | recovery_episode_mode=active | 改 config.yaml 为 `shadow` |
| **立即** | P0-2 | HARD KILL $74.74 | 已重启，监控 |
| **高** | P1-1 | expected_loss_usd=unknown | 修复 cost_basis 获取逻辑 |
| **高** | P1-2 | sell_original 竞态 | 下单前二次确认仓位 |
| **中** | P2-1 | banned_markets 3个 | 评估是否需要解禁 |
| **中** | P2-2 | API key 创建失败 | 增加重试/持久化 |
| **低** | P3-1 | terminal manual_hold | 评估 $3 budget 是否过紧 |

---

## 4. config.yaml 建议修改

```yaml
# 当前（有问题）
risk:
  recovery_episode_mode: active   # ← 需改为 shadow

# 建议修改为
risk:
  recovery_episode_mode: shadow   # 安全默认，active 需显式确认
```

修改后重启 bot 生效：
```bash
python -m pmbot.main run
```

---

*自动生成 by pmbot-log-monitor scheduled task*
