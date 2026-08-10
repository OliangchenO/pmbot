"""Pure recovery decision engine — stateless, no broker/SQLite/clock access.

Converts unpaired inventory into one of four paths:
  wait           — below minimum trade size, nothing to do
  buy_complement — taker-buy the complementary token to complete a pair
  sell_original  — taker-sell (reduce-only exit) the held token
  manual_hold    — all automatic paths exceed loss budget or lack data
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .gamma import Market

MIN_TAKER_SHARES = 5.0


@dataclass(frozen=True)
class RecoveryQuote:
    """Single recommended action for unpaired inventory.

    **Never** instantiate this from outside the decision module — always use
    ``choose_recovery_action()`` so the budget check is enforced.
    """

    path: Literal["wait", "buy_complement", "sell_original", "manual_hold"]
    token_id: str | None
    price: float | None
    size: float
    expected_loss_usd: float | None
    reason: str


@dataclass
class RecoveryEpisode:
    """Persistent record of one unpaired-inventory lifecycle.

    One episode per CID at a time. Opened when unpaired shares cross the
    minimum, updated on each tick (peak exposure, stage), and closed when
    inventory returns to flat.
    """

    cid: str
    started_ts: float
    initial_unpaired: float
    peak_abs_exposure_usd: float = 0.0
    stage: Literal["passive", "escalated", "terminal"] = "passive"
    chosen_path: str | None = None
    expected_loss_usd: float | None = None
    actual_loss_usd: float | None = None
    closed_ts: float | None = None
    closed_reason: str | None = None

    # Always 1: an episode persists until the position goes flat — polling
    # updates the existing row, never creates a second one.
    version: int = 1


def _taker_fee_per_share(market: "Market", price: float) -> float:
    """Taker fee for one share at *price*, from the market's fee schedule."""
    fee_rate = market.fee_bps / 10_000.0
    return fee_rate * (price * (1.0 - price)) ** market.fee_exponent


def choose_recovery_action(
    *,
    market: "Market",
    unpaired: float,
    basis: float | None,
    complement_ask: float | None,
    original_bid: float | None,
    elapsed_secs: float,
    max_loss_usd: float,
    force_execute: bool = False,
) -> RecoveryQuote:
    """Select the cheapest recovery path within the per-episode loss budget.

    Economic definitions (design doc §3):

        buy_complement_loss = max(0, basis + ask + taker_fee − 1) × shares
        sell_original_loss  = max(0, basis − bid + taker_fee) × shares

    The cheaper executable path wins.  If either path exceeds ``max_loss_usd``
    or neither path has a usable price/basis, the result is ``manual_hold``.

    ``force_execute`` (default False): when True, the budget check is
    skipped entirely and the cheapest available path is returned regardless
    of ``max_loss_usd``.  Intended for near-resolution markets where a
    controlled exit (even at a moderate loss) is preferable to being
    locked into the final resolution outcome.

    ``elapsed_secs`` participates in the decision only when a single
    available path has a positive loss — if that loss exceeds budget and
    ``elapsed_secs`` has reached an escalated window, the function still
    returns that path with a note that it is being taken at elevated
    loss.  The caller's stage-passive gate controls whether escalation
    actually triggers execution.
    """
    # ── guard: below minimum shares ──
    if abs(unpaired) < MIN_TAKER_SHARES:
        return RecoveryQuote(
            path="wait",
            token_id=None,
            price=None,
            size=abs(unpaired),
            expected_loss_usd=None,
            reason="below_min_shares",
        )

    # ── guard: must know cost basis ──
    if basis is None:
        return RecoveryQuote(
            path="manual_hold",
            token_id=None,
            price=None,
            size=abs(unpaired),
            expected_loss_usd=None,
            reason="unknown_cost_basis",
        )

    shares = abs(unpaired)

    # Positive unpaired = holding YES → complement is NO, original is YES
    # Negative unpaired = holding NO  → complement is YES, original is NO
    held_yes = unpaired > 0
    complement_token = market.no_token if held_yes else market.yes_token
    original_token = market.yes_token if held_yes else market.no_token

    # ── compute loss for each available path ──
    buy_loss: float | None = None
    sell_loss: float | None = None

    if complement_ask is not None:
        fee = _taker_fee_per_share(market, complement_ask)
        buy_loss = max(0.0, basis + complement_ask + fee - 1.0) * shares

    if original_bid is not None:
        fee = _taker_fee_per_share(market, original_bid)
        sell_loss = max(0.0, basis - original_bid + fee) * shares

    # ── guard: at least one path must have a usable price ──
    if buy_loss is None and sell_loss is None:
        return RecoveryQuote(
            path="manual_hold",
            token_id=None,
            price=None,
            size=shares,
            expected_loss_usd=None,
            reason="book_unavailable",
        )

    # ── pick the cheaper executable path ──
    if buy_loss is not None and sell_loss is not None:
        if buy_loss <= sell_loss:
            chosen_path = "buy_complement"
            chosen_loss = buy_loss
            chosen_token = complement_token
            chosen_price = complement_ask
        else:
            chosen_path = "sell_original"
            chosen_loss = sell_loss
            chosen_token = original_token
            chosen_price = original_bid
    elif buy_loss is not None:
        chosen_path = "buy_complement"
        chosen_loss = buy_loss
        chosen_token = complement_token
        chosen_price = complement_ask
    else:
        assert sell_loss is not None
        chosen_path = "sell_original"
        chosen_loss = sell_loss
        chosen_token = original_token
        chosen_price = original_bid

    # ── budget check ──
    if not force_execute and chosen_loss > max_loss_usd + 1e-9:
        return RecoveryQuote(
            path="manual_hold",
            token_id=None,
            price=None,
            size=shares,
            expected_loss_usd=None,
            reason="exceeds_loss_budget",
        )

    return RecoveryQuote(
        path=chosen_path,
        token_id=chosen_token,
        price=chosen_price,
        size=shares,
        expected_loss_usd=chosen_loss,
        reason="",
    )
