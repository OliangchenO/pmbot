"""Tests for QuoteRiskDecision engine (adverse selection guard)."""

import time
from collections import deque

import pytest

from pmbot.risk import MarketGuards, QuoteRiskDecision


CFG = {
    "capital_usd": 500,
    "risk": {
        "scale_with_equity": False,
        "daily_loss_limit_usd": 25,
        "hard_kill_loss_usd": 50,
        "max_total_inventory_usd": 250,
        "theme_max_inventory_usd": 25,
        "theme_groups": {"iran": ["iran"]},
    },
    "guards": {
        "vol_window_secs": 60,
        "vol_max_move_cents": 3.0,
        "max_same_side_fills": 3,
        "same_side_window_minutes": 15,
        "market_cooldown_minutes": 45,
        "velocity_window_secs": 10,
        "velocity_max_trades": 8,
        "directional_consecutive": 5,
        "side_cooldown_minutes": 10,
        "flow_window_secs": 300,
        "flow_min_volume_shares": 200,
        "flow_widen_threshold": 0.6,
        "flow_pull_threshold": 0.85,
        "flow_widen_max_cents": 2.0,
        "markout_horizons_secs": [30, 300],
        "markout_window_minutes": 120,
        "markout_min_samples": 3,
        "markout_trip_cents": -1.5,
        # New adverse-selection guards:
        "quote_risk_mode": "shadow",
        "quote_risk_widen_score": 0.60,
        "quote_risk_pull_score": 0.85,
        "quote_risk_resume_score": 0.45,
    },
}


def _market(question="Will Iran close airspace?") -> "Market":
    from pmbot.gamma import Market
    return Market(
        question=question, condition_id="cid1",
        yes_token="y1", no_token="n1", min_size=10,
        max_spread_cents=3, daily_pool=50, liquidity=1000,
        volume_24h=500, tick=0.01, end_date=None, neg_risk=False,
    )


# ── Structure tests ──

def test_quote_risk_decision_is_frozen_dataclass():
    d = QuoteRiskDecision(
        yes_action="allow", no_action="allow",
        yes_widen=0.0, no_widen=0.0,
        reason="test", score=0.0,
    )
    assert d.yes_action == "allow"
    assert d.no_action == "allow"
    with pytest.raises(Exception):
        d.yes_action = "pull"  # frozen


# ── Flow-only signal tests ──

def test_quote_risk_decision_allows_when_no_signal():
    """低流量/无信号时保持 allow。"""
    g = MarketGuards(CFG)
    m = _market()
    now = time.time()

    decision = g.quote_risk_decision(m, now, markout_avg=None, markout_samples=0)

    assert decision.yes_action == "allow"
    assert decision.no_action == "allow"
    assert decision.score == 0.0
    assert decision.reason == "no_signal"


def test_quote_risk_decision_allows_when_volume_below_minimum():
    """成交量低于 flow_min_volume_shares 时保持 allow。"""
    g = MarketGuards(CFG)
    m = _market()
    now = time.time()
    # 100 volume < 200 min → ignored
    g._flow[m.condition_id] = deque([(now, 1.0)] * 50 + [(now, -1.0)] * 50)

    decision = g.quote_risk_decision(m, now, markout_avg=None, markout_samples=0)

    assert decision.yes_action == "allow"
    assert decision.no_action == "allow"


def test_quote_risk_decision_widens_dangerous_side_at_flow_threshold():
    """达到流量门槛时只扩大危险侧。"""
    g = MarketGuards(CFG)
    m = _market()
    now = time.time()
    # 200 volume, 140 net = 0.70 imbalance → widen (0.60–0.85)
    g._flow[m.condition_id] = deque([(now, 1.0)] * 170 + [(now, -1.0)] * 30)

    decision = g.quote_risk_decision(m, now, markout_avg=None, markout_samples=0)

    assert decision.yes_action == "allow"     # safe side
    assert decision.no_action == "widen"      # dangerous side (net > 0)
    assert decision.no_widen > 0
    assert decision.yes_widen == 0.0


def test_quote_risk_decision_widens_yes_side_when_flow_is_negative():
    """净流量为负（NO 买入）时危险侧是 YES。"""
    g = MarketGuards(CFG)
    m = _market()
    now = time.time()
    # 200 volume, -140 net → YES side is dangerous
    g._flow[m.condition_id] = deque([(now, -1.0)] * 170 + [(now, 1.0)] * 30)

    decision = g.quote_risk_decision(m, now, markout_avg=None, markout_samples=0)

    assert decision.no_action == "allow"
    assert decision.yes_action == "widen"
    assert decision.yes_widen > 0


def test_quote_risk_decision_pulls_dangerous_side_above_pull_threshold():
    """超过 pull 阈值时只撤危险侧。"""
    g = MarketGuards(CFG)
    m = _market()
    now = time.time()
    # 200 volume, 180 net = 0.90 imbalance → pull (>0.85)
    g._flow[m.condition_id] = deque([(now, 1.0)] * 190 + [(now, -1.0)] * 10)

    decision = g.quote_risk_decision(m, now, markout_avg=None, markout_samples=0)

    assert decision.yes_action == "allow"
    assert decision.no_action == "pull"
    assert decision.no_widen == 0.0


# ── Hysteresis tests ──

def test_quote_risk_decision_hysteresis_holds_pull():
    """信号下降但未到恢复阈值时保持 pull（迟滞 + 冷却计时器）。"""
    g = MarketGuards(CFG)
    m = _market()
    now = time.time()

    # Step 1: reach pull: 190 YES buys, 10 sells → NO side endangered
    g._flow[m.condition_id] = deque([(now, 1.0)] * 195 + [(now, -1.0)] * 5)
    d1 = g.quote_risk_decision(m, now, markout_avg=None, markout_samples=0)
    assert d1.no_action == "pull"
    assert d1.yes_action == "allow"

    # Step 2: weaken to 0.55 imbalance — cooldown holds pull even though
    # pure-score hysteresis alone would allow resume in some edge cases
    g._flow[m.condition_id] = deque([(now, 1.0)] * 155 + [(now, -1.0)] * 45)
    d2 = g.quote_risk_decision(m, now, markout_avg=None, markout_samples=0)
    assert d2.no_action == "pull"  # cooldown holds

    # Step 3: drop to 0.35 imbalance but still inside cooldown — stays pull
    g._flow[m.condition_id] = deque([(now, 1.0)] * 135 + [(now, -1.0)] * 65)
    d3 = g.quote_risk_decision(m, now, markout_avg=None, markout_samples=0)
    assert d3.no_action == "pull"  # cooldown still active

    # Step 4: advance past cooldown + score below resume → allow
    later = now + g.side_cooldown + 60
    g._flow[m.condition_id] = deque([(later, 1.0)] * 135 + [(later, -1.0)] * 65)
    d4 = g.quote_risk_decision(m, later, markout_avg=None, markout_samples=0)
    assert d4.no_action == "allow"  # cooldown expired, score still low → resume


def test_quote_risk_decision_allow_stays_allow_below_widen():
    """低于 widen 阈值时保持 allow，不会在 allow/widen 间振荡。"""
    g = MarketGuards(CFG)
    m = _market()
    now = time.time()

    # Flow at 0.50 imbalance (below widen 0.60, above resume 0.45)
    g._flow[m.condition_id] = deque([(now, 1.0)] * 150 + [(now, -1.0)] * 50)

    d1 = g.quote_risk_decision(m, now, markout_avg=None, markout_samples=0)
    assert d1.no_action == "allow"

    # Same signal again — stays allow
    d2 = g.quote_risk_decision(m, now, markout_avg=None, markout_samples=0)
    assert d2.no_action == "allow"


def test_quote_risk_decision_widen_transitions_to_pull():
    """从 widen 可平滑升级到 pull。"""
    g = MarketGuards(CFG)
    m = _market()
    now = time.time()

    # Step 1: 0.70 → widen
    g._flow[m.condition_id] = deque([(now, 1.0)] * 170 + [(now, -1.0)] * 30)
    d1 = g.quote_risk_decision(m, now, markout_avg=None, markout_samples=0)
    assert d1.no_action == "widen"

    # Step 2: 0.90 → pull
    g._flow[m.condition_id] = deque([(now, 1.0)] * 190 + [(now, -1.0)] * 10)
    d2 = g.quote_risk_decision(m, now, markout_avg=None, markout_samples=0)
    assert d2.no_action == "pull"


# ── Markout signal tests ──

def test_quote_risk_decision_uses_markout_signal():
    """负 markout 可提升风险评分从而触发 widen。"""
    g = MarketGuards(CFG)
    m = _market()
    now = time.time()

    # No flow signal, but markout is strongly negative
    decision = g.quote_risk_decision(
        m, now, markout_avg=-0.030, markout_samples=5,
    )

    assert decision.score > 0.6  # markout alone pushes score up
    assert decision.reason != "no_signal"


def test_quote_risk_decision_ignores_markout_with_few_samples():
    """markout 样本不足时不将其纳入评分。"""
    g = MarketGuards(CFG)
    m = _market()
    now = time.time()

    decision = g.quote_risk_decision(
        m, now, markout_avg=-0.030, markout_samples=2,
    )

    # 2 samples < 3 minimum → markout ignored, flow is zero → allow
    assert decision.score == 0.0


# ── Mid velocity tests ──

def test_quote_risk_decision_uses_mid_velocity():
    """中间价快速变动会提升风险评分。"""
    g = MarketGuards(CFG)
    m = _market()
    now = time.time()

    # Simulate a mid move of 2 cents in 30s (below vol_max_move_cents=3.0)
    g._mids[m.condition_id] = [(now - 30, 0.50), (now, 0.52)]

    decision = g.quote_risk_decision(m, now, markout_avg=None, markout_samples=0)

    # 2c move out of 3c = 0.67 velocity score
    assert decision.score > 0.0


def test_quote_risk_decision_ignores_stale_mid():
    """超过时间窗口的中间价不参与评分。"""
    g = MarketGuards(CFG)
    m = _market()
    now = time.time()

    # Old mid outside the vol_window
    g._mids[m.condition_id] = [(now - 120, 0.50), (now - 120, 0.55)]

    decision = g.quote_risk_decision(m, now, markout_avg=None, markout_samples=0)

    assert decision.score == 0.0


# ── Mode / config tests ──

def test_quote_risk_mode_defaults_to_shadow():
    """未配置时默认使用 shadow 模式。"""
    cfg_no_mode = {
        **{k: v for k, v in CFG.items() if k != "guards"},
        "guards": {
            **{k: v for k, v in CFG["guards"].items()
               if k not in ("quote_risk_mode", "quote_risk_widen_score",
                            "quote_risk_pull_score", "quote_risk_resume_score")},
        },
    }
    g = MarketGuards(cfg_no_mode)
    assert g.quote_risk_mode == "shadow"


def test_quote_risk_off_mode_returns_allow():
    """off 模式始终返回 allow。"""
    cfg_off = {**CFG, "guards": {**CFG["guards"], "quote_risk_mode": "off"}}
    g = MarketGuards(cfg_off)
    m = _market()
    now = time.time()

    # Even with high flow
    g._flow[m.condition_id] = deque([(now, 1.0)] * 200)
    decision = g.quote_risk_decision(m, now, markout_avg=None, markout_samples=0)

    assert decision.yes_action == "allow"
    assert decision.no_action == "allow"


# ── 10-round stability test ──

def test_quote_risk_decision_stable_under_constant_signal():
    """同一稳定信号 10 次轮询最多产生一次动作变更（迟滞有效）。"""
    g = MarketGuards(CFG)
    m = _market()
    now = time.time()

    # Constant signal: 0.70 imbalance (widen zone)
    g._flow[m.condition_id] = deque([(now, 1.0)] * 170 + [(now, -1.0)] * 30)

    actions = []
    for _ in range(10):
        d = g.quote_risk_decision(m, now, markout_avg=None, markout_samples=0)
        actions.append((d.yes_action, d.no_action))

    # All 10 rounds should produce the same action pair
    assert len(set(actions)) == 1


# ── Cooldown tests ──

def test_pull_cooldown_survives_zero_score():
    """B1: 信号归零时 pull 冷却不被打断。

    复现：先触发 NO pull→1 秒后清空 flow→必须仍保持 pull。
    旧代码在 score <= 0 时直接返回 allow 绕过冷却判断。
    """
    g = MarketGuards(CFG)
    m = _market()
    now = time.time()

    # Step 1: reach pull on NO side
    g._flow[m.condition_id] = deque([(now, 1.0)] * 195 + [(now, -1.0)] * 5)
    d1 = g.quote_risk_decision(m, now, markout_avg=None, markout_samples=0)
    assert d1.no_action == "pull"

    # Step 2: 1 s later, flow cleared — score goes to zero.
    # The cooldown must keep NO pulled.
    g._flow[m.condition_id] = deque()
    d2 = g.quote_risk_decision(m, now + 1, markout_avg=None, markout_samples=0)
    assert d2.no_action == "pull", (
        "score 归零时 pull 侧必须仍被冷却保护，"
        f"实际得到 no_action={d2.no_action}"
    )


def test_pull_cooldown_expires_normally():
    """冷却过期后信号仍为零时恢复 allow。"""
    g = MarketGuards(CFG)
    m = _market()
    now = time.time()

    # reach pull
    g._flow[m.condition_id] = deque([(now, 1.0)] * 195 + [(now, -1.0)] * 5)
    d1 = g.quote_risk_decision(m, now, markout_avg=None, markout_samples=0)
    assert d1.no_action == "pull"

    # advance past cooldown with zero flow
    later = now + g.side_cooldown + 60
    g._flow[m.condition_id] = deque()
    d2 = g.quote_risk_decision(m, later, markout_avg=None, markout_samples=0)
    assert d2.no_action == "allow", (
        "冷却过期、信号归零后应恢复 allow"
    )


# ── Widen tick floor test ──

def test_widen_amount_minimum_is_market_tick():
    """B2: widen 下限必须保证改变至少一个 tick，不能停留在原价。

    默认 flow_widen_max = 0.02, market.tick = 0.01。
    25% floor = 0.005 → _round_tick(0.50 - 0.005, 0.01) == 0.50（原价不变）。
    修复后应以 market.tick (0.01) 为下限。
    """
    g = MarketGuards(CFG)
    m = _market()
    # tick=0.01, flow_widen_max=0.02 → min_frac = 0.01/0.02 = 0.5
    # widened by at least 0.02*0.5 = 0.01 = 1 tick
    amount = g._compute_widen_amount(0.60, m)
    # 0.60 is right AT the widen_score threshold (0.60)
    # frac = 0 → floored to min(0.5, 1.0) = 0.5
    # 0.02 * 0.5 = 0.01
    assert amount >= m.tick, (
        f"widen_amount ({amount}) 必须 >= market.tick ({m.tick})"
    )


def test_widen_amount_fraction_clamps_to_one():
    """widen 分数不会超过 1.0（不会把报价拉出合理范围）。"""
    g = MarketGuards(CFG)
    m = _market()
    # Score at pull threshold → frac = 1.0
    amount = g._compute_widen_amount(0.85, m)
    assert amount == g.flow_widen_max


def test_pull_cooldown_holds_despite_flow_reversal():
    """B3: 方向反转后原侧 pull 冷却不被打断。

    最小复现：正向 flow → NO pull，1 秒后反向 flow → 原侧 NO
    仍必须是 pull（冷却未过期），而不是变成 allow。
    """
    g = MarketGuards(CFG)
    m = _market()
    now = time.time()

    # Step 1: net > 0 (YES buying) → NO is the dangerous side → pull
    g._flow[m.condition_id] = deque([(now, 1.0)] * 195 + [(now, -1.0)] * 5)
    d1 = g.quote_risk_decision(m, now, markout_avg=None, markout_samples=0)
    assert d1.no_action == "pull"
    assert d1.yes_action == "allow"

    # Step 2: 1 s later, flow reverses — new net < 0 (NO buying) →
    # YES is now the dangerous side.  But NO was pulled in step 1
    # and the cooldown has not expired, so NO must remain pull.
    g._flow[m.condition_id] = deque([(now + 1, -1.0)] * 195 + [(now + 1, 1.0)] * 5)
    d2 = g.quote_risk_decision(m, now + 1, markout_avg=None, markout_samples=0)
    assert d2.yes_action == "pull", (
        "反向 flow 触发 YES pull 正确"
    )
    assert d2.no_action == "pull", (
        f"反向 flow 不能覆盖原侧 NO 的 pull 冷却，实际 no_action={d2.no_action}"
    )


def test_pull_cooldown_survives_mid_velocity():
    """B4: mid-velocity 分支不绕过 pull 冷却。

    最小复现：高 flow → NO pull → 清空 flow + 弱 mid move（score=0.30）。
    旧代码中 mid_score >= score 分支重新调用 _resolve_action，
    score 低于 resume(0.45) → allow，覆盖了 NO 的 pull 冷却。
    """
    g = MarketGuards(CFG)
    m = _market()
    now = time.time()

    # Step 1: reach pull on NO side via flow
    g._flow[m.condition_id] = deque([(now, 1.0)] * 195 + [(now, -1.0)] * 5)
    d1 = g.quote_risk_decision(m, now, markout_avg=None, markout_samples=0)
    assert d1.no_action == "pull"

    # Step 2: clear flow, inject weak mid move (2c out of 3c = 0.67 → score=0.67)
    # score > resume (0.45) but < pull (0.85) → _resolve_action returns "allow".
    # WITH cooldown active on NO side, NO must stay pull.
    g._flow[m.condition_id] = deque()  # flow → 0
    g._mids[m.condition_id] = [(now, 0.50), (now + 1, 0.52)]
    d2 = g.quote_risk_decision(m, now + 1, markout_avg=None, markout_samples=0)
    assert d2.no_action == "pull", (
        f"mid-velocity 覆盖 _resolve_action 后必须重新检查冷却，"
        f"实际 no_action={d2.no_action}"
    )
