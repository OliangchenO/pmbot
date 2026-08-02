# pmbot — 速度与延迟优化

> # pmbot — Speed &amp; Latency Improvements

专家评估：哪些环节已足够快、哪些还不够、以及应当改什么——按**每工程小时的预期 PnL 回报**排序，场景设定为小资金（$500，5 个市场）的奖励 farming 操作。

> Expert assessment of where this bot is fast enough, where it is not, and
> what to change — ordered by **expected PnL impact per engineering hour** for
> a reward-farming operation at small capital ($500, 5 markets).

这**不是**一份 HFT 重写指南。Polymarket 流动性奖励依据的是挂单在带内的时间占比和距中间价的距离，而非亚毫秒级的反应速度。本文讨论的速度优化瞄准三个可衡量的成本：

> This is **not** an HFT rewrite guide. Polymarket liquidity rewards score resting
> orders on in-band uptime and distance from mid, not on sub-millisecond reaction
> time. Speed improvements here target three measurable costs:

> pick-off during requotes + time dark after guard trips + hedge delay on toxic inventory

重新报价期间的被吃风险 + 风控触发后的空窗时间 + 有毒库存的对冲延迟

> pick-off during requotes + time dark after guard trips + hedge delay on toxic inventory

其余各项都是次要的，除非实盘数据暴露出具体的瓶颈。

> Everything else is secondary unless live data shows a specific bottleneck.

---

> ---

## 当前延迟画像

> ## Current latency profile

| 层级 | 当前行为 | 典型延迟 |
| --- | --- | --- |
| 订单簿数据 | CLOB 市场 WebSocket（`books.py`） | ~10–50 ms |
| 成交检测 | 用户 WebSocket（`userfeed.py`，实盘） | ~10–50 ms |
| 报价决策循环 | `main.py::run` 中的固定 tick | **2.0 s** |
| 重新报价触发 | 价格漂移须 ≥ `requote_move_cents`（0.4¢） | 有意为之 |
| 订单提交 | 工作线程中通过 `py_clob_client` 走 REST | **100–300 ms** 每笔 |
| 订单撤销 | REST `cancel_orders` / `cancel` 回退 | **100–300 ms** |
| 旧报价存活窗口 | 旧单在撤单 ACK 返回前仍挂在簿上（GTD 刷新余量约 90s） | **100–300 ms** 每次替换 |
| 仓位对账 | REST 每 12 s 轮询 + WS 增量叠加 | 0–12 s |
| 强制对冲重试 | `main.py` 中 `FLATTEN_RETRY_SECONDS` | **15 s** |
| 风控检查 | 仅在 2 s 报价循环中评估 | **0–2 s**（触发条件后） |

> | Layer | Current behavior | Typical delay |
> | --- | --- | --- |
> | Book data | CLOB market WebSocket (`books.py`) | ~10–50 ms |
> | Fill detection | User WebSocket (`userfeed.py`, live) | ~10–50 ms |
> | Quote decision loop | Fixed tick in `main.py::run` | **2.0 s** |
> | Requote trigger | Price must drift ≥ `requote_move_cents` (0.4¢) | intentional |
> | Order placement | REST via `py_clob_client` in worker thread | **100–300 ms** per op |
> | Order cancellation | REST `cancel_orders` / `cancel` fallback | **100–300 ms** |
> | Stale quote window | Old quote live until cancel ACK (GTD refresh at ~90s margin) | **100–300 ms** per replace |
> | Position reconcile | REST poll every 12 s + WS delta overlay | 0–12 s |
> | Forced hedge retry | `FLATTEN_RETRY_SECONDS` in `main.py` | **15 s** |
> | Guard pull | Only evaluated on the 2 s quote loop | **0–2 s** after trip condition |

**结论：** 速度足以参与中等层级的奖励池竞争，但不足以统治顶级池或在最紧报价份额上胜出。剩余的最大速度成本来自**重新报价期间被吃**，而非循环频率。

> **Verdict:** Fast enough to participate in mid-tier reward pools. Not fast
> enough to dominate top pools or compete on tightest-quote share. The largest
> remaining speed cost is **requote pick-off**, not loop frequency.

---

> ---

## 优先级 1 — 高 ROI（优先做）

> ## Priority 1 — High ROI (do these first)

这些改动直接减少逆向选择或提升带内在线时间。每一项都兼容当前的 Python/asyncio 架构。

> These changes directly reduce adverse selection or increase in-band uptime.
> Each is compatible with the current Python/asyncio architecture.

### 1.1 事件驱动撤单（不等 2 s 循环）— ✅ 已实现

> ### 1.1 Event-driven quote pulls (don't wait for the 2 s loop) — ✅ IMPLEMENTED

**涉及：** `main.py::_quote_all`、`risk.py::MarketGuards`、`books.py::_handle`

> **Where:** `main.py::_quote_all`, `risk.py::MarketGuards`, `books.py::_handle`

**问题：** 风控守卫（`record_mid`、`record_trade`、`check_flow`）可能在循环中途触发，但报价只在下一次 `_quote_all` 轮次才被撤回——最长延迟可达 **2 s**。当有新闻驱动的行情波动或交易量爆发时，bot 在受威胁的一侧仍挂着单，而订单簿已将其击穿。

> **Problem:** Guards (`record_mid`, `record_trade`, `check_flow`) can trip
> mid-loop, but quotes are only pulled on the next `_quote_all` pass — up to
> **2 s** later. During a news move or velocity burst the bot stays quoted on
> the endangered side while the book runs through it.

**为什么重要：** 风控触发的存在就是为了减少被吃；2 s 的反应延迟让它大打折扣。这是**不需要**更快订单 API 的前提下，回报最高的单项速度修复。

> **Why it matters:** Guard trips exist to reduce pick-off; a 2 s reaction delay
> partially defeats them. This is the single highest-ROI speed fix that does
> **not** require faster order APIs.

**方案：** 当风控触发时（`MarketGuards._trip`、`trip_market`、`check_flow` 的撤单路径、`_quote_all` 中的 markout 触发），立即将对应市场的撤单操作排入异步队列——不要等下一次循环 tick。

> **Fix:** When a guard trips (`MarketGuards._trip`, `trip_market`,
> `check_flow` pull path, markout trip in `_quote_all`), enqueue an immediate
> async cancel for that market's quotes — do not wait for the next loop tick.

```python
# 示意：风控触发时回调 Bot
async def _pull_market_quotes(self, cid: str) -> None:
    m = self._markets_by_cid.get(cid)
    if m and self.broker.open_quotes(m):
        self.metrics.sample_uptime(cid, False)
        await self._broker_call(self.broker.set_quotes, m, [])
```

> ```python
> # Sketch: guards call back into Bot on trip
> async def _pull_market_quotes(self, cid: str) -> None:
>     m = self._markets_by_cid.get(cid)
>     if m and self.broker.open_quotes(m):
>         self.metrics.sample_uptime(cid, False)
>         await self._broker_call(self.broker.set_quotes, m, [])
> ```

给 `MarketGuards` 加一个可选的 `on_trip: Callable[[str], Awaitable]`，在 bot 初始化时注入。保留 2 s 循环用于正常的重新报价；事件通道仅用于撤单。

> Wire `MarketGuards` with an optional `on_trip: Callable[[str], Awaitable]` set
> at bot init. Keep the 2 s loop for normal requoting; use events only for pulls.

**预期效果：** 将最坏情况下的有毒暴露窗口从 2 s 降至 ~300 ms（撤单往返时间）。在你做市的较平静市场上价值很高；如果你将来放宽 `exclude_keywords`，这一点将至关重要。

> **Expected impact:** Cuts worst-case toxic exposure window from 2 s → ~300 ms
> (cancel round trip). High value on the calmer markets you quote; essential if
> you ever loosen `exclude_keywords`.

---

> ---

### 1.2 并行化每个循环内的逐市场订单操作 — ✅ 已实现

> ### 1.2 Parallelize per-market order ops within a loop — ✅ IMPLEMENTED

**涉及：** `main.py::_quote_all`、`_manage_inventory`；`brokers.py::set_quotes`

> **Where:** `main.py::_quote_all`, `_manage_inventory`; `brokers.py::set_quotes`

**问题：** `_quote_all` 按顺序处理 5 个市场。每个需要重新报价的市场执行“撤单 + 批量提交”（约 200–600 ms）。一个完整的重新报价周期可能需要 **1–3 s** 的墙上时间，在此期间迭代顺序靠后的市场用的是过时的订单簿数据。

> **Problem:** `_quote_all` processes 5 markets sequentially. Each market that
> needs a requote runs cancel + batch post (~200–600 ms). A full requote cycle
> can take **1–3 s** wall time, during which later markets in the iteration
> order are quoted on stale books.

**为什么重要：** 延迟变得*与活跃度相关*——你重新报价是因为行情动了，而循环中最后处理的市场是最“瞎”的，持续时间最长。

> **Why it matters:** Latency becomes *correlated with activity* — you requote
> because things moved, and markets processed last in the loop are blind longest.

**方案：** 算出所有目标报价后，并发派发订单操作：

> **Fix:** After computing all desired quotes, dispatch order ops concurrently:

```python
tasks = []
for m, final in markets_to_update:
    tasks.append(self._broker_call(self.broker.set_quotes, m, final))
await asyncio.gather(*tasks)
```

> ```python
> tasks = []
> for m, final in markets_to_update:
>     tasks.append(self._broker_call(self.broker.set_quotes, m, final))
> await asyncio.gather(*tasks)
> ```

对 `_manage_inventory` 的退出/对冲调用也应用相同模式，只要安全（不同市场间的对冲彼此独立；同一市场两次对冲不独立——如有需要按 `condition_id` 分组）。

> Apply the same pattern to `_manage_inventory` exit/hedge calls where safe
> (hedges across different markets are independent; two hedges on the same market
> are not — group by `condition_id` if needed).

**预期效果：** 全循环重新报价时间从 O(n × 延迟） 降至 O（延迟）。以 5 个市场计，每个重度重新报价周期节省约 1–2 s。

> **Expected impact:** Full-loop requote time drops from O(n × latency) to
> O(latency). At 5 markets, ~1–2 s saved per heavy requote cycle.

---

> ---

### 1.3 每次替换都先撤后挂（最小化被吃窗口）

> ### 1.3 Cancel-before-place on every replace (minimize pick-off window)

**涉及：** `brokers.py::LiveBroker.set_quotes`

> **Where:** `brokers.py::LiveBroker.set_quotes`

**问题：** 当前流程：评估保留/撤单列表 → 批量撤单 → 批量挂单。逻辑正确，但当 `to_cancel` 和 `desired` 在同一 token 上有重叠时，旧价格仍有一段存活窗口。行情快速变动时，旧买单会在撤单 ACK 返回后、新单落地前被吃掉。

> **Problem:** Current flow: evaluate keep/cancel list → batch cancel → batch
> place. Correct, but when `to_cancel` and `desired` overlap on the same token,
> there is still a window where the old price is live. On a fast move, the stale
> bid gets hit between cancel ACK and new order landing.

**为什么重要：** 对于一个大约每 2 s 在中间价漂移 0.4¢ 时重新报价的奖励 farmer 而言，这是最主要的实盘逆向选择来源。

> **Why it matters:** This is the dominant live adverse-selection source for a
> reward farmer that requotes ~every 2 s when mid drifts 0.4¢.

**方案（渐进式）：**

> **Fix (incremental):**

1. **优先加宽而非替换：** 如果中间价朝不利于你的方向移动，先尝试只撤受威胁一侧的单；如果敞口已经偏高，跳过在下一循环挂新单。
2. **立即发出撤单**——当 `reconcile_quotes` 判定某一侧必须变动时，不要等为市场中所有 token 攒齐批量再发。
3. 跟踪每个订单的 `cancel_sent_at`；在撤单确认或超时（200 ms）前抑制新单提交，之后再挂。

> 1. **Prefer widen-over-replace:** if mid moved against you, first try
>    cancelling the endangered side only; skip placing the new side until next
>    loop if exposure is already elevated.
> 2. **Fire cancel immediately** when `reconcile_quotes` decides a side must
>    change — don't wait to build the full batch for all tokens in the market.
> 3. Track `cancel_sent_at` per order; suppress new placement until cancel
>    confirmed or timeout (200 ms), then place.

**方案（结构性）：** 调研新版 `py_clob_client` 是否支持 CLOB **modify/replace** 端点——原子替换在改善价格时保留排队位置，并缩短空窗。

> **Fix (structural):** Investigate CLOB **modify/replace** endpoints if available
> in newer `py_clob_client` versions — atomic replace preserves queue on price
> improvements and shrinks the dead zone.

**预期效果：** 直接减少重新报价期间的被吃成交。无实盘 markout 数据难以量化；可能在逆向选择中占比 20–40%。

> **Expected impact:** Direct reduction in pick-off fills during requotes.
> Hard to quantify without live markout data; likely 20–40% of adverse selection.

---

> ---

### 1.4 将报价循环与库存循环解耦

> ### 1.4 Decouple quote loop from inventory loop timing

**涉及：** `main.py::run`

> **Where:** `main.py::run`

**问题：** `_quote_all` 和 `_manage_inventory` 每 2 s 顺序执行。库存管理（被动退出、强制对冲）要等报价完成，反之亦然。一次慢的重新报价周期会将对冲延迟数秒。

> **Problem:** `_quote_all` and `_manage_inventory` run sequentially every 2 s.
> Inventory management (passive exits, forced hedges) waits for quoting to finish,
> and vice versa. A slow requote cycle delays hedges by seconds.

**为什么重要：** 当库存有毒时，对冲延迟比报价新鲜度的代价更高。

> **Why it matters:** When inventory is toxic, hedge latency is more costly than
> quote freshness.

**方案：** 以不同间隔运行两个异步任务：

> **Fix:** Run two async tasks on different intervals:

| 任务 | 间隔 | 目的 |
| --- | --- | --- |
| `_quote_all` | 2 s（或在 1.5 之后改为 1 s） | 挂单报价 |
| `_manage_inventory` | 0.5–1 s | 退出与强制对冲 |

> | Task | Interval | Purpose |
> | --- | --- | --- |
> | `_quote_all` | 2 s (or 1 s after 1.5) | quote placement |
> | `_manage_inventory` | 0.5–1 s | exits and forced hedges |

两者都读取共享的 broker/tracker 状态；订单操作已经通过 `_broker_call` / 工作线程。如果同一市场的报价和对冲操作不可重叠，使用按 `condition_id` 区分的 `asyncio.Lock`。

> Both read shared broker/tracker state; order ops already go through
> `_broker_call` / worker threads. Use an `asyncio.Lock` per `condition_id` if
> quote and hedge ops on the same market must not overlap.

**预期效果：** 对冲反应时间从（2 s + 重新报价耗时）降至 ≤ 1 s。减少快速变动的互补 token 上的强制对冲滑点。

> **Expected impact:** Hedge reaction time drops from (2 s + requote duration) to
> ≤ 1 s. Reduces forced-hedge slippage on fast-moving complements.

---

> ---

## 优先级 2 — 中等 ROI（实盘校准后再做）

> ## Priority 2 — Medium ROI (after live calibration)

### 2.1 主循环间隔可配置为 1 s

> ### 2.1 Reduce main loop interval to 1 s (configurable)

**涉及：** `main.py` — `LOOP_SECONDS = 2.0`

> **Where:** `main.py` — `LOOP_SECONDS = 2.0`

**问题：** 2 s 偏保守。奖励采样以分钟为单位；你不需要亚秒级重新报价。但 2 s 意味着中间价漂移使你脱离奖励带却未触发风控后，最长有 2 s 不在带内。

> **Problem:** 2 s is conservative. Reward sampling is per-minute; you do not
> need sub-second requotes. But 2 s means up to 2 s out of band after a mid
> drift that pushes you outside the reward band without tripping a guard.

**为什么重要：** 奖励得分与距中间价的距离呈二次关系。每个周期在带外漂移 0.5¢ 持续 2 s，累积一天下来不可忽视——尤其当竞争对手的报价比你更紧时。

> **Why it matters:** Reward score is quadratic in distance from mid. Drifting
> 0.5¢ outside band for 2 s every cycle adds up over a day — especially if
> competitors stay tighter.

**方案：** 在 `config.yaml` 中增加 `loop_seconds: 1.0`。保持 `requote_move_cents` 在 0.4¢，以保留排队优先级——更快的循环不意味着更多的替换，而是更快检测到*需要*的替换和风控反应。

> **Fix:** Add `loop_seconds: 1.0` to `config.yaml`. Keep `requote_move_cents`
> at 0.4¢ so queue priority is preserved — faster loop does not mean more
> replaces, it means faster detection of *needed* replaces and guard reactions.

**注意：** 将循环减半，如果大多数市场每个 tick 都重新报价，REST 调用量会翻倍。结合对账逻辑，使未变化的报价成为空操作（已通过 `reconcile_quotes` + `set_quotes` 中的保留逻辑实现）。

> **Caution:** Halving the loop doubles REST call volume if most markets requote
> every tick. Combine with reconcile logic so unchanged quotes are no-ops (already
> done via `reconcile_quotes` + keep logic in `set_quotes`).

---

> ---

### 2.2 CLOB 客户端持久 HTTP 连接池

> ### 2.2 Persistent HTTP connection pool for CLOB client

**涉及：** `brokers.py::LiveBroker` — `self.client` 内部使用 `httpx`

> **Where:** `brokers.py::LiveBroker` — `self.client` uses `httpx` internally

**问题：** 每次订单操作可能新建 TCP+TLS 会话，具体取决于客户端配置。在每笔 100–300 ms 的操作中，连接建立的耗时占比不小。

> **Problem:** Each order op may establish a new TCP+TLS session depending on
> client configuration. At 100–300 ms per op, connection setup is a meaningful
> fraction.

**方案：**

> **Fix:**

1. 确认 `py_clob_client` 是否复用连接（检查其 `httpx.Client` 生命周期）。
2. 如果不复用，打补丁或封装以持有单个长生命周期客户端实例。
3. 在地理上靠近 Polymarket 基础设施的低延迟 VPS（面向美国的 API 通常为 US East）上运行。

> 1. Verify `py_clob_client` reuses connections (check its `httpx.Client` lifecycle).
> 2. If not, patch or wrap to hold a single long-lived client instance.
> 3. Run from a low-latency VPS geographically close to Polymarket infra (US East
>    typical for US-facing APIs).

**预期效果：** 每笔订单操作节省 20–80 ms。单独来看不算大，但与 1.2 和 1.3 结合后效果累积。

> **Expected impact:** 20–80 ms shaved per order op. Modest alone; compounds with
> 1.2 and 1.3.

---

> ---

### 2.3 所有市场合并为一次 `post_orders` 调用

> ### 2.3 Batch all markets into one `post_orders` call per loop

**涉及：** `brokers.py::LiveBroker.set_quotes`

> **Where:** `brokers.py::LiveBroker.set_quotes`

**问题：** 每个市场独立调用 `post_orders`。五个市场 → 即使并行也是五次往返。

> **Problem:** Each market calls `post_orders` independently. Five markets → five
> round trips even when parallelized.

**方案：** 增加 `LiveBroker.batch_set_quotes(markets: list[tuple[Market, list[Quote]]])`，将跨市场的所有新订单收集到一次 `post_orders` 有效负载中。撤销同样可以用一次 `cancel_orders` 带所有 ID。

> **Fix:** Add `LiveBroker.batch_set_quotes(markets: list[tuple[Market, list[Quote]]])`
> that collects all new orders across markets into one `post_orders` payload.
> Cancels can similarly use one `cancel_orders` with all IDs.

**预期效果：** 挂单阶段：5 × 200 ms → 1 × 200 ms（仍需先顺序撤单）。最好与 1.2 结合。

> **Expected impact:** Order placement phase: 5 × 200 ms → 1 × 200 ms (sequential
> cancel still needed first). Best combined with 1.2.

---

> ---

### 2.4 高活跃期缩短仓位轮询间隔

> ### 2.4 Shorten position poll interval during high activity

**涉及：** `main.py` — `POSITION_REFRESH_SECONDS = 12.0`

> **Where:** `main.py` — `POSITION_REFRESH_SECONDS = 12.0`

**问题：** 实盘模式下 WS 成交推送是主要数据源，但当用户 feed 断开时，要等 12 s 才恢复到基于轮询的对账。在此期间库存偏斜与对冲决策可能是错误的。

> **Problem:** WS fills are primary in live mode, but when the user feed drops,
> 12 s until poll-based reconciliation resumes. Inventory skew and hedge
> decisions can be wrong for that window.

**方案：** 自适应轮询间隔：

> **Fix:** Adaptive poll interval:

- **12 s**：当 `ws_fills_active` 且库存平坦时
- **3 s**：当用户 feed 断开 **或** `total_inventory_usd` > 资金上限的 50% 时
- **12 s**：其余情况

> - **12 s** when `ws_fills_active` and inventory is flat
> - **3 s** when user feed is down OR `total_inventory_usd` > 50% of cap
> - **12 s** otherwise

**预期效果：** 减少 feed 中断期间的敞口波动和重复对冲风险。不算正常路径上的速度收益。

> **Expected impact:** Reduces exposure flapping and double-hedge risk during
> feed outages. Not a speed win in the happy path.

---

> ---

### 2.5 在关键路径之外预先签名订单

> ### 2.5 Pre-sign orders off the critical path

**涉及：** `brokers.py::_place_buy`、`set_quotes`

> **Where:** `brokers.py::_place_buy`, `set_quotes`

**问题：** `create_order`（EIP-712 签名）在工作线程的 `set_quotes` 内同步执行。签名每个订单约增加 10–50 ms。

> **Problem:** `create_order` (EIP-712 signing) runs synchronously inside
> `set_quotes` on the worker thread. Signing adds ~10–50 ms per order.

**方案：** 在 `_quote_all` 中算出目标报价后，立即将签名提交到线程池。到撤单 ACK 返回时，已签名的订单已准备好提交。

> **Fix:** After computing desired quotes in `_quote_all`, submit signing to a
> thread pool immediately. By the time cancel ACK returns, signed orders are
> ready to post.

**预期效果：** 每个订单的收益不大（~10–50 ms），但在 1.2/1.3 完成后是零额外成本的改进。

> **Expected impact:** Small (~10–50 ms per order) but free once 1.2/1.3 are done.

---

> ---

## 优先级 3 — 较低 ROI / 较高工作量

> ## Priority 3 — Lower ROI / higher effort

以下各项对于竞争顶级池或更大资金规模是重要的，但在 $500 中等层级的奖励市场上不太可能改变盈利状况。

> These matter for competing on top pools or at larger capital. Unlikely to
> change profitability at $500 on mid-tier reward markets.

### 3.1 WebSocket 下单（当/如 CLOB 支持时）

> ### 3.1 WebSocket order entry (if/when CLOB supports it)

**问题：** REST 往返是硬地板（~100 ms）。WebSocket 下单可将其降至 ~20–50 ms。

> **Problem:** REST round trips are the hard floor (~100 ms). WebSocket order
> entry could cut this to ~20–50 ms.

**状态：** 查阅当前 Polymarket CLOB 文档 / `py_clob_client` 变更日志。截至本次代码库审计，客户端表层尚未提供此能力。

> **Status:** Check current Polymarket CLOB docs / `py_clob_client` changelog.
> Not available in the client surface as of the current codebase audit.

---

> ---

### 3.2 托管于就近机房 / 专用低延迟主机

> ### 3.2 Colocation / dedicated low-latency host

**问题：** 从家庭网络或远离目标区域的 VPS 运行时，每次 REST 调用增加 50–200 ms RTT。

> **Problem:** Running from a home connection or far-region VPS adds 50–200 ms
> RTT to every REST call.

**方案：** 部署到 US-East VPS（如 AWS us-east-1、Vultr NJ）。前后分别测量到 `clob.polymarket.com` 的 RTT。

> **Fix:** Deploy to US-East VPS (e.g. AWS us-east-1, Vultr NJ). Measure RTT to
> `clob.polymarket.com` before and after.

**预期效果：** 每笔订单操作节省 50–150 ms。上实盘之前值得做；在那之前不值得进一步优化。

> **Expected impact:** 50–150 ms per order op. Worth doing before going live;
> not worth optimizing further until then.

---

> ---

### 3.3 用 Rust / Go 重写热路径

> ### 3.3 Rewrite hot path in Rust / Go

**问题：** Python + asyncio + 线程池对于 1–5 s 循环没有问题，但对于亚 100 ms 的反应时间没有竞争力。

> **Problem:** Python + asyncio + thread pool is fine for 1–5 s loops. It is not
> competitive at sub-100 ms reaction times.

**何时考虑：** 资金 > $5k，报价 top-10 奖励池，或实盘数据显示你因反应更快、报价更紧的对手而损失了 >30% 的奖励份额。

> **When to consider:** Capital > $5k, quoting top-10 reward pools, or live data
> shows you lose >30% of reward share to tighter quoters who react faster.

**何时跳过：** 当前策略（0.35 offset，0.4¢ 重新报价容忍度，$500，小众池）。速度不是你的约束瓶颈。

> **When to skip:** Current strategy (0.35 offset, 0.4¢ requote tolerance,
> $500, niche pools). Speed is not your binding constraint.

---

> ---

### 3.4 基于订单簿的亚秒级重新报价

> ### 3.4 Sub-second book-driven requoting

**问题：** 报价仅在主循环中更新，而非每次订单簿变化就更新。

> **Problem:** Quotes only update on the main loop, not on every book change.

**方案：** 注册一个订单簿监听器，当中间价变动 ≥ `requote_move_cents` 时重新计算报价并触发 `_broker_call(set_quotes)`。

> **Fix:** Register a book listener that recomputes quotes when mid moves ≥
> `requote_move_cents` and triggers `_broker_call(set_quotes)`.

**注意：** 会大幅增加订单换手率，**破坏排队优先级**，除非结合严格的对账容忍度。对于奖励 farming，事件驱动的**撤单**（1.1）比事件驱动的**重新挂单**更安全。

> **Caution:** Will massively increase order churn and **destroy queue priority**
> unless combined with strict reconcile tolerance. For reward farming, event-driven
> **pulls** (1.1) are safer than event-driven **replaces**.

---

> ---

## 不应该优化的

> ## What NOT to optimize

| 诱惑 | 为什么跳过 |
| --- | --- |
| 亚秒级主循环 | 增加换手率；排队优先级比新鲜度更重要 |
| 更紧的 `requote_move_cents` | 更多的替换 → 更多被吃 → 更差的 markout |
| 更紧的 `offset_frac_of_max_spread` | 更高的奖励得分但更多有毒成交；根据实盘 markout 调参 |
| 更快的扫描器刷新 | 重扫描已经做差量比较；30 分钟足够了 |
| 到处用线程替代 asyncio | 订单操作已卸载到线程；订单簿 WS 是异步的——架构没问题 |

> | Temptation | Why to skip it |
> | --- | --- |
> | Sub-second main loop | Increases churn; queue priority matters more than freshness |
> | Tighter `requote_move_cents` | More replaces → more pick-off → worse markouts |
> | Tighter `offset_frac_of_max_spread` | Higher reward score but more toxic fills; tune on live markouts |
> | Faster scanner refresh | Rescan already diffs markets; 30 min is fine |
> | Replace asyncio with threads everywhere | Order ops already offloaded; book WS is async — architecture is sound |

---

> ---

## 度量：证明速度改进有效

> ## Measurement: prove speed changes help

每次改动前后，从 `metrics.db` 跟踪以下指标：

> Before and after each change, track these from `metrics.db`:

| 指标 | 查询 / 来源 | 目标 |
| --- | --- | --- |
| 被吃率 | 30s markout < −1¢ 的成交 / 总成交 | 下降 |
| 带内在线时间 | 日报中的 `uptime_pct` | 提升或持平 |
| 风控触发后空窗时间 | 新增：记录 `guard_trip` → 下一次 `record_quotes` 的时间差 | < 500 ms |
| 重新报价被吃成交 | `_dying` 报价上的成交（paper）/ 撤单途中的成交（实盘） | 下降 |
| 对冲滑点 | 均价对冲价格 − 对冲时刻互补方的中间价 | 下降 |
| 奖励份额 | 已实现 / 预估奖励 | 提升 |

> | Metric | Query / source | Target |
> | --- | --- | --- |
> | Pick-off rate | Fills where markout @ 30s < −1¢ / total fills | Decrease |
> | In-band uptime | `uptime_pct` in daily report | Increase or hold |
> | Time dark after guard trip | New: log `guard_trip` → next `record_quotes` delta | < 500 ms |
> | Requote pick-off fills | Fills on `_dying` quotes (paper) / cancels-in-flight (live) | Decrease |
> | Hedge slippage | avg hedge price − complement mid at hedge time | Decrease |
> | Reward share | realized / estimated rewards | Increase |

如果实现了 1.1 或 1.3，在 `metrics.py` 中增加一个 `events` 表，记录风控触发和撤单/挂单时间戳——否则你无法判断速度工作是否起效。

> Add a `events` table to `metrics.py` for guard trips and cancel/post timestamps
> if you implement 1.1 or 1.3 — otherwise you cannot tell whether speed work helped.

---

> ---

## 实盘上线流程（在下注盈利之前）

> ## Go-live workflow (before betting on profitability)

速度工作不能替代校准。在扩大资金规模或追求优先级 2/3 优化之前，按以下顺序进行。

> Speed work does not substitute for calibration. Follow this sequence before
> scaling capital or chasing Priority 2/3 optimizations.

### 阶段 1 — Paper（1–2 周）

> ### Phase 1 — Paper (1–2 weeks)

使用 `config.yaml` 中的真实模拟设置（`paper.order_latency_ms`、`paper.reward_haircut`）运行：

> Run with the realism settings in `config.yaml` (`paper.order_latency_ms`,
> `paper.reward_haircut`):

```bash
python -m pmbot.main run
python -m pmbot.main report
```

> ```bash
> python -m pmbot.main run
> python -m pmbot.main report
> ```

**门槛：** 如果 Paper PnL 在 1–2 周后没有明确为正，不要上实盘。Paper 是天花板——实盘只会更差。Paper 持平或亏损意味着策略或市场选择有问题，而不是你需要更多速度。

> **Gate:** If paper PnL is not clearly positive after 1–2 weeks, do not go live.
> Paper is a ceiling — live will be worse. A flat or negative paper run means
> the strategy or market selection is wrong, not that you need more speed.

### 阶段 2 — 小规模实盘（$100–200，至少 2 周）

> ### Phase 2 — Live small (2+ weeks at $100–200)

不要以 `capital_usd: 500` 起步。将资金设置为 **$100–200**，保持 `top_n_markets: 5`，纯粹以收集数据为目的运行实盘：

> Do not start at `capital_usd: 500`. Set capital to **$100–200**, keep
> `top_n_markets: 5`, and run live purely to collect data:

```bash
python -m pmbot.main run
python -m pmbot.main report   # 每日：已实现 vs 预估奖励、在线时间、markout
```

> ```bash
> python -m pmbot.main run
> python -m pmbot.main report   # daily: realized vs est. rewards, uptime, markouts
> ```

逐市场跟踪（来自 `metrics.db` + 日志）：

> Track per market (from `metrics.db` + logs):

| 信号 | 来源 | 采取措施的阈值 |
| --- | --- | --- |
| 已实现奖励份额 | `realized_rewards_usd / est_rewards_usd` | 若 **< 30%** 估值则弃掉该市场 |
| 成交毒性 | `MarkoutTracker.market_avg` / 会话统计 | 若平均 markout **< −1¢** 则弃掉 |
| 带内在线时间 | 报告中的 `uptime_pct` | 若持续 **< 50%** 则弃掉 |
| 净 PnL | 权益 PnL + 价差捕获 − 对冲成本 | 若超过 1 周为负则弃掉 |

> | Signal | Source | Action threshold |
> | --- | --- | --- |
> | Realized reward share | `realized_rewards_usd / est_rewards_usd` | Drop market if **< 30%** of estimate |
> | Fill toxicity | `MarkoutTracker.market_avg` / session stats | Drop market if avg markout **< −1¢** |
> | In-band uptime | `uptime_pct` in report | Drop market if consistently **< 50%** |
> | Net PnL | equity PnL + spread capture − hedge costs | Drop market if negative over 1 week |

### 阶段 3 — 扩大规模（仅限已验证的市场）

> ### Phase 3 — Scale (only proven markets)

实盘 2+ 周后：

> After 2+ weeks live:

1. **弃掉**任何未达到上述阈值的市场。
2. **保留**净 PnL 为正、markout 可接受、已实现奖励份额 ≥ 估值 30% 的 2–3 个市场。
3. 仅在这些市场上将 `capital_usd` **逐步扩大**至 $500 左右（只有更多市场通过了同样的门槛才提高 `top_n_markets`）。

> 1. **Drop** any market failing the thresholds above.
> 2. **Keep** the 2–3 markets with positive net PnL, acceptable markouts, and
>    realized reward share ≥ 30% of estimate.
> 3. **Scale** `capital_usd` toward $500 only on those markets (raise
>    `top_n_markets` only if more markets pass the same gates).

不要因为 Paper 表现好或速度优化已上线就扩大规模。扩大规模是因为**特定市场的实盘数据证明了经济可行性**。

> Do not scale because paper looked good or because speed improvements shipped.
> Scale because live data on specific markets proved the economics.

---

> ---

## 策略调参（不重写代码）

> ## Strategy tuning (without a full rewrite)

速度不是唯一的杠杆。对于一个 $500 奖励 farmer，以下变更的 ROI 等同于或高于优先级 2/3：

> Speed is not the only lever. These changes have equal or higher ROI than
> Priority 2/3 for a $500 reward farmer:

| 变更 | 位置 | 理由 |
| --- | --- | --- |
| **事件驱动撤单** | 上文 §1.1 | 风控触发时不等 2 s 循环 |
| **根据实盘 markout 收紧市场选择** | `gamma.scan` + 事后过滤 | 停止在实盘 markout 持续为负的市场做市，即使有自适应 offset；通过重扫描或手动禁止名单轮换出去 |
| **更宽的默认 offset（0.40–0.45）** | `config.yaml` → `offset_frac_of_max_spread` | 在 3¢ 带宽上以 0.35 报价，毛 pair 捕获（~2¢）几乎被一次 −1.5¢ 的 markout 触发完全吃掉。从更宽起步，直到 markout 证明市场温和；让 `adaptive_offset` 逐市场收紧 |
| **略微提高 `requote_move_cents`（0.5–0.6¢）** | `config.yaml` | 减少替换次数 → 减少撤单/挂单期间的被吃；以奖励得分为代价换取排队稳定性 |

> | Change | Where | Rationale |
> | --- | --- | --- |
> | **Event-driven quote pulls** | §1.1 above | Don't wait for the 2 s loop when guards trip |
> | **Tighter market selection from live markouts** | `gamma.scan` + post-hoc filter | Stop quoting markets where live markouts stay negative even after adaptive offset; rotate out via rescan or a manual denylist |
> | **Wider default offset (0.40–0.45)** | `config.yaml` → `offset_frac_of_max_spread` | At 0.35 on a 3¢ band, gross pair capture (~2¢) is almost fully consumed by a −1.5¢ markout trip. Start wider until markouts prove a market is benign; let `adaptive_offset` tighten per market |
> | **Raise `requote_move_cents` slightly (0.5–0.6¢)** | `config.yaml` | Fewer replaces → less pick-off during cancel/post; trades reward score for queue stability |

优先级 1 之外的速度优化对本策略**收益递减**，除非实盘数据显示你的奖励份额正在输给更快的报价者（已实现/预估比率尚可，但在线时间高而份额仍然偏低）。

> Speed optimizations beyond Priority 1 have **diminishing returns** for this
> strategy unless live data shows you are losing reward share to faster quoters
> (realized/estimated ratio OK but uptime high and share still low).

---

> ---

## 推荐的工作顺序

> ## Suggested order of work

**先校准（见上方实盘上线流程）：**

> **Calibration first (see Go-live workflow above):**

0. **Paper 1–2 周**——使用真实模拟设置——以明确为正的 PnL 为门槛。
1. **部署到 US-East VPS**（3.2）——在上实盘之前做，而非之后。
2. **$100–200 小规模实盘至少 2 周**——通过 `python -m pmbot.main report` 度量。
3. **弃掉差的市场；仅在已验证的市场上扩大规模**，逐步至 $500。

> 0. **Paper 1–2 weeks** with realism settings — gate on clearly positive PnL.
> 1. **Deploy to US-East VPS** (3.2) — before live, not after.
> 2. **Live at $100–200 for 2+ weeks** — measure via `python -m pmbot.main report`.
> 3. **Drop bad markets; scale only proven ones** toward $500.

**再做速度/策略（仅在实盘数据支持继续的前提下）：**

> **Then speed/strategy (only if live data supports continuing):**

4. **事件驱动的风控撤单**（1.1）——最大的安全收益，约 1 天工作量。
5. **更宽的 offset（0.40–0.45）** + 基于 markout 的市场过滤——策略调整，不涉及代码。
6. **并行化逐市场订单操作**（1.2）——约半天，与各项叠加受益。
7. **解耦库存循环**（1.4）——约半天，有助于对冲延迟。
8. **然后考虑：** 1.3 先撤后挂、2.1 循环改为 1 s、2.3 跨市场批量化。
9. **仅在实盘数据显示奖励份额输给更快报价者时：** 2.2、2.5、3.x。

> 4. **Event-driven guard pulls** (1.1) — biggest safety win, ~1 day of work.
> 5. **Wider offset (0.40–0.45)** + markout-based market filter — strategy, not code.
> 6. **Parallel per-market order ops** (1.2) — ~half a day, compounds everything.
> 7. **Decouple inventory loop** (1.4) — ~half a day, helps hedge latency.
> 8. **Then consider:** 1.3 cancel-before-place, 2.1 loop → 1 s, 2.3 cross-market batching.
> 9. **Only if live data shows reward share loss to faster quoters:** 2.2, 2.5, 3.x.

---

> ---

## 底线

> ## Bottom line

这个 bot 不需要达到 HFT 意义上的快。它需要做到：

> This bot does not need to be fast in the HFT sense. It needs to be:

1. **慢于重新报价**（保留排队位置）但**快于撤单**（避免被吃）
2. **当库存变毒时快速对冲**
3. **稳定留在带内**（在线时间 > 报价新鲜度）

> 1. **Slow to requote** (preserve queue) but **fast to pull** (avoid pick-off)
> 2. **Fast to hedge** when inventory turns toxic
> 3. **Consistently in-band** (uptime > quote freshness)

2 s 循环对于 (3) 是可以接受的。缺口在于 (1）——风控撤单在等循环——以及撤单/挂单期间的重新报价被吃。在追逐亚秒级循环或语言重写之前，先把这些修好。

> The 2 s loop is acceptable for (3). The gaps are (1) — guard pulls wait for
> the loop — and requote pick-off during cancel/post. Fix those before chasing
> sub-second loops or a language rewrite.

在 $500 的中等层级池子上，仅实现优先级 1 就很可能足够了。优先级 2 和 3 是为了扩大资金规模或向上进入竞争更激烈的池子。

> At $500 on mid-tier pools, implementing Priority 1 alone is likely sufficient.
> Priority 2 and 3 are for scaling capital or moving upmarket into contested pools.
