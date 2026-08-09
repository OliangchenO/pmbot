# PMBot 问题追踪
上次扫描: 2026-08-09 20:01:18

> 自动生成，手动管理。标记为 `resolved` 或 `wontfix` 的问题不会再被自动重新打开，除非问题复现。

## 🔴 待处理 (5)

<!-- pmbot-issue: {"id": "PM-46422F", "group": "error-4f85948c", "status": "open", "first_seen": "2026-08-09 19:59:25", "severity": "🟠 High", "category": "ERROR", "last_seen": "2026-08-09 20:01:18", "count_window": {"2026-08-09 19:59:25": 1, "2026-08-09 20:01:18": 1}} -->
### [PM-46422F] 🟠 High · ERROR 🔴

- **描述**: 1 条 ERROR: ERROR    [py_clob_client_v2.http_helpers.helpers] [py_clob_client_v2] request er
- **当前窗口**: 1 次
- **详情**:
  ```
  2026-08-09 19:43:47 ERROR    [py_clob_client_v2.http_helpers.helpers] [py_clob_client_v2] request error status=400 url=https://clob.polymarket.com/auth/api-key body={"error":"Could not create api key"
  ```
- **首次发现**: 2026-08-09 19:59:25
- **最近出现**: 2026-08-09 20:01:18
- **状态**: open  ← 手动改为 `resolved` 或 `wontfix` 以关闭

---

<!-- pmbot-issue: {"id": "PM-C6220D", "group": "duplicate-orders", "status": "open", "first_seen": "2026-08-09 19:59:25", "severity": "🟠 High", "category": "重复报价", "last_seen": "2026-08-09 20:01:18", "count_window": {"2026-08-09 19:59:25": 1, "2026-08-09 20:01:18": 1}} -->
### [PM-C6220D] 🟠 High · 重复报价 🔴

- **描述**: 1 组重复报价: SELL x20.00 @0.700 (2p/0c)
- **当前窗口**: 1 次
- **详情**:
  ```
    SELL x20.00 @ 0.700 — placed 2, cancelled 0
  ```
- **首次发现**: 2026-08-09 19:59:25
- **最近出现**: 2026-08-09 20:01:18
- **状态**: open  ← 手动改为 `resolved` 或 `wontfix` 以关闭

---

<!-- pmbot-issue: {"id": "PM-4D4FE0", "group": "order-failures", "status": "open", "first_seen": "2026-08-09 19:59:25", "severity": "🟠 High", "category": "订单失败", "last_seen": "2026-08-09 20:01:18", "count_window": {"2026-08-09 19:59:25": 7, "2026-08-09 20:01:18": 7}} -->
### [PM-4D4FE0] 🟠 High · 订单失败 🔴

- **描述**: 7 条订单失败
- **当前窗口**: 7 次
- **详情**:
  ```
  2026-08-09 19:59:00 INFO     [pmbot.broker] ORDER event=ORDER_POST_FAILED market='Will FURIA Win CBLOL 2026 Split 2' price=0.600 size=115.90 side=BUY reason='not enough balance / allowance: the balanc
  2026-08-09 19:59:02 INFO     [pmbot.broker] ORDER event=ORDER_POST_FAILED market='Will FURIA Win CBLOL 2026 Split 2' price=0.600 size=115.90 side=BUY reason='not enough balance / allowance: the balanc
  2026-08-09 19:59:06 INFO     [pmbot.broker] ORDER event=ORDER_POST_FAILED market='Will FURIA Win CBLOL 2026 Split 2' price=0.600 size=115.90 side=BUY reason='not enough balance / allowance: the balanc
  2026-08-09 19:59:15 INFO     [pmbot.broker] ORDER event=ORDER_POST_FAILED market='Will FURIA Win CBLOL 2026 Split 2' price=0.600 size=115.90 side=BUY reason='not enough balance / allowance: the balanc
  2026-08-09 19:59:17 INFO     [pmbot.broker] ORDER event=ORDER_POST_FAILED market='Will FURIA Win CBLOL 2026 Split 2' price=0.600 size=115.90 side=BUY reason='not enough balance / allowance: the balanc
  ```
- **首次发现**: 2026-08-09 19:59:25
- **最近出现**: 2026-08-09 20:01:18
- **状态**: open  ← 手动改为 `resolved` 或 `wontfix` 以关闭

---

<!-- pmbot-issue: {"id": "PM-FF4B1B", "group": "recovery-phase2", "status": "open", "first_seen": "2026-08-09 19:59:25", "severity": "🟡 Medium", "category": "补单", "last_seen": "2026-08-09 20:01:18", "count_window": {"2026-08-09 19:59:25": 6, "2026-08-09 20:01:18": 6}} -->
### [PM-FF4B1B] 🟡 Medium · 补单 🔴

- **描述**: 6 个市场进入 Phase 2 补单
- **当前窗口**: 6 次
- **详情**:
  ```
  2026-08-09 19:44:18 WARNING  [pmbot] 补单阶段 'Phase 2 (升级: 盘口中枢补单)': Will another city host the final of the 2030 FIFA World Cup?  敞口=70
  2026-08-09 19:44:18 WARNING  [pmbot] 补单阶段 'Phase 2 (升级: 盘口中枢补单)': Will András Baka be the next President of Hungary?  敞口=-14
  2026-08-09 19:44:18 WARNING  [pmbot] 补单阶段 'Phase 2 (升级: 盘口中枢补单)': Will 25-49 ships transit the Strait of Hormuz between August  敞口=10
  2026-08-09 19:44:18 WARNING  [pmbot] 补单阶段 'Phase 2 (升级: 盘口中枢补单)': Will FURIA Win CBLOL 2026 Split 2  敞口=116
  2026-08-09 19:44:18 WARNING  [pmbot] 补单阶段 'Phase 2 (升级: 盘口中枢补单)': Will OpenAI have the best Text-to-Image AI at the end of Sep  敞口=-47
  ```
- **首次发现**: 2026-08-09 19:59:25
- **最近出现**: 2026-08-09 20:01:18
- **状态**: open  ← 手动改为 `resolved` 或 `wontfix` 以关闭

---

<!-- pmbot-issue: {"id": "PM-FB2112", "group": "other-warnings", "status": "open", "first_seen": "2026-08-09 19:59:25", "severity": "🔵 Low", "category": "WARNING", "last_seen": "2026-08-09 20:01:18", "count_window": {"2026-08-09 19:59:25": 37, "2026-08-09 20:01:18": 46}} -->
### [PM-FB2112] 🔵 Low · WARNING 🔴

- **描述**: 46 条其他 WARNING
- **当前窗口**: 46 次
- **详情**:
  ```
  2026-08-09 20:00:34 WARNING  [pmbot.broker] position refresh failed: [SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol (_ssl.c:1081)
  2026-08-09 20:00:48 WARNING  [pmbot.broker] position refresh failed: [SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol (_ssl.c:1081)
  2026-08-09 20:01:00 WARNING  [pmbot.broker] position refresh failed: [SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol (_ssl.c:1081)
  2026-08-09 20:01:14 WARNING  [pmbot.broker] position refresh failed: [SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol (_ssl.c:1081)
  2026-08-09 20:01:14 WARNING  [pmbot] INVENTORY_RECOVERY_SKIPPED market='Will Diogo Dalot attend Cristiano Ronaldo's wedding?' reason=near_resolution 原因=临近结算 unpaired=20 cost_basis=unknown yes_book=emp
  ```
- **首次发现**: 2026-08-09 19:59:25
- **最近出现**: 2026-08-09 20:01:18
- **状态**: open  ← 手动改为 `resolved` 或 `wontfix` 以关闭

---

## ✅ 已关闭 (0)

暂无已关闭问题。
