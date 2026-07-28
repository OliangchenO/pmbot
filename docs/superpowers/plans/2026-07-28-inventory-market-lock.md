# 带仓市场锁定实施计划

> **供执行代理使用：** 必须使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans`，逐任务实施。步骤采用复选框追踪。

**目标：** 未配对的 YES/NO 库存锁定其市场名额，并只报价互补方向，避免普通扫描换出后立即 taker 对冲。

**架构：** 在 `Bot._rescan()` 前识别带仓市场，并让它们先占用选池容量；选池只能填补剩余名额。`Bot._quote_all()` 根据未配对方向过滤现有策略报价并限制数量，库存管理因锁定市场仍在 `self.markets` 而不会将一次普通扫描视为紧急退出。

**技术栈：** Python 3、asyncio、pytest、现有 `PaperBroker` 和 `BookTracker`。

## 全局约束

- 后续方案、测试说明与新增日志使用中文。
- 不修改 live 凭据、不启动 live 交易进程；验证使用本地测试与 paper 组件。
- 仅当 `abs(unpaired_shares) >= MIN_TAKER_SHARES` 时锁定市场。
- 锁定市场占用 `top_n_markets` 名额；空仓市场保持既有粘性选池逻辑。
- 锁定市场只报价互补 token，订单数量不超过未配对股数。
- 结算窗口、现有风险限制、被动退出与强制对冲逻辑保持可用。

---

### 任务 1：锁定带仓市场并保留选池名额

**文件：**

- 修改：`pmbot/main.py:484-667`
- 测试：`tests/test_main.py`

**接口：**

- 新增：`Bot._locked_inventory_markets(markets: list[Market]) -> list[Market]`
- 修改：`Bot._select_markets(ranked: list[Market], locked: list[Market] | None = None) -> list[Market]`
- 消费：`self.broker.unpaired_shares(market)` 与 `MIN_TAKER_SHARES`
- 产生：包含锁定市场且总数不超过 `scanner.top_n_markets` 的 `self.markets`

- [ ] **步骤 1：写失败测试**

在 `tests/test_main.py` 增加以下场景：`top_n_markets=2`，旧集合为 `[held, flat]`，其中
`held` 有 20 股未配对 NO，下一次扫描只返回 `[flat, fresh]`。调用重新扫描后断言：

```python
assert [m.condition_id for m in bot.markets] == ["held", "flat"]
assert held.condition_id not in cancelled_market_ids
```

再将 `held` 的 NO 与 YES 设为相等，重复扫描，断言它不再锁定且由 `fresh` 填补名额：

```python
assert {m.condition_id for m in bot.markets} == {"flat", "fresh"}
assert held.condition_id in cancelled_market_ids
```

- [ ] **步骤 2：运行测试确认失败**

运行：

```powershell
rtk pytest .\tests\test_main.py -k inventory_market_lock
```

预期：失败，因为当前选池会把不在 `ranked` 中的 `held` 换出。

- [ ] **步骤 3：实现最小选池锁定逻辑**

在 `Bot` 中实现：

```python
def _locked_inventory_markets(self, markets: list[gamma.Market]) -> list[gamma.Market]:
    return [m for m in markets
            if abs(self.broker.unpaired_shares(m)) >= MIN_TAKER_SHARES]
```

在 `_rescan()` 中先计算锁定市场，再调用扩展后的 `_select_markets(ranked, locked)`。修改
`_select_markets()`：先放入锁定市场，将 `top_n_markets - len(locked)` 作为普通粘性选池
容量；锁定市场不参与“已持有但仍合格”的排序或替换判断。旧市场取消逻辑只取消未出现在
新集合中的空仓市场。

- [ ] **步骤 4：运行定向测试确认通过**

运行：

```powershell
rtk pytest .\tests\test_main.py -k inventory_market_lock
```

预期：新增的锁定、释放与撤单断言全部通过。

### 任务 2：锁定市场只挂互补方向且限额

**文件：**

- 修改：`pmbot/main.py:686-799`
- 测试：`tests/test_main.py`

**接口：**

- 消费：`self.broker.unpaired_shares(market)`、`strategy.compute_quotes()` 的 `list[Quote]`
- 产生：未配对 NO 时仅返回 YES 买单；未配对 YES 时仅返回 NO 买单；其 `size <= abs(unpaired)`

- [ ] **步骤 1：写失败测试**

在 paper broker 上构造正常双边盘口与 20 股未配对 NO，调用 `_quote_all()` 后断言：

```python
quotes = broker.open_quotes(market)
assert [q.token_id for q in quotes] == [market.yes_token]
assert quotes[0].size <= 20.0
```

将持仓改为 20 股未配对 YES，断言仅保留 `market.no_token`；将 YES/NO 设为相等，断言恢复
双边报价。

- [ ] **步骤 2：运行测试确认失败**

运行：

```powershell
rtk pytest .\tests\test_main.py -k complementary_quote
```

预期：失败，因为当前 `compute_quotes()` 仅按美元敞口偏斜，未保证单边与股数上限。

- [ ] **步骤 3：实现报价过滤器**

在 `_quote_all()` 的 `compute_quotes()` 调用之后、`reconcile_quotes()` 之前加入：

```python
unpaired = self.broker.unpaired_shares(m)
if abs(unpaired) >= MIN_TAKER_SHARES:
    complement = m.no_token if unpaired > 0 else m.yes_token
    desired = [strategy.Quote(q.token_id, q.price, min(q.size, abs(unpaired)))
               for q in desired if q.token_id == complement]
```

这会取消已持有方向的买单，并阻止互补方向买入超过待配对数量。

- [ ] **步骤 4：运行定向测试确认通过**

运行：

```powershell
rtk pytest .\tests\test_main.py -k complementary_quote
```

预期：NO 超额、YES 超额与配平恢复双边报价三个场景全部通过。

### 任务 3：回归验证与文档核对

**文件：**

- 验证：`tests/test_main.py`、完整 `tests/`
- 核对：`docs/superpowers/specs/2026-07-28-inventory-market-lock-design.md`

**接口：**

- 消费：任务 1、任务 2 的选池与报价行为。
- 产生：测试通过的实现，且无 live 运行。

- [ ] **步骤 1：运行相关模块测试**

运行：

```powershell
rtk pytest .\tests\test_main.py .\tests\test_brokers.py .\tests\test_strategy.py
```

预期：所有测试通过，尤其是待确认强制对冲与普通报价回归。

- [ ] **步骤 2：运行完整测试套件**

运行：

```powershell
rtk pytest .\tests
```

预期：全套通过，无失败或错误。

- [ ] **步骤 3：进行静态改动检查**

运行：

```powershell
rtk git diff --check
rtk git diff -- pmbot/main.py tests/test_main.py
```

预期：无空白错误；改动仅涉及带仓市场锁定与互补报价。

- [ ] **步骤 4：提交实现**

```powershell
rtk git add -- pmbot/main.py tests/test_main.py docs/superpowers/specs/2026-07-28-inventory-market-lock-design.md docs/superpowers/plans/2026-07-28-inventory-market-lock.md
rtk git commit -m "feat: lock inventory markets during rescans"
```

预期：仅在 Git 写入权限可用时创建提交；不得包含用户的 `.idea/` 文件。
