# 退出订单生命周期实施计划

> **供执行 Agent 使用：** 必须逐项执行并使用 checkbox 跟踪。实施时先跑红灯测试，再写最小实现，最后运行验证；不得启动、重启或修改正在运行的 live 交易进程。

**目标：** 为被动退出 SELL 单提供独立、更长的 GTD 生命周期，减少同价同量退出单的无效撤挂，同时保留到期保护和防卖超顺序。

**架构：** `LiveBroker` 将普通买入报价与退出卖单的 GTD 到期时间分离：普通报价继续使用 `quoting.order_ttl_secs`，退出卖单使用 `risk.exit_order_ttl_secs`。`set_exit()` 的保留判断继续读取订单实际 `expiration`；要替换时保持 cancel-before-post。

**技术栈：** Python、pytest、py-clob-client-v2、YAML。

## 全局约束

- 当前 `mode: live` 进程不得启动、停止、重启或被修改。
- 普通双边报价的 `quoting.order_ttl_secs: 180` 与其 post-before-cancel 刷新逻辑不得改变。
- 退出 SELL 单不得采用 post-before-cancel，避免两张卖单同时成交而卖超。
- 不修改 forced hedge、`flatten_max_spread_cents`、市场筛选和市场数量。
- 新配置为 `risk.exit_order_ttl_secs: 600`；继续使用现有 `GTD_SECURITY_THRESHOLD_SECS` 和 `GTD_REFRESH_MARGIN_SECS`。

---

## 文件与职责

| 文件 | 职责 |
|---|---|
| `pmbot/brokers.py` | 生成独立退出订单到期时间；SELL 订单使用该时间。 |
| `config.yaml`、`config.debug.yaml` | 声明退出订单 600 秒 TTL。 |
| `tests/test_brokers.py` | 验证 SELL TTL，并回归验证不变订单保留与变更订单的撤后挂。 |

### 任务 1：以失败测试定义独立退出 TTL

**文件：**

- 修改：`tests/test_brokers.py:509-521` 附近

**接口：**

- 消费：`LiveBroker._place_sell(q: Quote) -> RestingOrder | None`
- 产出：SELL 订单应使用 600 秒退出 TTL 的失败测试。

- [ ] **步骤 1：写失败测试。**

```python
def test_live_place_sell_uses_exit_order_ttl(monkeypatch):
    broker = _live_stub()
    broker.order_ttl = 180
    broker.exit_order_ttl = 600
    now = 1_700_000_000
    monkeypatch.setattr("pmbot.brokers.time.time", lambda: now)
    broker._gtd_expiration = LiveBroker._gtd_expiration.__get__(broker, LiveBroker)
    broker.client.post_order.return_value = {"orderID": "exit-1"}

    broker._place_sell(Quote("yes1", 0.53, 10))

    args = broker.client.create_order.call_args.args[0]
    assert args.expiration == now + 600 + GTD_SECURITY_THRESHOLD_SECS
```

- [ ] **步骤 2：运行测试并确认红灯。**

运行：`rtk pytest tests/test_brokers.py -k "live_place_sell_uses_exit_order_ttl" -q`

预期：FAIL；当前 `_place_sell()` 调用无参 `_gtd_expiration()`，实际到期时间基于 180 秒普通报价 TTL，而不是 600 秒。

### 任务 2：最小实现与配置

**文件：**

- 修改：`pmbot/brokers.py:561-568,626-627,733-753`
- 修改：`config.yaml:275-320`
- 修改：`config.debug.yaml` 对应 `risk` 节

**接口：**

- 新增：`LiveBroker.exit_order_ttl: int`
- 修改：`LiveBroker._gtd_expiration(ttl_secs: int | None = None) -> int`
- 保持：`LiveBroker.set_exit(market, quote)` 的 cancel-before-post 次序。

- [ ] **步骤 1：在 `LiveBroker.__init__()` 加载退出 TTL。**

```python
self.order_ttl = int(cfg["quoting"].get("order_ttl_secs", 90))
self.exit_order_ttl = int(cfg["risk"].get("exit_order_ttl_secs", 600))
```

- [ ] **步骤 2：令 GTD 计算函数接受可选 TTL，默认仍为普通报价 TTL。**

```python
def _gtd_expiration(self, ttl_secs: int | None = None) -> int:
    ttl = self.order_ttl if ttl_secs is None else ttl_secs
    return int(time.time()) + ttl + GTD_SECURITY_THRESHOLD_SECS
```

- [ ] **步骤 3：仅令 `_place_sell()` 使用退出 TTL，并复用同一个 expiration 写入 `RestingOrder`。**

```python
expiration = self._gtd_expiration(self.exit_order_ttl)
signed = self.client.create_order(OrderArgs(
    price=q.price, size=q.size, side=Side.SELL, token_id=q.token_id,
    expiration=expiration,
))
# 成功时：RestingOrder(oid, q, time.time(), expiration)
```

- [ ] **步骤 4：在两个 YAML 的 `risk.passive_exit` 后添加显式配置。**

```yaml
  # Keep an unchanged passive exit in the queue longer than normal quotes;
  # GTD expiry still clears it after a crash. Never use an unbounded/GTC exit.
  exit_order_ttl_secs: 600
```

- [ ] **步骤 5：重跑红灯测试并确认转绿。**

运行：`rtk pytest tests/test_brokers.py -k "live_place_sell_uses_exit_order_ttl" -q`

预期：PASS；实际 expiration 等于当前时间加 600 秒和 GTD 安全余量。

### 任务 3：退出单生命周期回归测试

**文件：**

- 修改：`tests/test_brokers.py:509-521` 附近

**接口：**

- 消费：`LiveBroker.set_exit(market: Market, quote: Quote | None) -> None`
- 产出：已有订单保留与替换顺序的防回归保障。

> 以下是对既有 `set_exit()` 安全行为的回归测试，因此在改动前就可能通过；不得把“已通过”误报为新功能已实现。新功能的红绿证明在任务 1 和任务 2 中完成。

- [ ] **步骤 1：增加同 token、同价、同量且未临近到期时不撤不挂的测试。**

```python
def test_live_exit_keeps_unchanged_order_before_refresh(monkeypatch):
    broker = _live_stub()
    quote = Quote("yes1", 0.53, 10)
    now = 1_700_000_000
    monkeypatch.setattr("pmbot.brokers.time.time", lambda: now)
    broker._exit_orders["cid1"] = RestingOrder(
        "exit-old", quote, now - 60, now + GTD_REFRESH_MARGIN_SECS + 1,
    )
    broker._batch_cancel = MagicMock(return_value=True)
    broker._place_sell = MagicMock()

    broker.set_exit(_market(), quote)

    broker._batch_cancel.assert_not_called()
    broker._place_sell.assert_not_called()
```

- [ ] **步骤 2：增加同价同量但临近到期时先撤后挂的测试。**

```python
def test_live_exit_replaces_at_refresh_after_cancel(monkeypatch):
    broker = _live_stub()
    quote = Quote("yes1", 0.53, 10)
    now = 1_700_000_000
    monkeypatch.setattr("pmbot.brokers.time.time", lambda: now)
    broker._exit_orders["cid1"] = RestingOrder(
        "exit-old", quote, now - 590, now + GTD_REFRESH_MARGIN_SECS - 1,
    )
    calls = []
    broker._batch_cancel = lambda ids: calls.append(("cancel", ids)) or True
    broker._place_sell = lambda q: calls.append(("post", q)) or RestingOrder(
        "exit-new", q, now, now + 600,
    )

    broker.set_exit(_market(), quote)

    assert calls == [("cancel", ["exit-old"]), ("post", quote)]
```

- [ ] **步骤 3：增加数量变化时先撤后挂的测试。**

```python
def test_live_exit_quantity_change_cancels_before_replacement(monkeypatch):
    broker = _live_stub()
    now = 1_700_000_000
    monkeypatch.setattr("pmbot.brokers.time.time", lambda: now)
    old = Quote("yes1", 0.53, 10)
    new = Quote("yes1", 0.53, 5)
    broker._exit_orders["cid1"] = RestingOrder("exit-old", old, now - 1, now + 600)
    calls = []
    broker._batch_cancel = lambda ids: calls.append(("cancel", ids)) or True
    broker._place_sell = lambda q: calls.append(("post", q)) or RestingOrder(
        "exit-new", q, now, now + 600,
    )

    broker.set_exit(_market(), new)

    assert calls == [("cancel", ["exit-old"]), ("post", new)]
```

- [ ] **步骤 4：运行上述回归测试。**

运行：`rtk pytest tests/test_brokers.py -k "live_exit_keeps_unchanged_order_before_refresh or live_exit_replaces_at_refresh_after_cancel or live_exit_quantity_change_cancels_before_replacement" -q`

预期：PASS；普通同价订单保留，临近到期或数量变化时严格 cancel-before-post。

### 任务 4：完整验证与交付

**文件：**

- 检查：`pmbot/brokers.py`、`config.yaml`、`config.debug.yaml`、`tests/test_brokers.py`

- [ ] **步骤 1：运行 focused tests。**

运行：`rtk pytest tests/test_brokers.py -k "live_exit or live_place_sell" -q`

预期：PASS。

- [ ] **步骤 2：运行完整测试套件。**

运行：`rtk pytest tests/ -q`

预期：PASS；失败时先定位根因，禁止以放宽风险限制掩盖。

- [ ] **步骤 3：审查最终差异。**

运行：`rtk git diff --check` 与 `rtk git diff -- pmbot/brokers.py config.yaml config.debug.yaml tests/test_brokers.py`

预期：无 whitespace 错误；普通 BUY 路径和 forced-hedge 路径无语义变更；退出单仍 cancel-before-post。

- [ ] **步骤 4：报告验证结果，不启动 live 进程。**

报告修改文件、focused/full-suite 结果、预期撤挂间隔从 180 秒变为 600 秒的影响，以及未验证的交易所实盘行为。
