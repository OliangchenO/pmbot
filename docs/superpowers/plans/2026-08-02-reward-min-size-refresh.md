# 奖励最小挂单份数刷新实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**目标：** 已入选市场的奖励最小份数在一分钟内刷新，并在下一次报价周期替换尺寸不足的挂单。

**架构：** 仅对已入选市场复用 `gamma.fetch_market(condition_id)`；这避免重新进行全市场排名扫描，同时保持市场选择结果和盘口订阅不变，只更新内存中 `Market.min_size`。现有的目标报价计算和 broker 对账会据此替换尺寸与目标不同的订单。

**技术栈：** Python 3、asyncio、httpx、pytest。

## 全局约束

- 开发期间不启动或控制 live 进程、不修改操作者配置，也不向交易所发单。
- 仅对当前正常报价的已入选市场做轻量奖励条款查询。
- 查询失败时保留最后一次已知值，且不得中断报价。
- 刷新间隔为 60 秒，独立于 30 分钟的市场排名刷新。

---

### 任务 1：刷新已入选市场的奖励最小份数

**文件：**
- 修改：`pmbot/main.py:45-69, 630-644, 953-1090`
- 修改：`config.yaml:23-100`
- 修改：`config.debug.yaml:18-83`
- 测试：`tests/test_main.py`

**接口：**
- 消费：`gamma.fetch_market(condition_id: str) -> Market | None`。
- 产出：`Bot._refresh_reward_min_sizes() -> None`，只替换当前 `Bot.markets` 条目的 `Market.min_size`。
- 产出：`scanner.reward_min_size_refresh_seconds: float`，默认值为 `60`。

- [ ] **步骤 1：编写失败测试**

```python
def test_refresh_reward_min_size_updates_selected_market(tmp_path, monkeypatch):
    from pmbot import gamma as gamma_mod

    async def scenario():
        bot = _bot(tmp_path)
        market = _market()
        market.min_size = 20.0
        _setup(bot, tmp_path, market)
        refreshed = _market()
        refreshed.min_size = 50.0
        monkeypatch.setattr(gamma_mod, "fetch_market", lambda cid: refreshed)

        await bot._refresh_reward_min_sizes()

        assert bot.markets[0].min_size == 50.0
        bot.metrics.close()

    asyncio.run(scenario())
```

- [ ] **步骤 2：运行测试并确认失败**

执行：`.venv\\Scripts\\python.exe -m pytest tests/test_main.py::test_refresh_reward_min_size_updates_selected_market -q`

预期：失败，因为 `Bot._refresh_reward_min_sizes` 尚不存在。

- [ ] **步骤 3：实现最小代码**

```python
async def _refresh_reward_min_sizes(self) -> None:
    markets = list(self.markets)
    refreshed = await asyncio.gather(*(
        asyncio.to_thread(gamma.fetch_market, m.condition_id) for m in markets))
    for market, latest in zip(markets, refreshed):
        if latest is not None and latest.min_size != market.min_size:
            market.min_size = latest.min_size
```

在主循环中、`_quote_all()` 之前，最多每 `scanner.reward_min_size_refresh_seconds` 调用一次此方法。在两份随程序发布的 YAML 配置中都加入值为 `60` 的该配置项。

- [ ] **步骤 4：运行测试并确认通过**

执行：`.venv\\Scripts\\python.exe -m pytest tests/test_main.py::test_refresh_reward_min_size_updates_selected_market -q`

预期：通过。

### 任务 2：证明奖励门槛提高会替换旧挂单尺寸

**文件：**
- 测试：`tests/test_main.py`

**接口：**
- 消费：`Bot._refresh_reward_min_sizes() -> None`、`Bot._quote_all() -> None` 与 `PaperBroker.open_quotes(market) -> list[Quote]`。
- 产出：回归证据，证明已入选市场的奖励最低份数更新后，`20.0` 份挂单会被替换为 `50.0` 份挂单。

- [ ] **步骤 1：编写失败测试**

```python
def test_reward_min_size_increase_replaces_resting_quote(tmp_path, monkeypatch):
    from pmbot import gamma as gamma_mod

    async def scenario():
        bot = _bot(tmp_path)
        market = _market()
        market.min_size = 20.0
        broker = _setup(bot, tmp_path, market)
        bot.tracker.books[market.yes_token].bids = {0.49: 100.0}
        bot.tracker.books[market.yes_token].asks = {0.51: 100.0}
        bot.tracker.books[market.no_token].bids = {0.49: 100.0}
        bot.tracker.books[market.no_token].asks = {0.51: 100.0}
        await bot._quote_all()
        assert {q.size for q in broker.open_quotes(market)} == {20.0}

        refreshed = _market()
        refreshed.min_size = 50.0
        monkeypatch.setattr(gamma_mod, "fetch_market", lambda cid: refreshed)
        await bot._refresh_reward_min_sizes()
        await bot._quote_all()

        assert {q.size for q in broker.open_quotes(market)} == {50.0}
        bot.metrics.close()

    asyncio.run(scenario())
```

- [ ] **步骤 2：运行测试并确认失败**

执行：`.venv\\Scripts\\python.exe -m pytest tests/test_main.py::test_reward_min_size_increase_replaces_resting_quote -q`

预期：在任务 1 的实现前失败，因为刷新方法尚不存在。

- [ ] **步骤 3：运行聚焦回归测试集**

执行：`.venv\\Scripts\\python.exe -m pytest tests/test_main.py -q`

预期：通过，包括两项刷新测试和既有主循环覆盖。

- [ ] **步骤 4：运行完整测试集**

执行：`.venv\\Scripts\\python.exe -m pytest -q`

预期：通过；若有既有失败，单独报告确切测试名称。
