"""Tests for pure recovery decision module."""

import math
from unittest.mock import MagicMock

import pytest

from pmbot.recovery import (
    RecoveryEpisode,
    RecoveryQuote,
    choose_recovery_action,
)


# ── helpers ──


def _dummy_market():
    """Return a minimal Market-like object for recovery decision tests."""
    m = MagicMock()
    m.fee_bps = 200  # 2%
    m.fee_exponent = 0.5
    m.tick = 0.01
    m.condition_id = "test-cid"
    return m


def _taker_fee(price: float, market=None) -> float:
    """Compute taker fee per share for a given fill price."""
    if market is None:
        market = _dummy_market()
    fee_rate = market.fee_bps / 10_000.0
    return fee_rate * (price * (1.0 - price)) ** market.fee_exponent


# ── Task 1: choose_recovery_action pure function ──


def test_choose_buy_complement_when_cheaper_than_sell():
    """When buy_complement_loss < sell_original_loss and within budget,
    choose buy_complement."""
    market = _dummy_market()
    basis = 0.45  # held YES at 0.45
    # complement_ask=0.58: buy_loss = max(0, 0.45+0.58+fee-1)*10 ≈ 0.40
    # original_bid=0.30:  sell_loss = max(0, 0.45-0.30+fee)*10 ≈ 1.60
    complement_ask = 0.58
    original_bid = 0.30
    fee_complement = _taker_fee(complement_ask, market)
    fee_original = _taker_fee(original_bid, market)

    result = choose_recovery_action(
        market=market,
        unpaired=10.0,  # positive = holding YES
        basis=basis,
        complement_ask=complement_ask,
        original_bid=original_bid,
        elapsed_secs=200.0,
        max_loss_usd=3.0,
    )

    assert result.path == "buy_complement"
    buy_loss = max(0.0, basis + complement_ask + fee_complement - 1.0) * 10.0
    sell_loss = max(0.0, basis - original_bid + fee_original) * 10.0
    assert buy_loss > 0
    assert buy_loss < sell_loss
    assert result.token_id is not None
    assert result.price == pytest.approx(complement_ask)
    assert result.size == pytest.approx(10.0)
    assert result.expected_loss_usd is not None
    assert result.expected_loss_usd <= 3.0


def test_choose_sell_original_when_cheaper_than_buy_complement():
    """When sell_original has lower loss than buy_complement, choose sell."""
    market = _dummy_market()
    basis = 0.50
    # complement_ask=0.55: buy_loss = max(0, 0.50+0.55+fee-1)*10 ≈ large
    # original_bid=0.48: sell_loss = max(0, 0.50-0.48+fee)*10 ≈ tiny
    complement_ask = 0.55
    original_bid = 0.48
    fee_complement = _taker_fee(complement_ask, market)
    fee_original = _taker_fee(original_bid, market)

    result = choose_recovery_action(
        market=market,
        unpaired=10.0,
        basis=basis,
        complement_ask=complement_ask,
        original_bid=original_bid,
        elapsed_secs=200.0,
        max_loss_usd=3.0,
    )

    buy_loss = max(0.0, basis + complement_ask + fee_complement - 1.0) * 10.0
    sell_loss = max(0.0, basis - original_bid + fee_original) * 10.0

    assert result.path == "sell_original"
    assert buy_loss > sell_loss
    assert result.token_id is not None
    assert result.price == pytest.approx(original_bid)
    assert result.size == pytest.approx(10.0)
    assert result.expected_loss_usd == pytest.approx(sell_loss)


def test_returns_manual_hold_when_all_paths_exceed_budget():
    """When both buy_complement and sell_original exceed max_loss_usd,
    return manual_hold."""
    market = _dummy_market()
    basis = 0.40
    complement_ask = 0.65  # loss = (0.40 + 0.65 + fee - 1) * shares
    original_bid = 0.35  # loss = (0.40 - 0.35 + fee) * shares
    fee = _taker_fee(complement_ask, market)

    result = choose_recovery_action(
        market=market,
        unpaired=50.0,
        basis=basis,
        complement_ask=complement_ask,
        original_bid=original_bid,
        elapsed_secs=200.0,
        max_loss_usd=0.50,  # very tight budget
    )

    buy_loss = max(0.0, basis + complement_ask + fee - 1.0) * 50.0
    assert buy_loss > 0.50
    assert result.path == "manual_hold"
    assert result.token_id is None
    assert result.price is None
    assert result.size == pytest.approx(abs(50.0))
    assert result.expected_loss_usd is None


def test_returns_manual_hold_when_basis_missing():
    """Without cost basis, decision is impossible — return manual_hold."""
    market = _dummy_market()

    result = choose_recovery_action(
        market=market,
        unpaired=10.0,
        basis=None,
        complement_ask=0.55,
        original_bid=0.45,
        elapsed_secs=200.0,
        max_loss_usd=3.0,
    )

    assert result.path == "manual_hold"
    assert result.reason == "unknown_cost_basis"


def test_returns_manual_hold_when_both_prices_missing():
    """When neither complement_ask nor original_bid is available, manual_hold."""
    market = _dummy_market()

    result = choose_recovery_action(
        market=market,
        unpaired=10.0,
        basis=0.45,
        complement_ask=None,
        original_bid=None,
        elapsed_secs=200.0,
        max_loss_usd=3.0,
    )

    assert result.path == "manual_hold"
    assert result.reason == "book_unavailable"


def test_returns_wait_when_below_min_shares():
    """When unpaired is below MIN_TAKER_SHARES, return wait."""
    market = _dummy_market()

    result = choose_recovery_action(
        market=market,
        unpaired=3.0,  # below 5.0 minimum
        basis=0.45,
        complement_ask=0.55,
        original_bid=0.45,
        elapsed_secs=200.0,
        max_loss_usd=3.0,
    )

    assert result.path == "wait"
    assert result.reason == "below_min_shares"


def test_buy_complement_when_sell_original_unavailable():
    """When only complement_ask is available, choose buy_complement if within budget."""
    market = _dummy_market()
    basis = 0.45
    complement_ask = 0.55
    fee = _taker_fee(complement_ask, market)
    buy_loss = max(0.0, basis + complement_ask + fee - 1.0) * 10.0

    result = choose_recovery_action(
        market=market,
        unpaired=10.0,
        basis=basis,
        complement_ask=complement_ask,
        original_bid=None,  # only complement available
        elapsed_secs=200.0,
        max_loss_usd=3.0,
    )

    assert result.path == "buy_complement"
    assert result.expected_loss_usd == pytest.approx(buy_loss)


def test_sell_original_when_complement_unavailable():
    """When only original_bid is available, choose sell_original if within budget."""
    market = _dummy_market()
    basis = 0.45
    original_bid = 0.40
    fee = _taker_fee(original_bid, market)
    sell_loss = max(0.0, basis - original_bid + fee) * 10.0

    result = choose_recovery_action(
        market=market,
        unpaired=10.0,
        basis=basis,
        complement_ask=None,
        original_bid=original_bid,
        elapsed_secs=200.0,
        max_loss_usd=3.0,
    )

    assert result.path == "sell_original"
    assert result.expected_loss_usd == pytest.approx(sell_loss)


def test_negative_unpaired_means_holding_no():
    """Negative unpaired means holding NO, complement is YES token."""
    market = _dummy_market()
    market.yes_token = "yes-token"
    market.no_token = "no-token"
    basis = 0.55  # held NO at 0.55
    complement_ask = 0.40  # YES ask (complement for NO)
    original_bid = 0.45  # NO bid (exit)
    fee = _taker_fee(complement_ask, market)

    result = choose_recovery_action(
        market=market,
        unpaired=-10.0,  # negative = holding NO
        basis=basis,
        complement_ask=complement_ask,
        original_bid=original_bid,
        elapsed_secs=200.0,
        max_loss_usd=3.0,
    )

    assert result.path == "buy_complement"
    assert result.token_id == "yes-token"  # complement of NO
    buy_loss = max(0.0, basis + complement_ask + fee - 1.0) * 10.0
    assert result.expected_loss_usd == pytest.approx(buy_loss)


def test_break_even_path_chosen_when_cost_is_zero():
    """When basis + complement_ask + fee <= 1.0, loss is 0, choose buy_complement."""
    market = _dummy_market()
    basis = 0.40
    complement_ask = 0.58  # 0.40 + 0.58 + fee <= 1.0
    fee = _taker_fee(complement_ask, market)

    result = choose_recovery_action(
        market=market,
        unpaired=10.0,
        basis=basis,
        complement_ask=complement_ask,
        original_bid=0.35,
        elapsed_secs=200.0,
        max_loss_usd=3.0,
    )

    buy_loss = max(0.0, basis + complement_ask + fee - 1.0) * 10.0
    assert buy_loss == 0.0
    assert result.path == "buy_complement"
    assert result.expected_loss_usd == 0.0


def test_expected_loss_never_negative():
    """Loss calculations must floor at zero — profit is never counted as negative loss."""
    market = _dummy_market()
    basis = 0.80
    complement_ask = 0.10  # basis + ask = 0.90 < 1.0 — profit!
    fee = _taker_fee(complement_ask, market)

    result = choose_recovery_action(
        market=market,
        unpaired=10.0,
        basis=basis,
        complement_ask=complement_ask,
        original_bid=0.85,
        elapsed_secs=200.0,
        max_loss_usd=3.0,
    )

    assert result.expected_loss_usd == 0.0


def test_token_id_correct_for_buy_complement_positive_unpaired():
    """Holding YES (positive unpaired) means complement is NO token.
    Use prices where buy_complement is cheaper than sell_original."""
    market = _dummy_market()
    market.yes_token = "yes-t"
    market.no_token = "no-t"

    # buy_complement: basis=0.45, complement_ask=0.58 → some loss
    # sell_original: basis=0.45, original_bid=None → unavailable
    result = choose_recovery_action(
        market=market,
        unpaired=10.0,
        basis=0.45,
        complement_ask=0.58,
        original_bid=None,  # force buy_complement
        elapsed_secs=200.0,
        max_loss_usd=3.0,
    )

    assert result.path == "buy_complement"
    assert result.token_id == "no-t"


def test_token_id_correct_for_sell_original_positive_unpaired():
    """Holding YES (positive unpaired) means sell YES on original side."""
    market = _dummy_market()
    market.yes_token = "yes-t"
    market.no_token = "no-t"

    result = choose_recovery_action(
        market=market,
        unpaired=10.0,
        basis=0.45,
        complement_ask=None,
        original_bid=0.40,
        elapsed_secs=200.0,
        max_loss_usd=3.0,
    )

    assert result.path == "sell_original"
    assert result.token_id == "yes-t"


# ── RecoveryEpisode dataclass ──


def test_recovery_episode_defaults():
    """RecoveryEpisode fields default correctly."""
    ep = RecoveryEpisode(
        cid="test-cid",
        started_ts=1000.0,
        initial_unpaired=10.0,
        peak_abs_exposure_usd=5.0,
        stage="passive",
    )
    assert ep.cid == "test-cid"
    assert ep.started_ts == 1000.0
    assert ep.initial_unpaired == 10.0
    assert ep.peak_abs_exposure_usd == 5.0
    assert ep.stage == "passive"


# ── float tolerance ──


def test_max_loss_boundary_exactly_at_budget():
    """When expected_loss_usd == max_loss_usd exactly, action is still allowed."""
    market = _dummy_market()
    basis = 0.45
    complement_ask = 0.58
    fee = _taker_fee(complement_ask, market)
    buy_loss = max(0.0, basis + complement_ask + fee - 1.0) * 10.0
    assert buy_loss > 0

    result = choose_recovery_action(
        market=market,
        unpaired=10.0,
        basis=basis,
        complement_ask=complement_ask,
        original_bid=None,  # only complement available
        elapsed_secs=200.0,
        max_loss_usd=buy_loss,  # exactly at budget
    )

    assert result.path == "buy_complement"
    assert result.expected_loss_usd is not None
    assert result.expected_loss_usd <= buy_loss + 1e-9


def test_max_loss_exceeded_by_epsilon():
    """When expected_loss_usd exceeds max_loss_usd by any amount, manual_hold."""
    market = _dummy_market()
    basis = 0.30
    complement_ask = 0.75  # basis + ask + fee > 1.0
    fee = _taker_fee(complement_ask, market)
    buy_loss = max(0.0, basis + complement_ask + fee - 1.0) * 20.0
    assert buy_loss > 0

    result = choose_recovery_action(
        market=market,
        unpaired=20.0,
        basis=basis,
        complement_ask=complement_ask,
        original_bid=None,  # only complement available
        elapsed_secs=200.0,
        max_loss_usd=buy_loss - 0.001,  # slightly under budget
    )

    assert result.path == "manual_hold"
    assert result.reason == "exceeds_loss_budget"


def test_fee_calculation_uses_market_fee_parameters():
    """Fee must be computed from the market's fee_bps and fee_exponent, not hardcoded."""
    market = _dummy_market()
    market.fee_bps = 100  # 1%
    market.fee_exponent = 1.0  # Linear fee

    result = choose_recovery_action(
        market=market,
        unpaired=10.0,
        basis=0.45,
        complement_ask=0.60,  # high enough to incur loss
        original_bid=None,
        elapsed_secs=200.0,
        max_loss_usd=3.0,
    )

    # fee = 0.01 * (0.60 * 0.40) = 0.01*0.24 = 0.0024
    expected_fee = 0.01 * (0.60 * 0.40)
    buy_loss = max(0.0, 0.45 + 0.60 + expected_fee - 1.0) * 10.0
    assert result.expected_loss_usd == pytest.approx(buy_loss)


# ── P1-5: force_execute near-resolution override ──


def test_force_execute_bypasses_budget_check():
    """When force_execute=True, the budget check is skipped even when
    the cheapest path exceeds max_loss_usd."""
    market = _dummy_market()
    basis = 0.40
    complement_ask = 0.65  # loss ≈ (0.40+0.65+fee-1)*50 = large
    fee = _taker_fee(complement_ask, market)
    buy_loss = max(0.0, basis + complement_ask + fee - 1.0) * 50.0
    assert buy_loss > 0.50  # loss exceeds the tight budget below

    result = choose_recovery_action(
        market=market,
        unpaired=50.0,
        basis=basis,
        complement_ask=complement_ask,
        original_bid=None,  # force buy_complement
        elapsed_secs=200.0,
        max_loss_usd=0.50,
        force_execute=True,
    )

    assert result.path == "buy_complement"
    assert result.expected_loss_usd is not None
    assert result.expected_loss_usd > 0.50


def test_force_execute_does_not_affect_normal_budget():
    """When force_execute=False (default), budget check works normally."""
    market = _dummy_market()
    basis = 0.40
    complement_ask = 0.65
    fee = _taker_fee(complement_ask, market)
    buy_loss = max(0.0, basis + complement_ask + fee - 1.0) * 50.0
    assert buy_loss > 0.50

    result = choose_recovery_action(
        market=market,
        unpaired=50.0,
        basis=basis,
        complement_ask=complement_ask,
        original_bid=0.35,
        elapsed_secs=200.0,
        max_loss_usd=0.50,
        force_execute=False,
    )

    assert result.path == "manual_hold"
    assert result.reason == "exceeds_loss_budget"


def test_force_execute_still_respects_known_guards():
    """force_execute skips budget but still honours basis availability
    and min-share guards."""
    market = _dummy_market()

    # Basis missing → manual_hold even with force_execute
    result = choose_recovery_action(
        market=market,
        unpaired=10.0,
        basis=None,
        complement_ask=0.55,
        original_bid=0.45,
        elapsed_secs=200.0,
        max_loss_usd=3.0,
        force_execute=True,
    )
    assert result.path == "manual_hold"
    assert result.reason == "unknown_cost_basis"

    # Below min shares → wait even with force_execute
    result = choose_recovery_action(
        market=market,
        unpaired=3.0,
        basis=0.45,
        complement_ask=0.55,
        original_bid=0.45,
        elapsed_secs=200.0,
        max_loss_usd=3.0,
        force_execute=True,
    )
    assert result.path == "wait"
    assert result.reason == "below_min_shares"
