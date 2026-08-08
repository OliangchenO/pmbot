"""Pure, executable-price tests for price-aware inventory exits."""

from pmbot.books import Book
from pmbot.gamma import Market
from pmbot.exit_strategy import complement_profit_cap, evaluate_exit


def _market(*, fee_bps: float = 0.0) -> Market:
    return Market(
        question="Will it rain tomorrow?", condition_id="cid-1",
        yes_token="yes", no_token="no", min_size=5.0,
        max_spread_cents=3.0, daily_pool=50.0, liquidity=1_000.0,
        volume_24h=500.0, tick=0.01, end_date=None, neg_risk=False,
        fee_bps=fee_bps,
    )


def _book(token: str, *, bids: dict[float, float] | None = None,
          asks: dict[float, float] | None = None, minimum: float = 5.0) -> Book:
    book = Book(token)
    book.bids = bids or {}
    book.asks = asks or {}
    book.min_order_size = minimum
    return book


def _cfg() -> dict:
    return {
        "min_shares": 5.0,
        "same_side_target": 0.01,
        "complement_target": 0.01,
        "min_exit_price": 0.05,
        "max_exit_price": 0.95,
        "progressive": {"enabled": False},
    }


def test_same_side_requires_executable_bid_at_net_target():
    decision = evaluate_exit(
        _market(), 10.0, 0.55, 1.0,
        _book("yes", bids={0.55: 10.0}, asks={0.57: 10.0}),
        _book("no"),
        5.0, _cfg(),
    )

    assert decision.action == "none"
    assert decision.reason == "same_side_bid_below_net_target"


def test_complement_cap_rejects_ask_even_when_mid_looks_profitable():
    market = _market()
    decision = evaluate_exit(
        market, 10.0, 0.55, 1.0,
        _book("yes"),
        _book("no", bids={0.42: 10.0}, asks={0.45: 10.0}),
        5.0, _cfg(),
    )

    assert complement_profit_cap(market, 0.55, 0.01) == 0.44
    assert decision.action == "none"
    assert decision.reason == "complement_ask_above_profit_cap"


def test_same_side_uses_profit_floor_not_bid_minus_tick():
    decision = evaluate_exit(
        _market(), 10.0, 0.55, 1.0,
        _book("yes", bids={0.57: 10.0}, asks={0.58: 10.0}),
        _book("no", bids={0.41: 10.0}, asks={0.45: 10.0}),
        5.0, _cfg(),
    )

    assert decision.action == "same_side_sell"
    assert decision.limit_price == 0.56
    assert decision.expected_net_pnl_per_share == 0.02


def test_complement_uses_fee_inclusive_profit_cap_and_displayed_depth():
    decision = evaluate_exit(
        _market(), 10.0, 0.55, 1.0,
        _book("yes"),
        _book("no", bids={0.42: 10.0}, asks={0.43: 10.0}),
        5.0, _cfg(),
    )

    assert decision.action == "complement_buy"
    assert decision.limit_price == 0.44
    assert decision.expected_net_pnl_per_share == 0.02


def test_prefer_same_side_when_both_actions_have_same_expected_pnl():
    decision = evaluate_exit(
        _market(), 10.0, 0.55, 1.0,
        _book("yes", bids={0.56: 10.0}, asks={0.57: 10.0}),
        _book("no", bids={0.43: 10.0}, asks={0.44: 10.0}),
        5.0, _cfg(),
    )

    assert decision.action == "same_side_sell"
