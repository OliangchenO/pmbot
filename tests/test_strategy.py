"""Tests for strategy.py quoting and reward estimation."""

from datetime import datetime

import pytest

from pmbot.books import Book
from pmbot.gamma import Market
from pmbot.strategy import (
    Quote,
    adaptive_offset,
    book_feed_stale,
    book_is_quotable,
    compute_net_shadow_score,
    compute_recovery_quote,
    compute_quotes,
    microprice,
    reconcile_quotes,
)


def test_quiet_book_with_live_feed_is_not_stale():
    # Feed alive (heartbeat 5s ago), but this book hasn't ticked in 40s — a
    # quiet market. We must NOT pull quotes: idle books are safe farming time.
    assert book_feed_stale(feed_age=5.0, book_age=40.0, max_stale=25.0) is False


def test_dead_feed_is_stale():
    # No websocket traffic at all for 30s > max_stale — socket lagging/dead.
    assert book_feed_stale(feed_age=30.0, book_age=30.0, max_stale=25.0) is True


def test_silently_dropped_token_backstop():
    # Feed alive but one book hasn't updated in >4x max_stale (and >120s):
    # backstop catches a silently dropped single-token subscription.
    assert book_feed_stale(feed_age=2.0, book_age=130.0, max_stale=25.0) is True
    # Just under the 120s floor with a live feed -> still safe.
    assert book_feed_stale(feed_age=2.0, book_age=110.0, max_stale=25.0) is False


def test_feed_age_floor_uses_120s_when_max_stale_small():
    # 4 * 10 = 40, but the floor is 120s, so a 90s-quiet book stays quotable.
    assert book_feed_stale(feed_age=3.0, book_age=90.0, max_stale=10.0) is False


def test_book_tracker_feed_age():
    from pmbot.books import BookTracker
    bt = BookTracker([])
    bt.last_msg_ts = 1000.0
    assert bt.feed_age(now=1007.0) == 7.0


def _market(**kw) -> Market:
    defaults = dict(
        question="Test market?",
        condition_id="0xabc",
        yes_token="yes_tok",
        no_token="no_tok",
        min_size=10.0,
        max_spread_cents=3.0,
        daily_pool=50.0,
        liquidity=1000.0,
        volume_24h=500.0,
        tick=0.01,
        end_date=None,
        neg_risk=False,
    )
    defaults.update(kw)
    return Market(**defaults)


def _book(bid=0.48, ask=0.52, bid_sz=100, ask_sz=100) -> Book:
    b = Book("yes_tok")
    b.bids = {bid: bid_sz}
    b.asks = {ask: ask_sz}
    return b


CFG = {
    "scanner": {"mid_range": [0.15, 0.85]},
    "quoting": {
        "offset_frac_of_max_spread": 0.35,
        "size_mult_of_min": 1.0,
        "max_capital_per_market": 90,
        "skew_strength": 0.6,
        "max_book_spread_mult_of_band": 3.0,
        "flow_drift_max_cents": 1.0,
        "adaptive_markout_gain": 1.0,
        "adaptive_tighten_max_cents": 0.5,
        "adaptive_widen_max_cents": 2.0,
    },
}


def test_recovery_quote_uses_market_center_outside_normal_mid_range():
    """超时补单不应被普通做市的中间价区间过滤挡住。"""
    market = _market()
    quote, pricing = compute_recovery_quote(
        market, _book(bid=0.10, ask=0.12), net_yes_exposure_usd=2.5,
        cfg=CFG, max_inventory_usd=30.0, complement_token=market.no_token,
        size=20.0,
    )

    assert quote is not None
    assert quote.token_id == market.no_token
    assert quote.size == 20.0
    assert quote.price == pytest.approx(0.88)
    assert pricing["fair"] == pytest.approx(0.11)
    assert pricing["recovery_path"] == "market_center_recovery"


def test_net_shadow_score_uses_sufficient_market_inputs():
    """P2.1 must score from this market's measured economics, not global data."""
    market = _market(daily_pool=240.0)
    cfg = {"scanner": {"net_shadow": {
        "min_reward_samples": 2, "min_uptime_samples": 2,
        "min_markout_samples": 2, "min_recovery_samples": 2,
        "reward_realization_prior": 0.5, "uptime_prior": 0.5,
        "markout_cost_per_hour_prior": 0.2,
        "recovery_cost_per_hour_prior": 0.3,
    }}}
    score, audit = compute_net_shadow_score(market, {
        "reward_realization": 0.8, "reward_samples": 3,
        "uptime_ratio": 0.5, "uptime_samples": 3,
        "markout_cost_per_hour": 1.0, "markout_samples": 3,
        "recovery_cost_per_hour": 0.5, "recovery_samples": 3,
        "taker_fee_per_hour": 0.25, "taker_fee_samples": 1,
    }, cfg)

    assert score == pytest.approx(2.25)
    assert audit["reward_realization"]["source"] == "market"
    assert audit["insufficient_sample"] is False


def test_net_shadow_score_uses_conservative_priors_for_insufficient_market_data():
    """A new market must be labelled uncertain rather than inheriting global results."""
    market = _market(daily_pool=240.0)
    cfg = {"scanner": {"net_shadow": {
        "min_reward_samples": 2, "min_uptime_samples": 2,
        "min_markout_samples": 2, "min_recovery_samples": 2,
        "reward_realization_prior": 0.5, "uptime_prior": 0.5,
        "markout_cost_per_hour_prior": 0.2,
        "recovery_cost_per_hour_prior": 0.3,
    }}}

    score, audit = compute_net_shadow_score(market, {}, cfg)

    assert score == pytest.approx(2.0)
    assert audit["uptime"]["source"] == "prior"
    assert audit["insufficient_sample"] is True


def test_book_is_quotable_rejects_one_sided():
    b = Book("t")
    b.last_trade_price = 0.5
    assert not book_is_quotable(b, 0.03, 3.0)


def test_book_is_quotable_rejects_wide_spread():
    b = _book(bid=0.40, ask=0.60)
    assert not book_is_quotable(b, 0.03, 3.0)


def test_microprice_weights_by_size():
    b = _book(bid=0.48, ask=0.52, bid_sz=100, ask_sz=300)
    fair = microprice(b)
    assert fair is not None
    assert 0.48 < fair < 0.50


def test_compute_quotes_returns_two_sides():
    m = _market()
    quotes = compute_quotes(m, _book(), 0.0, CFG, 60.0)
    assert len(quotes) == 2
    tokens = {q.token_id for q in quotes}
    assert tokens == {"yes_tok", "no_tok"}


def test_compute_quotes_exposes_reproducible_pricing_snapshot():
    """Recovery-quote logs need the inputs required to reproduce its price."""
    pricing = {}
    quotes = compute_quotes(
        _market(), _book(bid=0.48, ask=0.52, bid_sz=80, ask_sz=120),
        net_yes_exposure_usd=-20.0, cfg=CFG, max_inventory_usd=60.0,
        fade_yes=0.01, fade_no=0.02, flow_imbalance=-0.5,
        markout_avg=-0.003, size_factor=1.25, pricing=pricing,
    )

    assert quotes
    assert pricing == pytest.approx({
        "yes_bid": 0.48,
        "yes_bid_size": 80.0,
        "yes_ask": 0.52,
        "yes_ask_size": 120.0,
        "yes_microprice": 0.496,
        "flow_imbalance": -0.5,
        "flow_drift": -0.005,
        "fair": 0.491,
        "base_offset": 0.0105,
        "adaptive_offset": 0.003,
        "offset": 0.0135,
        "net_yes_exposure_usd": -20.0,
        "max_inventory_usd": 60.0,
        "skew": -0.0027,
        "fade_yes": 0.01,
        "fade_no": 0.02,
        "yes_bid_quote": 0.47,
        "no_bid_quote": 0.47,
    })


def test_compute_quotes_skew_drops_side_at_cap():
    m = _market()
    quotes = compute_quotes(m, _book(), 60.0, CFG, 60.0)
    assert len(quotes) == 1
    assert quotes[0].token_id == "no_tok"


def test_compute_quotes_size_never_below_min_incentive_size():
    """A low size_factor must not shrink orders below min_size — they would
    score zero rewards."""
    m = _market(min_size=10.0)
    quotes = compute_quotes(m, _book(), 0.0, CFG, 60.0, size_factor=0.5)
    assert quotes
    assert all(q.size >= m.min_size for q in quotes)


def test_compute_quotes_fee_market_does_not_widen():
    """Makers are never charged fees on Polymarket, so the market fee rate must
    not affect resting quotes — widening would only forfeit reward-band rewards.
    A fee market should produce the same quotes as a fee-free one."""
    free = compute_quotes(_market(), _book(), 0.0, CFG, 60.0)
    fee_low = compute_quotes(_market(fee_bps=200), _book(), 0.0, CFG, 60.0)
    fee_high = compute_quotes(_market(fee_bps=1000), _book(), 0.0, CFG, 60.0)
    assert free and fee_low and fee_high
    for feed in (fee_low, fee_high):
        for f, q in zip(free, feed):
            assert f.token_id == q.token_id
            assert f.price == q.price
            assert f.size == q.size


def test_adaptive_offset_widens_on_negative_markout():
    adj = adaptive_offset(-0.02, CFG)
    assert adj > 0


def test_adaptive_offset_tightens_on_positive_markout():
    adj = adaptive_offset(0.02, CFG)
    assert adj < 0


def test_reconcile_quotes_keeps_close_quotes():
    cur = [Quote("yes_tok", 0.47, 10)]
    des = [Quote("yes_tok", 0.4705, 10)]
    final = reconcile_quotes(cur, des, move_cents=0.4)
    assert final[0] is cur[0]


def test_reconcile_quotes_replaces_distant_quotes():
    cur = [Quote("yes_tok", 0.47, 10)]
    des = [Quote("yes_tok", 0.45, 10)]
    final = reconcile_quotes(cur, des, move_cents=0.4)
    assert final[0] is des[0]
