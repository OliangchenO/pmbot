"""Tests for reward_exit pure computation module."""

import math
from unittest.mock import MagicMock

import pytest

from pmbot.reward_exit import (
    BatchStatus,
    CostSplit,
    RewardExitBatch,
    SellTargetResult,
    TakeFill,
    ceil_to_tick,
    compute_paired_loss,
    compute_sell_target,
    create_batch,
    remaining_exit,
    remaining_take,
    split_take_fills_fifo,
    transition_to_closed,
    transition_to_manual_hold,
    transition_to_seal_pending,
    valid_transition,
)


# ── helpers ──


def _dummy_market():
    """Return a minimal Market-like object."""
    m = MagicMock()
    m.fee_bps = 200  # 2%
    m.fee_exponent = 0.5
    m.tick = 0.01
    m.condition_id = "test-cid"
    return m


def _make_take_fill(fill_id: str, ts: float, price: float, size: float,
                    fee_bps: int = 200, fee_exponent: float = 0.5) -> TakeFill:
    fee_rate = fee_bps / 10_000.0
    fee = fee_rate * (price * (1.0 - price)) ** fee_exponent * size
    return TakeFill(
        fill_id=fill_id, ts=ts, price=price, size=size,
        notional=price * size, fee_usd=fee,
    )


# ── 11.1.1: create_batch ──


def test_create_batch_yes_fill_generates_no_take():
    """BUY YES 20 → complement is NO, take_target = 40."""
    b = create_batch(
        batch_id="batch-1", cid="cid-1",
        origin_order_id="ord-1", origin_fill_id="fill-1",
        origin_token_id="yes-token", complement_token_id="no-token",
        origin_size=20, origin_price=0.51, origin_fee_usd=0.0,
        created_ts=100.0,
    )
    assert b.batch_id == "batch-1"
    assert b.cid == "cid-1"
    assert b.origin_token_id == "yes-token"
    assert b.complement_token_id == "no-token"
    assert b.origin_size == 20.0
    assert b.origin_notional_usd == pytest.approx(10.2)  # 20 * 0.51
    assert b.take_target_size == 40.0  # 2q
    assert b.status == "TAKE_PENDING"
    assert b.take_filled_size == 0.0


def test_create_batch_no_fill_generates_yes_take():
    """BUY NO 15 → complement is YES, take_target = 30."""
    b = create_batch(
        batch_id="batch-2", cid="cid-2",
        origin_order_id="ord-2", origin_fill_id="fill-2",
        origin_token_id="no-token", complement_token_id="yes-token",
        origin_size=15, origin_price=0.40, origin_fee_usd=0.05,
        created_ts=200.0,
    )
    assert b.complement_token_id == "yes-token"
    assert b.take_target_size == 30.0
    assert b.origin_fee_usd == 0.05


# ── 11.1.2: FIFO cost splitting ──


def test_split_simple_two_fills():
    """Two fills of exactly q each → first goes to paired, second to exit."""
    fills = [
        _make_take_fill("f1", 1.0, 0.55, 10.0),
        _make_take_fill("f2", 2.0, 0.56, 10.0),
    ]
    split = split_take_fills_fifo(fills, origin_size=10.0)
    assert split.paired_notional == pytest.approx(5.5)   # 10 * 0.55
    assert split.paired_fee > 0
    assert split.exit_notional == pytest.approx(5.6)     # 10 * 0.56
    assert split.exit_fee > 0


def test_split_cross_boundary_fill():
    """One fill of 15 when q=10 → first 10 to paired, last 5 to exit."""
    fills = [
        _make_take_fill("f1", 1.0, 0.55, 15.0),
    ]
    split = split_take_fills_fifo(fills, origin_size=10.0)
    assert split.paired_notional == pytest.approx(0.55 * 10.0)  # 10/15 of 5.5
    assert split.exit_notional == pytest.approx(0.55 * 5.0)     # 5/15
    # Total notional should sum to the full fill notional
    assert split.paired_notional + split.exit_notional == pytest.approx(0.55 * 15.0)


def test_split_multiple_fills_straddle_boundary():
    """Multiple fills where one crosses the boundary.
    q=10, fills: [8 shares, 12 shares] → first gulp gets 8 paired, second gets 2 paired + 10 exit.
    """
    fills = [
        _make_take_fill("f1", 1.0, 0.50, 8.0),
        _make_take_fill("f2", 2.0, 0.52, 12.0),
    ]
    split = split_take_fills_fifo(fills, origin_size=10.0)
    # Paired: all 8 from f1 + 2 from f2 = 10
    expected_paired = 0.50 * 8.0 + 0.52 * 2.0
    # Exit: 10 from f2
    expected_exit = 0.52 * 10.0
    assert split.paired_notional == pytest.approx(expected_paired, abs=1e-6)
    assert split.exit_notional == pytest.approx(expected_exit, abs=1e-6)


def test_split_exact_fills():
    """Three fills that exactly cover 2q."""
    fills = [
        _make_take_fill("f1", 1.0, 0.55, 5.0),
        _make_take_fill("f2", 2.0, 0.56, 5.0),
        _make_take_fill("f3", 3.0, 0.57, 10.0),
    ]
    split = split_take_fills_fifo(fills, origin_size=10.0)
    # Paired: 5@0.55 + 5@0.56 = 5.55
    # Exit:   10@0.57 = 5.70
    assert split.paired_notional == pytest.approx(5.55)
    assert split.exit_notional == pytest.approx(5.70)


def test_split_empty_fills():
    """No fills produces zero split."""
    split = split_take_fills_fifo([], origin_size=10.0)
    assert split.paired_notional == 0.0
    assert split.exit_notional == 0.0
    assert split.paired_fee == 0.0
    assert split.exit_fee == 0.0


# ── 11.1.4: paired loss ──


def test_paired_loss_with_profit_clamps_to_zero():
    """When total cost < q, loss is 0 (profit not recognized)."""
    loss = compute_paired_loss(
        origin_notional_usd=4.0,  # 10 shares @ avg 0.40 each
        origin_fee_usd=0.0,
        paired_complement_notional=5.0,  # 10 shares @ avg 0.50 each
        paired_complement_fee=0.0,
        origin_size=10.0,
    )
    # paired_cost = 4.0 + 5.0 = 9.0, q=10 → loss = max(0, 9-10) = 0
    assert loss == 0.0


def test_paired_loss_positive():
    """When total cost > q, loss = cost - q."""
    loss = compute_paired_loss(
        origin_notional_usd=5.5,  # BUY YES 10 @ 0.55
        origin_fee_usd=0.1,
        paired_complement_notional=5.8,  # BUY NO 10 @ 0.58
        paired_complement_fee=0.1,
        origin_size=10.0,
    )
    # paired_cost = 5.5 + 0.1 + 5.8 + 0.1 = 11.5
    # loss = 11.5 - 10 = 1.5
    assert loss == pytest.approx(1.5)


def test_paired_loss_exact_breakeven():
    """When total cost = q, loss = 0."""
    loss = compute_paired_loss(
        origin_notional_usd=5.0,
        origin_fee_usd=0.0,
        paired_complement_notional=5.0,
        paired_complement_fee=0.0,
        origin_size=10.0,
    )
    assert loss == 0.0


# ── 11.1.5: SELL target price ──


def test_sell_target_basic():
    """Basic target price calculation."""
    m = _dummy_market()
    result = compute_sell_target(
        market=m,
        exit_size=10.0,
        exit_cost_notional=5.0,   # bought exit shares at avg 0.50
        exit_cost_fee=0.05,
        paired_loss_usd=1.0,
        best_bid=0.50,
    )
    # required_net = 5.0 + 0.05 + 1.0 = 6.05
    # required per share = 0.605
    # With fee ~0.01, target ≈ 0.615, rounded up to tick 0.01 → 0.62
    # But best_bid + tick = 0.50 + 0.01 = 0.51, so target stays at ceil
    assert result.price is not None
    assert result.required_net_usd == pytest.approx(6.05)
    assert result.price >= 0.51  # best_bid + tick floor


def test_sell_target_above_max_price_triggers_manual():
    """When required price exceeds market max, return manual hold."""
    m = _dummy_market()
    result = compute_sell_target(
        market=m,
        exit_size=10.0,
        exit_cost_notional=9.0,    # cost 0.90 per share
        exit_cost_fee=0.0,
        paired_loss_usd=2.0,       # need extra $2
        best_bid=0.95,
        max_price=0.99,
    )
    # required per share = (9+0+2)/10 = 1.10 — above max_price 0.99
    assert result.price is None
    assert "above_max_price" in result.reason


def test_sell_target_respects_best_bid_floor():
    """Target price must be >= best_bid + tick."""
    m = _dummy_market()
    result = compute_sell_target(
        market=m,
        exit_size=10.0,
        exit_cost_notional=4.5,    # cost 0.45 per share
        exit_cost_fee=0.0,
        paired_loss_usd=0.0,       # no loss
        best_bid=0.60,             # tight book
    )
    # required per share ≈ 0.45, but best_bid + tick = 0.61
    assert result.price is not None
    assert result.price >= 0.61  # must exceed best_bid + tick


def test_sell_target_with_fee():
    """Fee eats into net proceeds; target must cover it."""
    m = _dummy_market()
    m.fee_bps = 200  # 2%
    m.fee_exponent = 1.0  # linear fee rate
    result = compute_sell_target(
        market=m,
        exit_size=10.0,
        exit_cost_notional=5.0,
        exit_cost_fee=0.05,
        paired_loss_usd=1.0,
        best_bid=0.50,
    )
    assert result.price is not None
    assert result.price > 0.60  # raw required per share is 0.605, plus fee
    # Net at target should cover required
    fee = result.estimated_fee_per_share
    net_per_share = result.price - fee
    required_per_share = result.required_net_usd / 10.0
    assert net_per_share >= required_per_share - 1e-9


def test_zero_exit_size_returns_manual():
    """Zero exit size is a degenerate input."""
    m = _dummy_market()
    result = compute_sell_target(
        market=m, exit_size=0.0, exit_cost_notional=0.0,
        exit_cost_fee=0.0, paired_loss_usd=0.0, best_bid=0.50,
    )
    assert result.price is None
    assert result.reason == "zero_exit_size"


# ── 11.1.2: ceil_to_tick ──


def test_ceil_to_tick_exact():
    assert ceil_to_tick(0.50, 0.01) == 0.50


def test_ceil_to_tick_rounds_up():
    assert ceil_to_tick(0.505, 0.01) == 0.51


def test_ceil_to_tick_tiny_epsilon():
    assert ceil_to_tick(0.50 + 1e-10, 0.01) == 0.50  # 1e-9 epsilon guard


# ── 11.2: transitions ──


def test_transition_to_seal_pending():
    """Full take → SELL_PENDING with paired loss."""
    fills = [
        _make_take_fill("f1", 1.0, 0.55, 10.0),
        _make_take_fill("f2", 2.0, 0.56, 10.0),
    ]
    b = create_batch(
        batch_id="b1", cid="c1", origin_order_id="o1", origin_fill_id="f0",
        origin_token_id="yes", complement_token_id="no",
        origin_size=10, origin_price=0.51, origin_fee_usd=0.05,
        created_ts=100.0,
    )
    sealed = transition_to_seal_pending(b, take_fills=fills, updated_ts=150.0)
    assert sealed.status == "SELL_PENDING"
    assert sealed.take_filled_size == 20.0
    assert sealed.paired_size == 10.0
    assert sealed.exit_initial_size == 10.0
    assert sealed.paired_loss_usd >= 0  # origin@0.51, complement@0.55/0.56 → loss


def test_transition_to_seal_pending_insufficient_fills():
    """Not enough take fills → ValueError."""
    fills = [_make_take_fill("f1", 1.0, 0.55, 10.0)]  # only 10, need 20
    b = create_batch(
        batch_id="b1", cid="c1", origin_order_id="o1", origin_fill_id="f0",
        origin_token_id="yes", complement_token_id="no",
        origin_size=10, origin_price=0.51,
        created_ts=100.0,
    )
    with pytest.raises(ValueError, match="Not enough take fills"):
        transition_to_seal_pending(b, take_fills=fills, updated_ts=150.0)


def test_transition_to_closed():
    b = create_batch(
        batch_id="b1", cid="c1", origin_order_id="o1", origin_fill_id="f0",
        origin_token_id="yes", complement_token_id="no",
        origin_size=10, origin_price=0.51,
        created_ts=100.0,
    )
    closed = transition_to_closed(b, updated_ts=200.0)
    assert closed.status == "CLOSED"
    assert closed.closed_ts == 200.0


def test_transition_to_manual_hold():
    b = create_batch(
        batch_id="b1", cid="c1", origin_order_id="o1", origin_fill_id="f0",
        origin_token_id="yes", complement_token_id="no",
        origin_size=10, origin_price=0.51,
        created_ts=100.0,
    )
    held = transition_to_manual_hold(b, reason="above_max_price", updated_ts=150.0)
    assert held.status == "MANUAL_HOLD"
    assert held.manual_reason == "above_max_price"


# ── 11.2: valid transitions ──


def test_valid_transitions():
    assert valid_transition("TAKE_PENDING", "SELL_PENDING") is True
    assert valid_transition("TAKE_PENDING", "MANUAL_HOLD") is True
    assert valid_transition("TAKE_PENDING", "CLOSED") is False  # invalid skip
    assert valid_transition("SELL_PENDING", "CLOSED") is True
    assert valid_transition("SELL_PENDING", "MANUAL_HOLD") is True
    assert valid_transition("SELL_PENDING", "TAKE_PENDING") is False  # can't go back
    assert valid_transition("CLOSED", "TAKE_PENDING") is False
    assert valid_transition("MANUAL_HOLD", "TAKE_PENDING") is False


# ── remaining helpers ──


def test_remaining_take():
    assert remaining_take(target_size=40.0, filled_size=15.0) == 25.0
    assert remaining_take(target_size=40.0, filled_size=40.0) == 0.0
    assert remaining_take(target_size=40.0, filled_size=45.0) == 0.0  # should not happen


def test_remaining_exit():
    b = create_batch(
        batch_id="b1", cid="c1", origin_order_id="o1", origin_fill_id="f0",
        origin_token_id="yes", complement_token_id="no",
        origin_size=10, origin_price=0.51,
        created_ts=100.0,
    )
    # Exit initial is 0 until sealed
    assert remaining_exit(b) == 0.0
    # After sealing with fills:
    fills = [
        _make_take_fill("f1", 1.0, 0.55, 10.0),
        _make_take_fill("f2", 2.0, 0.56, 10.0),
    ]
    sealed = transition_to_seal_pending(b, take_fills=fills, updated_ts=150.0)
    assert sealed.exit_initial_size == 10.0
    assert remaining_exit(sealed) == 10.0
