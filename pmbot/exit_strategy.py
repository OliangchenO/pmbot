"""Pure, executable-price decisions for unpaired-inventory exits."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Literal

from .books import Book
from .gamma import Market


ExitAction = Literal["none", "same_side_sell", "complement_buy"]


@dataclass(frozen=True)
class ExitDecision:
    action: ExitAction
    token_id: str | None
    limit_price: float | None
    requested_size: float
    expected_net_pnl_per_share: float | None
    phase: str
    reason: str
    target_offset: float = 0.0


def _fee_per_share(market: Market, price: float) -> float:
    rate = market.fee_bps / 10_000.0
    return rate * (price * (1.0 - price)) ** market.fee_exponent


def _ceil_tick(price: float, tick: float) -> float:
    return round(math.ceil((price - 1e-9) / tick) * tick, 6)


def _phase_target(cfg: dict, elapsed_hours: float) -> tuple[str, float]:
    progressive = cfg.get("progressive") or {}
    default = float(cfg.get("same_side_target", 0.01))
    if not progressive.get("enabled", False):
        return "profit", default
    for phase in progressive.get("phases", []):
        if elapsed_hours <= float(phase["max_hours"]):
            return f"phase_{phase['max_hours']}h", float(phase.get("same_side_target", default))
    return "small_loss", -float(progressive.get("max_loss_cents", 1.0)) / 100.0


def complement_profit_cap(market: Market, basis: float, target_offset: float) -> float:
    """Highest tick price whose fee-inclusive paired result meets the target."""
    price = min(1.0 - market.tick, 1.0 - basis - target_offset)
    price = round(math.floor((price + 1e-9) / market.tick) * market.tick, 6)
    while price > 0:
        if basis + price + _fee_per_share(market, price) <= 1.0 - target_offset + 1e-9:
            return price
        price = round(price - market.tick, 6)
    return 0.0


def _same_side_floor(market: Market, basis: float, target_offset: float) -> float:
    price = _ceil_tick(max(market.tick, basis + target_offset), market.tick)
    while price < 1.0:
        if price - _fee_per_share(market, price) >= basis + target_offset - 1e-9:
            return price
        price = round(price + market.tick, 6)
    return 1.0


def _size_at_or_better(levels: dict[float, float], price: float, *, buy: bool) -> float:
    if buy:
        return sum(size for level, size in levels.items() if level <= price + 1e-9)
    return sum(size for level, size in levels.items() if level >= price - 1e-9)


def _none(reason: str, phase: str, target_offset: float) -> ExitDecision:
    return ExitDecision("none", None, None, 0.0, None, phase, reason, target_offset)


def evaluate_exit(
    market: Market,
    unpaired: float,
    basis: float | None,
    elapsed_hours: float,
    excess_book: Book | None,
    complement_book: Book | None,
    clob_min_order_size: float | None,
    cfg: dict,
) -> ExitDecision:
    """Return one safe exit action, considering only executable book prices."""
    phase, target_offset = _phase_target(cfg, elapsed_hours)
    if basis is None:
        return _none("unknown_cost_basis", phase, target_offset)
    size = abs(unpaired)
    minimum = max(float(cfg.get("min_shares", 5.0)), float(clob_min_order_size or 0.0))
    if size < minimum:
        return _none("below_current_clob_min_order_size", phase, target_offset)

    excess_token = market.yes_token if unpaired > 0 else market.no_token
    complement_token = market.no_token if unpaired > 0 else market.yes_token
    candidates: list[ExitDecision] = []
    same_reason = "same_side_book_unavailable"
    if excess_book is not None and excess_book.best_bid is not None:
        floor = _same_side_floor(market, basis, target_offset)
        if floor > float(cfg.get("max_exit_price", 0.95)) + 1e-9:
            same_reason = "same_side_target_out_of_range"
        elif excess_book.best_bid < floor - 1e-9:
            same_reason = "same_side_bid_below_net_target"
        elif _size_at_or_better(excess_book.bids, floor, buy=False) < minimum:
            same_reason = "same_side_insufficient_depth"
        else:
            executable = excess_book.best_bid
            pnl = executable - _fee_per_share(market, executable) - basis
            candidates.append(ExitDecision(
                "same_side_sell", excess_token, floor, size, round(pnl, 6),
                phase, "same_side_executable", target_offset,
            ))

    complement_reason = "complement_book_unavailable"
    if complement_book is not None and complement_book.best_ask is not None:
        cap = complement_profit_cap(market, basis, target_offset)
        if cap <= 0:
            complement_reason = "complement_profit_cap_unavailable"
        elif complement_book.best_ask > cap + 1e-9:
            complement_reason = "complement_ask_above_profit_cap"
        elif _size_at_or_better(complement_book.asks, cap, buy=True) < minimum:
            complement_reason = "complement_insufficient_depth"
        else:
            executable = complement_book.best_ask
            pnl = 1.0 - basis - executable - _fee_per_share(market, executable)
            candidates.append(ExitDecision(
                "complement_buy", complement_token, cap, size, round(pnl, 6),
                phase, "complement_executable", target_offset,
            ))

    if candidates:
        candidates.sort(key=lambda item: (
            item.expected_net_pnl_per_share or float("-inf"),
            item.action == "same_side_sell",
        ), reverse=True)
        return candidates[0]
    if complement_reason not in {"complement_book_unavailable"}:
        return _none(complement_reason, phase, target_offset)
    return _none(same_reason, phase, target_offset)
