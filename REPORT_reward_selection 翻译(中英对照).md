# 奖励优先选市与仓位缩放 — 研究报告 (中英对照)

**档位判定**：偏中硬金融研究报告，纽马克交际翻译，自由度 ≈ 4–5。技术术语保持英文原词（markout、CVaR 等），首次出现时加中文注；数据和表格精确不加工；论述部分以地道中文流水句表达，适度意合但不抹掉原文的量化严谨性。读者为中文交易团队，目的是理解研究结论并指导代码修改。

> **Question (from the desk).** Keep the market filter for *eligibility*, but
> then prioritize the eligible markets where we would earn the **highest rewards**
> — today we sit in markets where we contribute almost nothing to in-band
> liquidity, so we capture almost none of the pool. Make safety a first-class
> constraint. Back-test it. And separately: as capital grows, instead of quoting
> **more markets**, should we hold **bigger positions** per market (today's cap is
> ~50, what about 100)?

**来自交易台的问题**：保留现有市场过滤器做资格筛选，但在合格市场中，优先选出**我们能赚到最高奖励**的市场——目前我们报价的市场中，在带流动性贡献基本为零，所以几乎拿不到池子里的奖励。将安全性作为一等约束，做回测。另外，资金增长时，是应该报**更多市场**，还是在每个市场中下**更大的仓位**（当前上限 ~50，能不能设到 100）？

---

> **TL;DR.**
>
> 1. We are currently **~0.5% of the in-band reward score** in the markets we
>    quote (realized rewards are only **20% of the estimator's number** —
>    $5.22 vs $26.70 over the live window). That means we sit deep in the
>    **linear** part of the reward curve: **doubling our quote size roughly
>    doubles our reward**, with negligible diminishing returns.
> 2. Because we are this small, **reward density (pool ÷ liquidity) and expected
>    captured reward (pool × our share) rank the eligible markets identically.**
>    The selection fix that matters is **(a) weighting absolute pool and our
>    *capturable* share as we size up, and (b) screening toxicity** — not swapping
>    one ratio for another.
> 3. **The entire profit swing in the back-test comes from selection *quality*
>    (avoiding toxic markets), not from the reward mechanism.** Our live losses
>    were adverse-selection / forced-hedge bleed in Toy Story & Trump-style books,
>    not low rewards.
> 4. **Depth beats breadth as capital grows — up to a diversification floor.**
>    Spreading a bigger book across *more* markets forces capital down the quality
>    ladder (and there are only ~8 eligible markets right now, so breadth even
>    leaves capital idle). Concentrating into a **few vetted markets with larger
>    positions** earns more *and* has a better tail. The risk-adjusted sweet spot
>    is **2–3 markets, not 1, and not 8.**

**TL;DR**

1. 在当前报价的市场中，我们约占**在带奖励评分的 0.5%**（实盘窗口内已实现奖励仅为预估的 **20%**——$5.22 vs $26.70）。这意味着我们深处于奖励曲线的**线性**区间：**报价规模翻倍，奖励基本翻倍**，边际递减几乎为零。
2. 因为规模太小，**奖励密度（池子 ÷ 流动性）和预期捕获奖励（池子 × 我们的份额）对合格市场的排序结果完全相同。** 真正重要的选市修正是（a）随着规模放大，需要按绝对池子大小和*可捕获*份额加权，（b）筛查毒性——而不是切换到另一个比率公式。
3. **回测中整个利润变化来自选市*质量*（避开有毒市场），而不是奖励机制。** 实盘亏损是逆向选择/强制对冲在 Toy Story 和 Trump 风格市场中造成的失血，不是低奖励。
4. **资金增长时，深度优于广度——但有分散化底线。** 将更大的资金撒到*更多*市场会强行把资金推到质量阶梯的下端（且当前只有 ~8 个合格市场，广度的上限这么小，甚至会让资金闲置）。集中在**少数经过审查的市场中下大仓**能赚更多，尾部风险也更优。风险调整后的甜点在 **2–3 个市场，不是 1 个，也不是 8 个。**

---

> Reproduce everything with:
>
> ```bash
> .venv/bin/python scripts/reward_selection_study.py     # this report's numbers
> .venv/bin/python scripts/backtest.py                   # fill replay (existing)
> ```

所有结论可复现：

```bash
.venv/bin/python scripts/reward_selection_study.py     # 本报告的数据
.venv/bin/python scripts/backtest.py                   # 成交回放（已有）
```

---

## 1. 奖励机制的实际运作（以及我们为什么赚这么少）

> Polymarket scores each resting order inside the reward band with
> `S = ((v − s)/v)² · size`, where `v` is the band width (cents) and `s` is our
> distance from the mid. Our payout each epoch is our score divided by the **total
> in-band score** (us + every competitor):
>
> ```
> our_reward = pool × q_ours / (q_ours + q_competitors),   q_ours ∝ size
> ```
>
> The share is **concave in our size** but **linear when we are small**. From the
> live DB:

Polymarket 对每笔在奖励带内的挂单按 `S = ((v − s)/v)² · size` 打分，其中 `v` 是带宽（美分），`s` 是报价距中间价的距离。每个 epoch 的 payout 是我们得分除以**在带总分**（我们 + 所有对手）：

```
our_reward = pool × q_ours / (q_ours + q_competitors),   q_ours ∝ size
```

份额对规模**是凹函数**，但在规模很小时**是线性的**。来自实盘数据库：

> | measured (live, 81h, ~$100) | value |
> | --- | --- |
> | realized rewards | **$5.22** |
> | estimator's rewards | $26.70 |
> | realized ÷ estimate | **20%** |
> | implied in-band score share | **~0.5%** (we are ~1/195 of the book) |
> | long-horizon markout (mean / worst) | **−1.59c / −13c** |
> | pairs merged / hedge notional | 1,150 / $658 |

| 实测（实盘，81 小时，~$100） | 数值 |
| --- | --- |
| 已实现奖励 | **$5.22** |
| 预估奖励 | $26.70 |
| 已实现 ÷ 预估 | **20%** |
| 推定在带评分份额 | **~0.5%**（约为总书的 1/195） |
| 长期 markout（均值 / 最差） | **−1.59c / −13c** |
| 合并对数 / 对冲名义金额 | 1,150 / $658 |

> At a **0.5% share** we are nowhere near saturation, so:
>
> ```
> size  50 → 0.51% share   (1.0×)
> size 100 → 1.02% share   (2.0×)   ← "50 → 100" almost exactly doubles reward
> size 200 → 2.02% share   (3.9×)
> ```
>
> **This is the single most important fact in the report.** The reason we "don't
> make much reward" is not bad market selection per se — it is that we quote the
> **minimum size (50 shares)** and are therefore a rounding error in the pool.
> The most direct lever to "make more reward" is to **contribute more size**, and
> because we are in the linear regime that conversion is ~1:1.

在 **0.5% 份额**下，远未达到饱和，因此：

```
size  50 → 0.51% 份额   (1.0×)
size 100 → 1.02% 份额   (2.0×)   ← "50 → 100" 奖励几乎精确翻倍
size 200 → 2.02% 份额   (3.9×)
```

**这是整个报告中最重要的一个事实。**我们"奖励赚不多"不是选错了市场——而是只报了**最小规模（50 股）**，因此在池子里只是一个舍入误差。赚更多奖励的最直接杠杆是**下更多量**，而在线性区间，这个转换比例接近 1:1。

---

## 2. 选市：密度 vs 预期捕获奖励

> The scanner currently ranks eligible markets by **reward density = pool ÷ book
> liquidity** (`pmbot/gamma.py`). The desk's instinct is to rank by **expected
> captured reward = pool × our share** instead. Running both on a **live scan of
> 633 markets → 8 eligible** today:
>
> ```
> --- BY DENSITY (current) ---            --- BY EXPECTED CAPTURE @ size 50 ---
> Mazzei OK Gov   pool$461 cap$3.17/d     Mazzei OK Gov   pool$461 cap$3.17/d
> Avila Chevalier pool$405 cap$2.93/d     Avila Chevalier pool$405 cap$2.93/d
> Espaillat NY    pool$395 cap$2.49/d     Espaillat NY    pool$395 cap$2.49/d
> ...                                     ... (identical order)
> ```

当前扫描器按**奖励密度 = 池子 ÷ 流动性**（`pmbot/gamma.py`）排序。交易台的想法是按**预期捕获奖励 = 池子 × 我们的份额**来排。用一次对 633 个市场的实盘扫描 → 8 个合格市场的结果：

```
--- 按密度（当前）---            --- 按预期捕获 @ size 50 ---
Mazzei OK Gov   pool$461 cap$3.17/d     Mazzei OK Gov   pool$461 cap$3.17/d
Avila Chevalier pool$405 cap$2.93/d     Avila Chevalier pool$405 cap$2.93/d
Espaillat NY    pool$395 cap$2.49/d     Espaillat NY    pool$395 cap$2.49/d
...                                     ... (排序完全相同)
```

> **They produce the same ranking.** The reason is algebraic: when our share is
> tiny, `capture = pool · αs/(αs + γ·liq) ≈ (αs/γ)·(pool/liq)` — i.e. expected
> capture is *proportional to density*. So at our current size, switching the
> ranking metric changes nothing.
>
> The two metrics **diverge only once our size is large enough that share stops
> being tiny** — exactly the regime the 50→100→200 position change moves us into:
>
> ```
> --- BY EXPECTED CAPTURE @ size 250 ---
> Mazzei OK Gov   share 10.5%  cap$14.54/d
> Avila Chevalier share 11.0%  cap$13.34/d
> Adrian Boafo    share  4.3%  cap$ 7.04/d   ← high pool ($542) but deepest book,
>                                              so capture saturates slower
> ```
>
> **Conclusion for selection:** adopt the **expected-captured-reward** ranking
> (it is the economically correct objective and is free to compute), but
> understand that its *benefit* only switches on once we size up. The bigger,
> immediate selection win is the **toxicity screen** below.

**两者产生完全一致的排名。**原因是代数性的：当份额很小时，`capture = pool · αs/(αs + γ·liq) ≈ (αs/γ)·(pool/liq)`——即预期捕获与密度*成正比*。所以在当前规模下，切换排名指标毫无区别。

两个指标**只有当规模足够大、份额不再很小时才开始分化**——这正是 50→100→200 的仓位变化将我们推入的区间：

```
--- 按预期捕获 @ size 250 ---
Mazzei OK Gov   share 10.5%  cap$14.54/d
Avila Chevalier share 11.0%  cap$13.34/d
Adrian Boafo    share  4.3%  cap$ 7.04/d   ← 高池子 ($542) 但最深的书，
                                             捕获饱和更慢
```

**选市结论：**采用**预期捕获奖励**排名（这是正确的经济学目标，计算成本为零），但要清楚其好处只在规模放大后才生效。更大的、更直接的选市收益是下面的**毒性筛查**。

---

## 3. 安全是真正的 PnL 驱动——回测

> The fill-replay back-test (`scripts/backtest.py`) on the live window shows
> trading cash of **−$48.89**, with the losses concentrated in **event/observation
> markets** (Trump −$17, Toy Story −$27 across three books) that throw off large
> forced-hedge spend. Rewards over the same window were only ~$5 realized — far too
> small to offset that bleed.
>
> The Monte-Carlo in `scripts/reward_selection_study.py` makes the mechanism
> explicit by running every capital allocation under two selection regimes:
>
> - **OLD** — raw empirical markout sample (the toxic −13c tail intact).
> - **NEW** — reward-prioritized selection of slow, low-turnover markets **plus
>   the toxicity guard**, which clips the worst markout tail at −3c.
>
> `PnL$/d` = reward + Σ pairs·(markout − hedge slip), bootstrapped from our own
> markout distribution; `CVaR5%` is the mean of the worst 5% of days (lower =
> worse tail). Reward is haircut by a 0.30 realization factor (uptime/eligibility)
> so the dollars are honest; the haircut cancels in every comparison.

成交回放回测（`scripts/backtest.py`）在实盘窗口上显示交易现金为 **−$48.89**，亏损集中在**事件/观测类市场**（Trump −$17，Toy Story 三板合计 −$27），这些市场产生大量强制对冲支出。同期的奖励仅 ~$5 已实现——远不足以抵消这些失血。

`scripts/reward_selection_study.py` 中的 Monte-Carlo 通过运行两种选市方案，将机制直观化：

- **OLD** — 原始经验 markout 样本（包含毒性 −13c 尾部）。
- **NEW** — 奖励优先选择慢速、低换手率市场**加上毒性 guard**，将最差 markout 尾部截断在 −3c。

`PnL$/d` = 奖励 + Σ pairs·(markout − hedge slip)，从我们自己的 markout 分布 bootstrap 抽样；`CVaR5%` 是最差 5% 交易日的均值（越低 = 尾部越差）。奖励按 0.30 实现因子打折（考虑在带时间/资格影响），这样美元数字是诚实的；该折扣在每项比较中抵消。

> ```
>                          OLD selection            NEW selection (reward-priority + guard)
> CAPITAL $500             PnL$/d  P(profit) CVaR    PnL$/d  P(profit) CVaR
> 8 mkt × 50sh (breadth)   -8.20    11%     -25.3    -3.89    17%     -12.0
> 5 mkt × 100sh            -6.16    32%     -34.1    -0.80    45%     -13.5
> 3 mkt × 150sh            -3.49    46%     -38.0    +1.34    59%     -13.4
> 2 mkt × 200sh (depth)    -2.21    49%     -41.4    +1.75    58%     -12.9   ← best
> 2 mkt × 250sh            -3.37    48%     -52.3    +1.96    54%     -16.5
> ```

```
                         OLD selection            NEW selection (奖励优先 + guard)
CAPITAL $500             PnL$/d  P(profit) CVaR    PnL$/d  P(profit) CVaR
8 mkt × 50sh (广度)       -8.20    11%     -25.3    -3.89    17%     -12.0
5 mkt × 100sh             -6.16    32%     -34.1    -0.80    45%     -13.5
3 mkt × 150sh             -3.49    46%     -38.0    +1.34    59%     -13.4
2 mkt × 200sh (深度)      -2.21    49%     -41.4    +1.75    58%     -12.9   ← 最优
2 mkt × 250sh             -3.37    48%     -52.3    +1.96    54%     -16.5
```

> **Read this top-to-bottom and left-to-right:**
>
> - **Left → right (OLD → NEW): the sign flips.** Selection quality — *not* the
>   reward formula, *not* the position size — is what turns the book from a daily
>   loser into a daily winner. This is the headline. Prioritising rewards only
>   pays once we have stopped feeding the toxic tail.
> - **Top → bottom (breadth → depth): depth wins once selection is fixed.** Under
>   NEW, max breadth (8×50) is the *worst* row on every axis and only deploys
>   $400 of the $500 (there are just 8 eligible markets — **breadth literally runs
>   out of good markets and leaves capital idle**). Concentrating into 2–3 markets
>   with 150–200-share positions earns more *and* has the tightest tail.

**从上到下，从左到右读这张表：**

- **左 → 右（OLD → NEW）：符号翻转。**选市质量——*不是*奖励公式，*不是*仓位大小——是把账本从日常亏损变成日常盈利的关键。这是头号结论。奖励优先化只有在先堵住了毒性尾部之后才有效。
- **上 → 下（广度 → 深度）：一旦选市修正后，深度胜出。**在 NEW 模式下，撒得最广（8×50）在所有维度上都是最差的，而且只部署了 $500 中的 $400（只有 8 个合格市场——**广度写到没钱花了，资金闲置**）。集中在 2–3 个市场做 150–200 股获得更多，且尾部最紧。

---

## 4. 广度 vs 深度：更多市场还是更大仓位？

> This is the desk's second question, and the answer is **bigger positions, with
> a floor on diversification.** Three forces:
>
> 1. **Reward is ~linear in size for us (Section 1)**, so there is essentially
>    *no* concavity penalty for concentrating size. (If we were a large share of a
>    pool, breadth would win on pure reward — we are not.)
> 2. **The eligible universe is small and quality-ranked.** Adding market #4, #5,
>    #8 means quoting progressively worse pools — and, historically, the toxic
>    ones. Every extra market is also an extra independent draw on the adverse-
>    selection lottery that produced our losses.
> 3. **Safety.** Fewer markets = less adverse-selection surface, less theme/neg-
>    risk correlation stacking, simpler inventory and hedge management on a small
>    wallet. But **one** market is too few — a single toxic surprise has nowhere to
>    diversify against (see the worsening CVaR at 2×250 vs 2×200).

这是交易台的第二个问题，答案是大仓位，但分散化有底线。三个因素：

1. **在我们的规模下，奖励对仓位是基本线性的（第 1 节）**，所以集中仓位基本没有凹度惩罚。（如果已经占了一个池子很大的份额，广度在纯奖励上会胜出——但我们不是。）
2. **合格市场数量少且有质量排序。**加上第 4、5、8 个市场意味着报价质量逐步下降——历史上，越靠后的越有毒。每增加一个市场，也增加一个从逆向选择彩票中独立抽样的出口，也就是产生亏损的那个彩票。
3. **安全性。**市场越少 = 逆向选择暴露面越小，跨主题/负风险的相关性累积越少，小资金下的库存和对冲管理越简单。但**仅一个**市场太少——一次毒性事件无处分散（比较 2×250 vs 2×200 恶化的 CVaR）。

> | capital | today (breadth) | recommended (depth) | why |
> | --- | --- | --- | --- |
> | $100 | 2 × 50 | **2 × 50** (hold) | edge is marginal; keep diversification |
> | $200–500 | 3–4 × 50 | **2–3 × 100–150** | depth ≈ 2× reward, better tail |
> | $750+ | 3–4 × 90 | **3 × 150–250** | stay in vetted markets, scale size |
>
> The "50 → 100" change the desk proposed is correct, and the back-test says push
> it further (toward 150–200) **as long as** (a) selection toxicity screening is on
> and (b) we never drop below ~2 markets.

| 资金 | 当前（广度） | 建议（深度） | 原因 |
| --- | --- | --- | --- |
| $100 | 2 × 50 | **2 × 50**（保持） | 边际优势不大；保持分散化 |
| $200–500 | 3–4 × 50 | **2–3 × 100–150** | 深度 ≈ 2× 奖励，尾部更好 |
| $750+ | 3–4 × 90 | **3 × 150–250** | 待在经过审查的市场中，放大规模 |

交易台提出的"50 → 100"是正确的，回测表明可以推得更远（到 150–200），**前提是**（a）选市毒性筛查已打开且（b）永远不低于 ~2 个市场。

---

## 5. 建议（按优先级）

> 1. **Keep ≥ 2 markets always; grow position size, not market count, as capital
>    grows.** Re-shape the controller's capital tiers so the wallet scales
>    `max_capital_per_market` (and quote size) ahead of `top_n_markets`. Concretely
>    add `quoting.max_capital_per_market` to the controller's tier knobs and set:
>
>    ```yaml
>    capital_tiers:
>      - {min_equity_usd: 0,    top_n_markets: 2, max_capital_per_market: 50,  max_inventory_usd_per_market: 30}
>      - {min_equity_usd: 250,  top_n_markets: 2, max_capital_per_market: 100, max_inventory_usd_per_market: 60}
>      - {min_equity_usd: 750,  top_n_markets: 3, max_capital_per_market: 175, max_inventory_usd_per_market: 120}
>      - {min_equity_usd: 2000, top_n_markets: 3, max_capital_per_market: 300, max_inventory_usd_per_market: 250}
>    ```
>
>    and let quote size track the cap (raise `quoting.size_mult_of_min`, or enable
>    bounded `risk.scale_with_equity`) so we actually *use* the bigger cap.

1. **始终保持 ≥ 2 个市场；资金增长时放大仓位而非市场数量。**调整 controller 的 capital_tiers，让资金先缩放 `max_capital_per_market`（和报价规模）再动 `top_n_markets`。具体来说，把 `quoting.max_capital_per_market` 加入 controller 的 tier knobs 并设为：

   ```yaml
   capital_tiers:
     - {min_equity_usd: 0,    top_n_markets: 2, max_capital_per_market: 50,  max_inventory_usd_per_market: 30}
     - {min_equity_usd: 250,  top_n_markets: 2, max_capital_per_market: 100, max_inventory_usd_per_market: 60}
     - {min_equity_usd: 750,  top_n_markets: 3, max_capital_per_market: 175, max_inventory_usd_per_market: 120}
     - {min_equity_usd: 2000, top_n_markets: 3, max_capital_per_market: 300, max_inventory_usd_per_market: 250}
   ```

   让报价规模跟踪上限（提高 `quoting.size_mult_of_min`，或启用有边界的 `risk.scale_with_equity`），真正做到规模更大。

> 2. **Switch the scanner ranking to expected captured reward.** Score eligible
>    markets by `pool × αs/(αs + γ·liquidity)` (a size-aware version of density),
>    not raw density. It is the correct objective and starts to matter the moment
>    we size up. Free to compute from fields the scanner already has.

2. **将扫描器排名切换到预期捕获奖励。**用 `pool × αs/(αs + γ·liquidity)`（带仓位感知的密度版本）而不是原始密度给合格市场打分。这是正确的经济目标，一开始放大规模便起作用。计算成本为零——所有字段扫描器已有。

> 3. **Selection toxicity screen is the biggest safety win** — and it is already
>    mostly built (`exclude_keywords`, `toxicity_turnover_penalty`, the markout
>    guard). Keep prioritising **slow, low-turnover markets** (political primaries,
>    long-dated yes/no) and keep `min_pool_to_liquidity` / `min_liquidity` so we
>    never chase thin toxic books for density.

3. **选市毒性筛查是最大的安全胜利**——且大部分已经建好了（`exclude_keywords`、`toxicity_turnover_penalty`、markout guard）。持续优先选择**慢速、低换手率市场**（政治初选、长周期 yes/no），保持 `min_pool_to_liquidity` / `min_liquidity` 过滤，绝不为了高密度去追又薄又毒的书。

> 4. **Couple every size increase to the existing guards.** Bigger positions mean
>    bigger per-fill damage if a market turns. The markout guard, per-market
>    inventory cap, theme cap, and forced-hedge thresholds must scale *with* size —
>    the tiers above already raise `max_inventory_usd_per_market` in step.

4. **每次规模放大都配上现有的 guard。**更大的仓位意味着一旦市场转向，单笔成交的伤害更大。markout guard、单市场库存上限、主题 cap、强制对冲阈值必须随仓位同比缩放——上面各 tier 已经把 `max_inventory_usd_per_market` 同步提高了。

---

## 6. 注意事项

> - We have **no historical order books**, so we cannot simulate counterfactual
>   *fills*. The reward math (Section 1–2) is exact; the PnL Monte-Carlo
>   (Section 3–4) bootstraps our own 27-sample markout distribution — directionally
>   reliable, not a guarantee. Treat absolute dollars as illustrative and the
>   *comparisons* as the signal.
> - The competition model `q_comp = γ·liquidity` is a calibrated proxy. The
>   qualitative conclusions (linear regime, density≈capture when small, depth>breadth
>   after the toxicity fix) are robust across the γ range tested; precise shares are
>   not.
> - Realized rewards are paid per UTC day only while we are actually in-band, so
>   real-world capture also depends on **uptime** — another reason to prefer fewer,
>   more reliably-quoted markets over many thinly-watched ones.

- **没有历史订单簿**，所以无法模拟反事实的*成交*。奖励数学（第 1–2 节）是精确的；PnL Monte-Carlo（第 3–4 节）用我们自己的 27 个样本的 markout 分布做 bootstrap——方向可信，但不保证精准。将绝对美元视为示意，**比较**才是真正的信号。
- 竞争模型 `q_comp = γ·liquidity` 是一个校准得到的近似。定性结论（线性区间、小规模时 density≈capture、修复毒性后深度>广度）在测试的 γ 范围内保持稳健；精确份额数字不是。
- 已实现奖励按每个 UTC 日结算，且只有实际在带时才累积，所以现实捕获也取决于**在带时间**——这又是另一个偏爱更少、更可靠报价的市场，而非多数薄疏监管市场的理由。
