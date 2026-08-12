"""Pure reward-exit batch computation — stateless, no broker/SQLite/clock access.

Converts a normal reward fill into a RewardExitBatch, handles FIFO
cost allocation for take fills, computes paired loss and target SELL price.

Design doc: 2026-08-10-reward-fill-double-take-exit-design.md
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .gamma import Market

# ── Status per design doc §4.1 ──
BatchStatus = Literal[
    "TAKE_PENDING", "TAKE_BLOCKED", "SELL_PENDING", "CLOSED", "MANUAL_HOLD",
]

# ── Valid transitions ──
_VALID_TRANSITIONS: dict[str, frozenset[str]] = {
    "TAKE_PENDING": frozenset({"TAKE_BLOCKED", "SELL_PENDING", "MANUAL_HOLD"}),
    "TAKE_BLOCKED": frozenset({"TAKE_PENDING", "MANUAL_HOLD"}),
    "SELL_PENDING": frozenset({"CLOSED", "MANUAL_HOLD"}),
    "CLOSED": frozenset(),
    "MANUAL_HOLD": frozenset(),
}

MARKET_MAX_PRICE = 1.0  # tick


@dataclass(frozen=True)
class RewardExitBatch:
    """One reward-fill exit lifecycle. Created per normal reward fill.

    Never instantiated directly — use ``create_batch()``.
    """

    batch_id: str
    cid: str
    origin_order_id: str
    origin_fill_id: str
    origin_token_id: str
    complement_token_id: str
    origin_size: float  # q — the original fill quantity
    origin_notional_usd: float
    origin_fee_usd: float
    take_target_size: float  # 固定为 2q
    take_filled_size: float = 0.0
    take_notional_usd: float = 0.0
    take_fee_usd: float = 0.0
    paired_size: float = 0.0  # 固定为 q, take 完成后固化
    paired_loss_usd: float = 0.0
    exit_initial_size: float = 0.0  # 固定为 q
    exit_filled_size: float = 0.0
    exit_notional_usd: float = 0.0
    exit_fee_usd: float = 0.0
    exit_target_price: float = 0.0
    status: BatchStatus = "TAKE_PENDING"
    manual_reason: str = ""
    created_ts: float = 0.0
    updated_ts: float = 0.0
    closed_ts: float | None = None


@dataclass(frozen=True)
class TakeFill:
    """One take fill for FIFO splitting. Must be sorted by (ts, fill_id)."""

    fill_id: str
    ts: float
    price: float
    size: float
    notional: float  # price × size
    fee_usd: float


@dataclass(frozen=True)
class CostSplit:
    """Result of FIFO-splitting take fills into paired and exit portions."""

    paired_notional: float
    paired_fee: float
    exit_notional: float
    exit_fee: float


@dataclass(frozen=True)
class SellTargetResult:
    """Result of computing the maker SELL target price."""

    price: float | None  # None → MANUAL_HOLD
    estimated_fee_per_share: float
    required_net_usd: float
    reason: str  # empty on success; set on MANUAL_HOLD


# ── Batch creation ──


def create_batch(
    *,
    batch_id: str,
    cid: str,
    origin_order_id: str,
    origin_fill_id: str,
    origin_token_id: str,
    complement_token_id: str,
    origin_size: float,
    origin_price: float,
    origin_fee_usd: float = 0.0,
    created_ts: float = 0.0,
) -> RewardExitBatch:
    """Create a new batch from a confirmed normal reward fill.

    The caller MUST have already verified:
    - origin_fill_id is unique (no duplicate batch)
    - origin_size > 0
    - complement_token_id != origin_token_id
    """
    return RewardExitBatch(
        batch_id=batch_id,
        cid=cid,
        origin_order_id=origin_order_id,
        origin_fill_id=origin_fill_id,
        origin_token_id=origin_token_id,
        complement_token_id=complement_token_id,
        origin_size=origin_size,
        origin_notional_usd=origin_price * origin_size,
        origin_fee_usd=origin_fee_usd,
        take_target_size=2.0 * origin_size,
        created_ts=created_ts,
        updated_ts=created_ts,
    )


# ── FIFO cost splitting (design doc §6.3) ──


def split_take_fills_fifo(
    fills: list[TakeFill],
    origin_size: float,
) -> CostSplit:
    """Split confirmed take fills into *paired* (first *q* shares) and
    *exit* (next *q* shares) via stable FIFO.

    Fills MUST be sorted by ``(ts, fill_id)`` ascending.  Any fill whose
    shares fall partly in the paired bucket and partly in the exit bucket
    is split proportionally — its notional and fee are allocated to both
    sides in the same proportion.

    Shares beyond 2q are silently discarded.
    """
    q = origin_size
    paired_notional = 0.0
    paired_fee = 0.0
    exit_notional = 0.0
    exit_fee = 0.0

    remaining_paired = q
    remaining_exit = q

    for fill in fills:
        original_size = fill.size
        if original_size <= 0 or (remaining_paired <= 0 and remaining_exit <= 0):
            continue

        # Each bucket gets a fraction of the *original* fill, so the
        # paired+exit fractions always sum to 1.0 (or ≤1 when the fill
        # extends beyond 2q).

        # Allocate to paired bucket first
        to_paired = min(original_size, max(0.0, remaining_paired))
        if to_paired > 0:
            frac = to_paired / original_size
            paired_notional += fill.notional * frac
            paired_fee += fill.fee_usd * frac
            remaining_paired -= to_paired

        # Allocate remainder to exit bucket
        leftover = original_size - to_paired
        to_exit = min(leftover, max(0.0, remaining_exit))
        if to_exit > 0:
            frac = to_exit / original_size
            exit_notional += fill.notional * frac
            exit_fee += fill.fee_usd * frac
            remaining_exit -= to_exit

    return CostSplit(
        paired_notional=paired_notional,
        paired_fee=paired_fee,
        exit_notional=exit_notional,
        exit_fee=exit_fee,
    )


# ── Paired loss (design doc §6.4) ──


def compute_paired_loss(
    *,
    origin_notional_usd: float,
    origin_fee_usd: float,
    paired_complement_notional: float,
    paired_complement_fee: float,
    origin_size: float,
) -> float:
    """Compute the locked-in loss on the paired portion.

    ``paired_cost = origin_notional + origin_fee + complement_notional + complement_fee``
    ``paired_loss = max(0, paired_cost - q)``

    Each of the ``q`` pairs recycles at $1 (merge/resolution), so the
    nominal recovery is ``q`` dollars.  Negative loss (profit) is clamped
    to zero — paired profit does not subsidise other batches.
    """
    q = origin_size
    paired_cost = (
        origin_notional_usd
        + origin_fee_usd
        + paired_complement_notional
        + paired_complement_fee
    )
    return max(0.0, paired_cost - q)


# ── Maker SELL target price (design doc §6.5) ──


def _sell_fee_per_share(market: "Market", price: float) -> float:
    """Estimated taker fee for one SELL fill at *price*.

    The design doc assumes a worst-case taker fee for SELL targeting
    (the bot can't guarantee maker execution).  Polymarket uses the same
    fee formula for both sides.
    """
    if market.fee_bps <= 0:
        return 0.0
    rate = market.fee_bps / 10_000.0
    return rate * (price * (1.0 - price)) ** market.fee_exponent


def max_take_price_for_pair(
    origin_price: float,
    market: "Market",
    max_pair_loss_cents: float,
) -> float:
    """Return the highest complementary BUY price within the per-pair loss cap."""
    ceiling = 1.0 + max(0.0, max_pair_loss_cents) / 100.0
    tick = float(getattr(market, "tick", 0.01))
    price = math.floor(min(1.0 - tick, ceiling - origin_price) / tick + 1e-9) * tick
    while price > 0 and origin_price + price + _sell_fee_per_share(market, price) > ceiling + 1e-12:
        price = math.floor((price - tick) / tick + 1e-9) * tick
    return max(0.0, price)


def ceil_to_tick(price: float, tick: float) -> float:
    """Round *price* up to the nearest valid tick size."""
    import math
    # Use a small epsilon to avoid floating-point boundary issues where
    # a tick-exact price like 0.50 gets rounded up to 0.51.
    epsilon = tick * 1e-6
    return math.ceil(price / tick - epsilon) * tick


def compute_sell_target(
    *,
    market: "Market",
    exit_size: float,
    exit_cost_notional: float,
    exit_cost_fee: float,
    paired_loss_usd: float,
    best_bid: float | None,
    tick: float | None = None,
    max_price: float | None = None,
) -> SellTargetResult:
    """Compute the minimum maker SELL price that covers all costs.

    ``required_net = exit_cost_notional + exit_cost_fee + paired_loss_usd``

    The target is the lowest legal price *p* where:
        ``p * exit_size - estimated_sell_fee(p, exit_size) >= required_net``

    The price is rounded up to a valid tick and must exceed
    ``best_bid + tick`` to avoid crossing the spread (design doc §6.5).

    If the target exceeds the market maximum price, returns
    ``SellTargetResult(price=None, reason="above_max_price")``.
    """
    if exit_size <= 0:
        return SellTargetResult(
            price=None, estimated_fee_per_share=0.0,
            required_net_usd=0.0, reason="zero_exit_size")

    tick_val = tick if tick is not None else getattr(market, 'tick', 0.01)
    max_p = max_price if max_price is not None else MARKET_MAX_PRICE - 0.01

    required_net = exit_cost_notional + exit_cost_fee + paired_loss_usd

    # Required gross per share to net required_net after fee:
    #   p * exit_size - fee_rate * (p*(1-p))^exp * exit_size = required_net / exit_size
    #   → need to solve: p - fee_rate * (p*(1-p))^exp = required_net / exit_size
    #
    # We search from the theoretical minimum (required per share) up to
    # max_price in tick increments to find the smallest valid p.
    required_per_share = required_net / exit_size
    p = required_per_share
    found = False

    # Binary search through ticks
    while p <= max_p + tick_val * 0.5:
        fee_per_share = _sell_fee_per_share(market, p)
        net_per_share = p - fee_per_share
        if net_per_share >= required_per_share - 1e-12:
            found = True
            break
        p += tick_val

    if not found:
        return SellTargetResult(
            price=None,
            estimated_fee_per_share=_sell_fee_per_share(market, max_p),
            required_net_usd=required_net,
            reason=f"above_max_price: need p>={ceil_to_tick(required_per_share, tick_val):.4f} "
                   f"but max={max_p:.4f}")

    theoretical = p

    # Round up to tick
    target = ceil_to_tick(theoretical, tick_val)

    # Guard: must not cross the spread (design doc §6.5)
    if best_bid is not None:
        target = max(target, best_bid + tick_val)

    if target > max_p:
        return SellTargetResult(
            price=None,
            estimated_fee_per_share=_sell_fee_per_share(market, target),
            required_net_usd=required_net,
            reason=f"above_max_price_after_bid_guard: best_bid={best_bid:.4f} "
                   f"best_bid+tick={best_bid + tick_val:.4f} target={target:.4f} "
                   f"max={max_p:.4f}")

    return SellTargetResult(
        price=target,
        estimated_fee_per_share=_sell_fee_per_share(market, target),
        required_net_usd=required_net,
        reason="",
    )


# ── Transition helpers ──


def transition_to_seal_pending(
    batch: RewardExitBatch,
    *,
    take_fills: list[TakeFill],
    updated_ts: float = 0.0,
) -> RewardExitBatch:
    """Compute paired loss from confirmed take fills and move to SELL_PENDING.

    *take_fills* must cover exactly 2q total confirmed take volume.
    The split is irreversible — once sealed, order merge or subsequent
    fills may NOT recompute these costs.
    """
    q = batch.origin_size
    total_filled = sum(f.size for f in take_fills)
    if total_filled < 2 * q - 1e-9:
        raise ValueError(
            f"Not enough take fills to seal batch {batch.batch_id}: "
            f"filled={total_filled:.1f}, needed={2*q:.1f}")

    total_notional = sum(f.notional for f in take_fills)
    total_fee = sum(f.fee_usd for f in take_fills)

    split = split_take_fills_fifo(take_fills, q)
    paired_loss = compute_paired_loss(
        origin_notional_usd=batch.origin_notional_usd,
        origin_fee_usd=batch.origin_fee_usd,
        paired_complement_notional=split.paired_notional,
        paired_complement_fee=split.paired_fee,
        origin_size=q,
    )

    return RewardExitBatch(
        batch_id=batch.batch_id,
        cid=batch.cid,
        origin_order_id=batch.origin_order_id,
        origin_fill_id=batch.origin_fill_id,
        origin_token_id=batch.origin_token_id,
        complement_token_id=batch.complement_token_id,
        origin_size=q,
        origin_notional_usd=batch.origin_notional_usd,
        origin_fee_usd=batch.origin_fee_usd,
        take_target_size=batch.take_target_size,
        take_filled_size=total_filled,
        take_notional_usd=total_notional,
        take_fee_usd=total_fee,
        paired_size=q,
        paired_loss_usd=paired_loss,
        exit_initial_size=q,
        exit_target_price=0.0,  # set by caller via compute_sell_target
        status="SELL_PENDING",
        created_ts=batch.created_ts,
        updated_ts=updated_ts,
    )


def transition_to_closed(
    batch: RewardExitBatch,
    *,
    updated_ts: float = 0.0,
) -> RewardExitBatch:
    """Close a batch whose exit has fully sold."""
    return RewardExitBatch(
        batch_id=batch.batch_id,
        cid=batch.cid,
        origin_order_id=batch.origin_order_id,
        origin_fill_id=batch.origin_fill_id,
        origin_token_id=batch.origin_token_id,
        complement_token_id=batch.complement_token_id,
        origin_size=batch.origin_size,
        origin_notional_usd=batch.origin_notional_usd,
        origin_fee_usd=batch.origin_fee_usd,
        take_target_size=batch.take_target_size,
        take_filled_size=batch.take_filled_size,
        take_notional_usd=batch.take_notional_usd,
        take_fee_usd=batch.take_fee_usd,
        paired_size=batch.paired_size,
        paired_loss_usd=batch.paired_loss_usd,
        exit_initial_size=batch.exit_initial_size,
        exit_filled_size=batch.exit_filled_size,
        exit_notional_usd=batch.exit_notional_usd,
        exit_fee_usd=batch.exit_fee_usd,
        exit_target_price=batch.exit_target_price,
        status="CLOSED",
        created_ts=batch.created_ts,
        updated_ts=updated_ts,
        closed_ts=updated_ts,
    )


def transition_to_manual_hold(
    batch: RewardExitBatch,
    *,
    reason: str,
    updated_ts: float = 0.0,
) -> RewardExitBatch:
    """Move a batch to MANUAL_HOLD with a machine-readable reason."""
    return RewardExitBatch(
        batch_id=batch.batch_id,
        cid=batch.cid,
        origin_order_id=batch.origin_order_id,
        origin_fill_id=batch.origin_fill_id,
        origin_token_id=batch.origin_token_id,
        complement_token_id=batch.complement_token_id,
        origin_size=batch.origin_size,
        origin_notional_usd=batch.origin_notional_usd,
        origin_fee_usd=batch.origin_fee_usd,
        take_target_size=batch.take_target_size,
        take_filled_size=batch.take_filled_size,
        take_notional_usd=batch.take_notional_usd,
        take_fee_usd=batch.take_fee_usd,
        paired_size=batch.paired_size,
        paired_loss_usd=batch.paired_loss_usd,
        exit_initial_size=batch.exit_initial_size,
        exit_filled_size=batch.exit_filled_size,
        exit_notional_usd=batch.exit_notional_usd,
        exit_fee_usd=batch.exit_fee_usd,
        exit_target_price=batch.exit_target_price,
        status="MANUAL_HOLD",
        manual_reason=reason,
        created_ts=batch.created_ts,
        updated_ts=updated_ts,
    )


def transition_to_take_blocked(
    batch: RewardExitBatch,
    *,
    reason: str,
    updated_ts: float = 0.0,
) -> RewardExitBatch:
    """Keep an incomplete take batch locked until its cost becomes acceptable."""
    return replace(
        batch,
        status="TAKE_BLOCKED",
        manual_reason=reason,
        updated_ts=updated_ts,
    )


def valid_transition(from_status: str, to_status: str) -> bool:
    """Check whether *to_status* is an allowed transition from *from_status*."""
    allowed = _VALID_TRANSITIONS.get(from_status)
    return allowed is not None and to_status in allowed


def remaining_take(target_size: float, filled_size: float) -> float:
    """Shares still needed to complete the double take."""
    return max(0.0, target_size - filled_size)


def remaining_exit(batch: RewardExitBatch) -> float:
    """Shares still needed to complete the exit SELL."""
    return max(0.0, batch.exit_initial_size - batch.exit_filled_size)
