"""Brokers: PaperBroker (fill simulation, positions, PnL) and LiveBroker
(py-clob-client-v2 wrapper). Both expose the same interface."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from . import gamma
from .audit import AuditLogger
from .books import BookTracker
from .gamma import Market
from .strategy import Quote

log = logging.getLogger("pmbot.broker")

ORDER_RECONCILE_SECONDS = 30.0
# Substrings that mark a network blip worth retrying (vs a genuine API
# rejection like "not enough balance"). Seen live as SSL handshake timeouts,
# read timeouts, and "No route to host" from the CLOB client.
_TRANSIENT_MARKERS = (
    "timed out", "timeout", "route to host", "connection", "handshake",
    "temporarily unavailable", "reset by peer", "request exception",
    "max retries", "ssl", "eof occurred", "broken pipe",
)


def _is_transient(exc: Exception) -> bool:
    return any(m in str(exc).lower() for m in _TRANSIENT_MARKERS)


def _with_retry(label: str, fn, attempts: int = 3, base_delay: float = 0.3):
    """Run a network call, retrying transient failures with backoff.

    Runs on the broker's worker thread (order ops are dispatched via
    asyncio.to_thread), so the blocking sleep never stalls the event loop. A
    non-transient error (e.g. an order rejection) is re-raised immediately so
    callers keep their existing fail-fast / reconcile behavior.
    """
    last: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            last = e
            if i == attempts - 1 or not _is_transient(e):
                raise
            delay = base_delay * (2 ** i)
            log.warning("%s 出现临时错误（%s），%.1f 秒后重试 %d/%d",
                        label, e, delay, i + 1, attempts - 1)
            time.sleep(delay)
    assert last is not None
    raise last
# Polymarket GTD orders carry a 1-minute security threshold: an order with
# expiration=T is effectively dead at ~T-60s, so quote for ttl seconds we
# must sign expiration=now+ttl+60 and refresh well before T-60.
GTD_SECURITY_THRESHOLD_SECS = 60
GTD_REFRESH_MARGIN_SECS = GTD_SECURITY_THRESHOLD_SECS + 30
PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
USDC_DECIMALS = 1_000_000


@dataclass
class Position:
    yes_shares: float = 0.0
    no_shares: float = 0.0
    yes_cost: float = 0.0
    no_cost: float = 0.0
    merged_usd: float = 0.0
    fills: int = 0

    def merge(self) -> float:
        pairs = min(self.yes_shares, self.no_shares)
        if pairs > 0:
            self.yes_cost *= (self.yes_shares - pairs) / self.yes_shares
            self.no_cost *= (self.no_shares - pairs) / self.no_shares
            self.yes_shares -= pairs
            self.no_shares -= pairs
            self.merged_usd += pairs
        return pairs


@dataclass
class PaperState:
    cash: float
    start_equity: float
    positions: dict[str, Position] = field(default_factory=dict)
    est_rewards: float = 0.0
    fills_log: list[dict] = field(default_factory=list)


@dataclass
class RestingOrder:
    order_id: str
    quote: Quote
    placed_ts: float
    expiration: int = 0
    audit: dict = field(default_factory=dict)


@dataclass
class PendingHedge:
    """A confirmed FAK hedge which the Data API has not observed yet."""

    token_id: str
    size: float
    target_token_shares: float
    created_ts: float
    ws_observed: float = 0.0
    notified: bool = False  # 已发送 DingTalk 通知，防止 record_user_fill / refresh_state 重复推送


@dataclass
class PaperQuoteState:
    quote: Quote
    queue_ahead: float = 0.0
    active_at: float = 0.0  # resting on the book only once now >= active_at


@dataclass
class PaperRewardExitState:
    order_id: str
    quote: Quote
    audit: dict = field(default_factory=dict)
    queue_ahead: float = 0.0
    active_at: float = 0.0


def _parse_fill_amount(resp: dict, requested: float) -> float:
    for key in ("takingAmount", "taking_amount", "size_matched", "sizeMatched"):
        val = resp.get(key)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                pass
    if resp.get("success") is False or resp.get("error"):
        return 0.0
    status = str(resp.get("status") or "").upper()
    if status in ("MATCHED", "FILLED", "LIVE"):
        return requested
    return 0.0


def _parse_erc20_balance(result: str | None) -> float:
    if not result:
        raise ValueError("empty eth_call result")
    return int(result, 16) / USDC_DECIMALS


class PaperBroker:
    """Simulates fills against the live book with a queue-position model,
    simulated order latency (placement and cancellation each take
    `latency_secs` — stale quotes stay fillable until the cancel "lands"),
    depth-aware taker fills, and taker fees (makers are never charged on
    Polymarket, so only the taker_buy path pays a fee)."""

    def __init__(self, capital: float, tracker: BookTracker, data_dir: str = "data",
                 latency_secs: float = 0.0):
        self.state = PaperState(cash=capital, start_equity=capital)
        self.tracker = tracker
        self.latency = latency_secs
        self.unpaired_since: dict[str, float] = {}
        self._quotes: dict[str, list[PaperQuoteState]] = {}
        self._exits: dict[str, PaperQuoteState] = {}
        self._reward_exits: dict[str, PaperRewardExitState] = {}
        # Quotes whose cancel is still in flight: list of (quote, fillable_until).
        self._dying: dict[str, list[tuple[Quote, float]]] = {}
        self._markets: dict[str, Market] = {}
        self._token_to_market: dict[str, Market] = {}
        self._last_mids: dict[str, float] = {}
        self._data_path = Path(data_dir) / "paper_state.json"
        self._data_path.parent.mkdir(exist_ok=True)
        self.metrics = None
        self._load_persisted()
        tracker.on_trade(self._on_trade)

    def _load_persisted(self) -> None:
        """Restore fills_log and unpaired_since so escalation windows survive restarts."""
        try:
            if not self._data_path.exists():
                return
            data = json.loads(self._data_path.read_text())
            self.state.fills_log = data.get("fills", [])
            self.unpaired_since = {
                str(k): float(v) for k, v in data.get("unpaired_since", {}).items()
            }
        except (json.JSONDecodeError, OSError, ValueError) as e:
            log.warning("无法加载持久化状态：%s", e)

    def _fee_usd(self, market: Market, price: float, size: float) -> float:
        """Taker fee for a fill: rate × (p·(1−p))^exponent × shares.

        Matches Polymarket's protocol formula (fee = C × feeRate × p × (1−p),
        with fd.e the exponent — 1 for all current markets). Polymarket only
        charges takers, so this is applied exclusively on the taker_buy (FAK)
        path; resting maker fills (quotes and exits) pay 0.
        """
        if market.fee_bps <= 0:
            return 0.0
        rate = market.fee_bps / 10000.0
        return rate * (price * (1.0 - price)) ** market.fee_exponent * size

    def _start_dying(self, cid: str, st: PaperQuoteState, now: float) -> None:
        """A cancelled/replaced quote rests on the book until the cancel lands."""
        if now >= st.active_at and self.latency > 0:
            self._dying.setdefault(cid, []).append((st.quote, now + self.latency))

    def set_quotes(self, market: Market, quotes: list[Quote],
                   audit_context: dict[str, dict] | None = None) -> None:
        self._markets[market.condition_id] = market
        self._token_to_market[market.yes_token] = market
        self._token_to_market[market.no_token] = market
        now = time.time()
        cur = {s.quote.token_id: s for s in self._quotes.get(market.condition_id, [])}
        new_states = []
        for q in quotes:
            prev = cur.pop(q.token_id, None)
            if prev is not None and prev.quote.key() == q.key():
                new_states.append(prev)
                continue
            if prev is not None:
                self._start_dying(market.condition_id, prev, now)
            book = self.tracker.books.get(q.token_id)
            ahead = book.bids.get(q.price, 0.0) if book else 0.0
            new_states.append(PaperQuoteState(
                quote=q, queue_ahead=ahead, active_at=now + self.latency))
        for prev in cur.values():  # no longer desired
            self._start_dying(market.condition_id, prev, now)
        self._quotes[market.condition_id] = new_states

    def cancel_all(self, exclude_cids: set[str] | None = None) -> None:
        self.cancel_quotes(exclude_cids=exclude_cids)
        exclude = exclude_cids or set()
        for cid in list(self._exits):
            if cid not in exclude:
                self._exits.pop(cid, None)

    def cancel_quotes(self, exclude_cids: set[str] | None = None) -> None:
        now = time.time()
        exclude = exclude_cids or set()
        for cid, states in self._quotes.items():
            if cid in exclude:
                continue
            for st in states:
                self._start_dying(cid, st, now)
            self._quotes.pop(cid, None)

    def open_quotes(self, market: Market) -> list[Quote]:
        return [s.quote for s in self._quotes.get(market.condition_id, [])]

    def due_for_refresh(self, market: Market) -> bool:
        return False

    def set_exit(self, market: Market, quote: Quote | None) -> bool:
        cid = market.condition_id
        if quote is None:
            self._exits.pop(cid, None)
            return True
        cur = self._exits.get(cid)
        if cur is not None and cur.quote.key() == quote.key():
            return True
        self._markets[cid] = market
        self._token_to_market[market.yes_token] = market
        self._token_to_market[market.no_token] = market
        book = self.tracker.books.get(quote.token_id)
        ahead = book.asks.get(quote.price, 0.0) if book else 0.0
        self._exits[cid] = PaperQuoteState(
            quote=quote, queue_ahead=ahead, active_at=time.time() + self.latency)
        return True

    def exit_quote(self, market: Market) -> Quote | None:
        cur = self._exits.get(market.condition_id)
        return cur.quote if cur else None

    @property
    def fills_log(self) -> list[dict]:
        return self.state.fills_log

    async def _on_trade(self, token_id: str, trade_price: float,
                        side: str = "", size: float = 0.0) -> None:
        market = self._token_to_market.get(token_id)
        if market is None:
            return
        now = time.time()
        cid = market.condition_id
        # A trade print is one shared volume budget.  Keep it across ordinary
        # quotes, the legacy exit, and per-batch exits so simultaneous orders
        # cannot all consume the same print.
        trade_remaining = max(0.0, size) if size > 0 else float("inf")

        # Stale quotes whose cancel hasn't landed yet get picked off by
        # through-prints — the dominant live cost paper used to miss.
        keep_dying = []
        for q, until in self._dying.get(cid, []):
            if now >= until:
                continue
            if q.token_id == token_id and trade_price < q.price - 1e-9:
                fill_sz = min(q.size, trade_remaining)
                if fill_sz > 0:
                    self._fill(market, Quote(q.token_id, q.price, fill_sz), fill_sz)
                    trade_remaining -= fill_sz
            else:
                keep_dying.append((q, until))
        self._dying[cid] = keep_dying

        states = self._quotes.get(cid, [])
        remaining = []
        for st in states:
            q = st.quote
            if q.token_id != token_id:
                remaining.append(st)
                continue
            active = now >= st.active_at
            if trade_price < q.price - 1e-9:
                if not active:
                    # Level swept while our order was in flight: we missed the
                    # trade but will be alone at the level once we land.
                    st.queue_ahead = 0.0
                    remaining.append(st)
                    continue
                # Price priority guarantees the fill, but only for the taker's size.
                fill_sz = min(q.size, trade_remaining)
                if fill_sz > 0:
                    self._fill(market, Quote(q.token_id, q.price, fill_sz), fill_sz)
                    trade_remaining -= fill_sz
                if fill_sz < q.size - 1e-9:
                    st.quote = Quote(q.token_id, q.price, q.size - fill_sz)
                    st.queue_ahead = 0.0
                    remaining.append(st)
            elif abs(trade_price - q.price) < 1e-9 and trade_remaining > 0:
                consume = min(trade_remaining, st.queue_ahead)
                st.queue_ahead -= consume
                trade_remaining -= consume
                leftover = trade_remaining
                if active and leftover > 0 and st.queue_ahead <= 1e-9:
                    fill_sz = min(leftover, q.size)
                    self._fill(market, Quote(q.token_id, q.price, fill_sz), fill_sz)
                    trade_remaining -= fill_sz
                    if fill_sz < q.size - 1e-9:
                        st.quote = Quote(q.token_id, q.price, q.size - fill_sz)
                        remaining.append(st)
                else:
                    remaining.append(st)
            else:
                remaining.append(st)
        self._quotes[cid] = remaining

        ex = self._exits.get(cid)
        if ex is not None and ex.quote.token_id == token_id:
            active = now >= ex.active_at
            if trade_price > ex.quote.price + 1e-9:
                if active:
                    fill_sz = min(trade_remaining, ex.quote.size)
                    if fill_sz > 0:
                        self._fill_exit(market, ex.quote, fill_sz)
                        trade_remaining -= fill_sz
                        if fill_sz >= ex.quote.size - 1e-9:
                            self._exits.pop(cid, None)
                        else:
                            ex.quote = Quote(token_id, ex.quote.price,
                                             ex.quote.size - fill_sz)
            elif abs(trade_price - ex.quote.price) < 1e-9 and trade_remaining > 0:
                consume = min(trade_remaining, ex.queue_ahead)
                ex.queue_ahead -= consume
                trade_remaining -= consume
                leftover = trade_remaining
                if active and leftover > 0 and ex.queue_ahead <= 1e-9:
                    fill_sz = min(leftover, ex.quote.size)
                    self._fill_exit(market, ex.quote, fill_sz)
                    trade_remaining -= fill_sz
                    if fill_sz >= ex.quote.size - 1e-9:
                        self._exits.pop(cid, None)
                    else:
                        ex.quote = Quote(token_id, ex.quote.price,
                                         ex.quote.size - fill_sz)

        # Batch reward exits are independent per batch and must not reuse the
        # singleton recovery exit above.
        for batch_id, ex in list(self._reward_exits.items()):
            if trade_remaining <= 1e-9:
                break
            if ex.quote.token_id != token_id:
                continue
            active = now >= ex.active_at
            fill_sz = 0.0
            if trade_price > ex.quote.price + 1e-9 and active:
                fill_sz = min(trade_remaining, ex.quote.size)
            elif abs(trade_price - ex.quote.price) < 1e-9 and trade_remaining > 0:
                consume = min(trade_remaining, ex.queue_ahead)
                ex.queue_ahead -= consume
                trade_remaining -= consume
                leftover = trade_remaining
                if active and leftover > 0 and ex.queue_ahead <= 1e-9:
                    fill_sz = min(leftover, ex.quote.size)
            if fill_sz <= 0:
                continue
            self._fill_exit(market, ex.quote, fill_sz,
                            order_id=ex.order_id, audit=ex.audit)
            if fill_sz >= ex.quote.size - 1e-9:
                self._reward_exits.pop(batch_id, None)
            else:
                ex.quote = Quote(token_id, ex.quote.price,
                                 ex.quote.size - fill_sz)
            trade_remaining = max(0.0, trade_remaining - fill_sz)

    def check_crossed_books(self) -> None:
        now = time.time()
        for cid, dies in list(self._dying.items()):
            keep = []
            for q, until in dies:
                if now >= until:
                    continue
                book = self.tracker.books.get(q.token_id)
                ask = book.best_ask if book else None
                if ask is not None and ask <= q.price:
                    self._fill(self._markets[cid], q, q.size)
                else:
                    keep.append((q, until))
            self._dying[cid] = keep
        for cid, states in list(self._quotes.items()):
            market = self._markets[cid]
            remaining = []
            for st in states:
                q = st.quote
                book = self.tracker.books.get(q.token_id)
                ask = book.best_ask if book else None
                if ask is not None and ask <= q.price and now >= st.active_at:
                    self._fill(market, q, q.size)
                else:
                    if book is not None:
                        # People ahead of us cancelling shortens our queue.
                        st.queue_ahead = min(st.queue_ahead,
                                             book.bids.get(q.price, 0.0))
                    remaining.append(st)
            self._quotes[cid] = remaining
        for cid, ex in list(self._exits.items()):
            book = self.tracker.books.get(ex.quote.token_id)
            bid = book.best_bid if book else None
            if bid is not None and bid >= ex.quote.price and now >= ex.active_at:
                self._fill_exit(self._markets[cid], ex.quote, ex.quote.size)
                self._exits.pop(cid, None)

    def _record_reward_exit_fact(self, entry: dict) -> None:
        if not self.metrics or not hasattr(self.metrics, "record_reward_exit_fill"):
            return
        intent = str(entry.get("intent") or "")
        if intent not in {"normal_reward", "batch_exit"}:
            return
        self.metrics.record_reward_exit_fill(
            fill_id=str(entry["fill_id"]),
            batch_id=str(entry.get("batch_id") or ""),
            order_id=str(entry.get("order_id") or ""),
            intent=intent,
            cid=str(entry["cid"]), token_id=str(entry["token"]),
            side=str(entry["side"]), price=float(entry["price"]),
            size=float(entry["size"]), fee_usd=float(entry.get("fee") or 0.0),
            ts=float(entry["ts"]),
        )

    def _fill(self, market: Market, q: Quote, size: float) -> None:
        pos = self.state.positions.setdefault(market.condition_id, Position())
        # Maker fill: no fee on Polymarket.
        cost = q.price * size
        self.state.cash -= cost
        if q.token_id == market.yes_token:
            pos.yes_shares += size
            pos.yes_cost += cost
            side = "YES"
        else:
            pos.no_shares += size
            pos.no_cost += cost
            side = "NO"
        pos.fills += 1
        merged = pos.merge()
        if merged:
            self.state.cash += merged
        entry = {
            "ts": time.time(), "cid": market.condition_id,
            "market": market.question[:50], "side": side,
            "token": q.token_id, "price": q.price, "size": size, "merged": merged,
            "path": "normal", "intent": "normal_reward",
            "fill_id": f"paper-{uuid.uuid4().hex}",
        }
        self.state.fills_log.append(entry)
        if self.metrics:
            self.metrics.record_fill(entry)
            self._record_reward_exit_fact(entry)
            if merged:
                self.metrics.record_merge(market.condition_id, merged)
        log.info("成交：%s %s %.0f 股 @ %.3f（已合并 %.0f 对）",
                 market.question[:40], side, size, q.price, merged)
        self._persist()

    def _fill_exit(self, market: Market, q: Quote, fill_sz: float,
                   *, order_id: str | None = None,
                   audit: dict | None = None) -> None:
        pos = self.state.positions.setdefault(market.condition_id, Position())
        if q.token_id == market.yes_token:
            size = min(fill_sz, pos.yes_shares)
            if size > 0:
                pos.yes_cost *= (pos.yes_shares - size) / pos.yes_shares
            pos.yes_shares -= size
            side = "YES"
        else:
            size = min(fill_sz, pos.no_shares)
            if size > 0:
                pos.no_cost *= (pos.no_shares - size) / pos.no_shares
            pos.no_shares -= size
            side = "NO"
        if size <= 0:
            return
        # Resting limit-sell exit is a maker order: no fee on Polymarket.
        self.state.cash += q.price * size
        pos.fills += 1
        context = audit or {}
        entry = {
            "ts": time.time(), "cid": market.condition_id,
            "market": market.question[:50], "side": side,
            "token": q.token_id, "price": q.price, "size": size, "exit": True,
            "fill_id": f"paper-{uuid.uuid4().hex}",
        }
        if order_id:
            entry["order_id"] = order_id
        if context.get("batch_id"):
            entry["batch_id"] = context["batch_id"]
        entry["intent"] = context.get("intent", "batch_exit") if context else "normal_exit"
        self.state.fills_log.append(entry)
        if self.metrics:
            self.metrics.record_fill(entry)
            self._record_reward_exit_fact(entry)
        log.info("退出成交：%s 卖出 %.0f 股 %s @ %.3f",
                 market.question[:40], size, side, q.price)
        self._persist()

    def taker_buy(self, market: Market, token_id: str, size: float,
                  max_price: float, audit_context: dict | None = None) -> float:
        book = self.tracker.books.get(token_id)
        if book is None or not book.asks:
            return 0.0
        # Walk displayed depth up to max_price — a real FAK fills partially.
        remaining = size
        cost = 0.0
        filled = 0.0
        for price in sorted(book.asks):
            if price > max_price + 1e-9 or remaining <= 1e-9:
                break
            take = min(remaining, book.asks[price])
            cost += take * price
            filled += take
            remaining -= take
        if filled <= 0:
            return 0.0
        avg = cost / filled
        pos = self.state.positions.setdefault(market.condition_id, Position())
        fee = self._fee_usd(market, avg, filled)
        self.state.cash -= cost + fee
        if token_id == market.yes_token:
            pos.yes_shares += filled
            pos.yes_cost += cost + fee
            side = "YES"
        else:
            pos.no_shares += filled
            pos.no_cost += cost + fee
            side = "NO"
        pos.fills += 1
        merged = pos.merge()
        if merged:
            self.state.cash += merged
        context = audit_context or {}
        intent = context.get("intent", "forced_hedge")
        path = context.get(
            "path", "reward_exit_take" if intent == "batch_take" else "forced_hedge")
        entry = {
            "ts": time.time(), "cid": market.condition_id,
            "market": market.question[:50], "side": side,
            "token": token_id, "price": avg, "size": filled, "merged": merged,
            "taker": True, "path": path, "intent": intent,
            "fill_id": f"paper-{uuid.uuid4().hex}",
        }
        if context.get("batch_id"):
            entry["batch_id"] = context["batch_id"]
        if fee > 0:
            entry["fee"] = fee
        self.state.fills_log.append(entry)
        if self.metrics:
            self.metrics.record_fill(entry)
            self._record_reward_exit_fact(entry)
            self.metrics.record_hedge(market.condition_id, avg, filled)
            if merged:
                self.metrics.record_merge(market.condition_id, merged)
        log.info("吃单成交：%s %s %.0f 股 @ %.3f（已合并 %.0f 对%s）",
                 market.question[:40], side, filled, avg, merged,
                 f", fee ${fee:.2f}" if fee > 0 else "")
        self._persist()
        return filled

    def place_reward_exit(self, market: Market, batch_id: str,
                          quote: Quote, audit_context: dict) -> PaperRewardExitState | None:
        self._markets[market.condition_id] = market
        self._token_to_market[market.yes_token] = market
        self._token_to_market[market.no_token] = market
        book = self.tracker.books.get(quote.token_id)
        ahead = book.bids.get(quote.price, 0.0) if book else 0.0
        state = PaperRewardExitState(
            order_id=f"paper-exit-{uuid.uuid4().hex}", quote=quote,
            audit={**audit_context, "batch_id": batch_id, "intent": "batch_exit"},
            queue_ahead=ahead, active_at=time.time() + self.latency,
        )
        self._reward_exits[batch_id] = state
        return state

    def cancel_reward_exit(self, batch_id: str) -> bool:
        self._reward_exits.pop(batch_id, None)
        return True

    def position_tokens(self) -> list[str]:
        tokens = []
        for cid, pos in self.state.positions.items():
            if pos.yes_shares > 0 or pos.no_shares > 0:
                market = self._markets.get(cid)
                if market:
                    tokens.extend((market.yes_token, market.no_token))
        return tokens

    def equity(self) -> float:
        total = self.state.cash + self.state.est_rewards
        for cid, pos in self.state.positions.items():
            market = self._markets.get(cid)
            if market is None:
                continue
            yes_mid = self._mark(market.yes_token)
            no_mid = self._mark(market.no_token)
            if no_mid is None:
                no_mid = 1 - yes_mid if yes_mid is not None else 0.5
            if yes_mid is None:
                yes_mid = 1 - no_mid
            total += pos.yes_shares * yes_mid + pos.no_shares * no_mid
        return total

    def _mark(self, token_id: str) -> float | None:
        book = self.tracker.books.get(token_id)
        mid = book.mid if book else None
        if mid is not None:
            self._last_mids[token_id] = mid
            return mid
        return self._last_mids.get(token_id)

    def net_yes_exposure_usd(self, market: Market) -> float:
        pos = self.state.positions.get(market.condition_id)
        if pos is None:
            return 0.0
        mid = self._mark(market.yes_token)
        if mid is None:
            mid = 0.5
        return pos.yes_shares * mid - pos.no_shares * (1 - mid)

    def unpaired_shares(self, market: Market) -> float:
        pos = self.state.positions.get(market.condition_id)
        if pos is None:
            return 0.0
        return pos.yes_shares - pos.no_shares

    def unpaired_cost_basis(self, market: Market) -> float | None:
        """Average all-in cost of the currently unpaired paper position."""
        pos = self.state.positions.get(market.condition_id)
        if pos is None:
            return None
        if pos.yes_shares > pos.no_shares and pos.yes_shares > 0:
            return pos.yes_cost / pos.yes_shares
        if pos.no_shares > pos.yes_shares and pos.no_shares > 0:
            return pos.no_cost / pos.no_shares
        return None

    def position_shares(self, market: Market) -> tuple[float, float]:
        """Return (yes_shares, no_shares) for the market, or (0, 0)."""
        pos = self.state.positions.get(market.condition_id)
        if pos is None:
            return (0.0, 0.0)
        return (pos.yes_shares, pos.no_shares)

    def held_markets(self) -> list[Market]:
        return [
            self._markets[cid]
            for cid, pos in self.state.positions.items()
            if cid in self._markets and (pos.yes_shares > 0 or pos.no_shares > 0)
        ]

    def last_fill_ts(self, cid: str) -> float | None:
        """Timestamp of the most recent fill for this market, if any.

        Only returns timestamps from confirmed fills (entries with a price),
        not from Data API inferred position changes.
        """
        for entry in reversed(self.state.fills_log):
            if entry.get("cid") == cid and "price" in entry:
                return float(entry["ts"])
        return getattr(self, "unpaired_since", {}).get(cid)

    def total_inventory_usd(self) -> float:
        return sum(
            abs(self.net_yes_exposure_usd(m)) for m in self._markets.values()
        )

    def accrue_rewards(self, usd: float) -> None:
        self.state.est_rewards += usd
        if self.metrics:
            self.metrics.record_est_reward(usd)

    def _persist(self) -> None:
        try:
            self._data_path.write_text(json.dumps({
                "cash": self.state.cash,
                "est_rewards": self.state.est_rewards,
                "fills": self.state.fills_log[-200:],
                "unpaired_since": getattr(self, "unpaired_since", {}),
            }, indent=2))
        except OSError as e:
            log.warning("无法持久化状态：%s", e)


def _notify_live_fill(notifier, entry: dict, order_side: str) -> None:
    """Queue a live-fill notification without affecting broker state."""
    if notifier is None:
        return
    try:
        notifier.send_text(
            "[PMBot LIVE FILL] "
            f"{entry['market']} | {order_side} {entry['side']} "
            f"{entry['size']:.2f} @ {entry['price']:.3f}"
        )
    except Exception as exc:  # noqa: BLE001 - notifications are best-effort
        log.warning("钉钉成交通知发送失败：%s", exc)


class LiveBroker:
    """Real order placement through py-clob-client-v2 (CLOB V2)."""

    HOST = "https://clob.polymarket.com"
    DATA_API = "https://data-api.polymarket.com"
    CHAIN_ID = 137

    def __init__(self, cfg: dict, tracker: BookTracker, notifier=None):
        from py_clob_client_v2 import ClobClient

        key = os.environ.get("POLYMARKET_PRIVATE_KEY") or ""
        funder = os.environ.get("POLYMARKET_FUNDER") or None
        if not key:
            raise SystemExit(
                "live mode requires POLYMARKET_PRIVATE_KEY in .env "
                "(and POLYMARKET_FUNDER for email/browser-wallet accounts)"
            )
        self.cfg = cfg
        metrics_cfg = cfg.get("metrics") or {}
        self.audit = AuditLogger(metrics_cfg.get("audit_log", "data/audit.jsonl"))
        self._recovery_basis_cache = self.audit.recovery_bases()
        self.order_ttl = int(cfg["quoting"].get("order_ttl_secs", 90))
        self.recovery_order_ttl = int(cfg["quoting"].get(
            "recovery_order_ttl_secs", self.order_ttl))
        self.exit_order_ttl = int(cfg["risk"].get("exit_order_ttl_secs", 600))
        # Refresh resting quotes by posting the replacement BEFORE cancelling
        # the expiring order, so a pure GTD refresh never leaves the side off
        # the book (a momentary double-size overlap until the cancel lands,
        # which a reward farmer prefers to a reward-scoring gap). Price/size
        # changes still cancel-first to avoid resting two prices at once.
        self.refresh_overlap = bool(cfg["quoting"].get("refresh_overlap", True))
        self.rpc_url = cfg["live"].get("rpc_url")
        sig_type = int(cfg["live"]["signature_type"])
        if sig_type in (1, 2, 3) and not funder:
            raise SystemExit(
                f"live.signature_type={sig_type} requires POLYMARKET_FUNDER "
                "so orders, balances, and positions target the funding wallet "
                "(proxy / Safe / deposit wallet)"
            )
        kwargs = {"key": key, "chain_id": self.CHAIN_ID, "signature_type": sig_type}
        if funder:
            kwargs["funder"] = funder
        self.client = ClobClient(self.HOST, **kwargs)
        api_creds = _with_retry(
            "create_or_derive_api_key",
            self.client.create_or_derive_api_key,
            attempts=5, base_delay=2.0,
        )
        self.client.set_api_creds(api_creds)
        self.tracker = tracker
        self.notifier = notifier
        self.address = funder or self.client.get_address()
        self._client_lock = threading.RLock()
        self.ws_fills_active = False
        self._open_orders: dict[str, list[RestingOrder]] = {}
        self._exit_orders: dict[str, RestingOrder] = {}
        self._reward_exit_orders: dict[str, RestingOrder] = {}
        self._taker_order_contexts: dict[str, dict] = {}
        # Audit contexts saved for ~120s after cancellation so late-arriving
        # WebSocket fills from orders that were matched before the cancel landed
        # still carry the correct path/intent (e.g. for reward-exit batching).
        self._recently_cancelled: dict[str, tuple[dict, float]] = {}
        self._markets: dict[str, Market] = {}
        self._unmanaged_position_cids: set[str] = set()
        self._positions: dict[str, dict] = {}
        self._token_shares: dict[str, float] = {}
        self._pending_hedges: dict[str, PendingHedge] = {}
        self._state_lock = threading.RLock()
        self._collateral: float = float("nan")
        self._synced = False
        self.fills_log: list[dict] = []
        # _over_since timestamps that survive restarts so escalate windows
        # don't reset. Populated by Bot._manage_market_inventory and read
        # back by last_fill_ts fallback when fills_log is cold (fresh start).
        metrics_cfg = cfg.get("metrics") or {}
        self._live_data_path = Path(metrics_cfg.get("db_path", "data/metrics.db")).parent / "live_state.json"
        self._live_data_path.parent.mkdir(parents=True, exist_ok=True)
        self.unpaired_since: dict[str, float] = {}
        self._load_persisted()
        self._ws_deltas: deque[tuple[float, str, float, str, float, str]] = deque()
        # record_user_fill appends on the event loop while refresh_state
        # (worker thread) iterates/trims — guard against concurrent mutation.
        self._ws_deltas_lock = threading.Lock()
        self._last_order_reconcile = 0.0
        self._logged_post_shape = False
        self.metrics = None
        self.merger = None
        if cfg["live"].get("merge_enabled", False):
            try:
                from .merger import Merger
                # Deposit wallets (signature_type 3) merge gaslessly via the
                # Polymarket relayer, which authenticates with *builder* API-key
                # creds (Builders page -> API keys: apiKey/secret/passphrase).
                # Absent, type-3 merging stays off.
                builder_creds = None
                bk = os.environ.get("POLYMARKET_BUILDER_API_KEY")
                bs = os.environ.get("POLYMARKET_BUILDER_SECRET")
                bp = os.environ.get("POLYMARKET_BUILDER_PASSPHRASE")
                if bk and bs and bp:
                    builder_creds = {"key": bk, "secret": bs, "passphrase": bp}
                self.merger = Merger(
                    cfg["live"]["rpc_url"], sig_type, key, funder,
                    relayer_url=cfg["live"].get("relayer_url"),
                    builder_creds=builder_creds,
                    audit_event=self.audit.record)
            except Exception as e:  # noqa: BLE001
                log.warning("链上合并器不可用：%s", e)
        log.info("LIVE 客户端已就绪（signature_type=%d，地址=%s）", sig_type, self.address)

    def _gtd_expiration(self, ttl_secs: int | None = None) -> int:
        ttl = self.order_ttl if ttl_secs is None else ttl_secs
        return int(time.time()) + ttl + GTD_SECURITY_THRESHOLD_SECS

    def _buy_order_ttl(self, audit: dict | None) -> int:
        """Use the longer GTD lifetime only for inventory-reducing recovery bids."""
        return self.recovery_order_ttl if (audit or {}).get("recovery_order") else self.order_ttl

    def _erc20_balance(self, token_address: str, owner: str) -> float:
        import httpx

        if not self.rpc_url:
            raise RuntimeError("live.rpc_url is required for on-chain balance refresh")
        owner_arg = owner.lower().removeprefix("0x").rjust(64, "0")
        data = "0x70a08231" + owner_arg
        resp = httpx.post(
            self.rpc_url,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "eth_call",
                "params": [{"to": token_address, "data": data}, "latest"],
            },
            timeout=10.0,
        )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("error"):
            raise RuntimeError(payload["error"])
        return _parse_erc20_balance(payload.get("result"))

    def _sync_clob_balance(self, asset_type, token_id: str | None = None) -> None:
        """Push on-chain balances into the CLOB's server-side cache.

        REQUIRED for deposit wallets (signature_type 3): unlike EOA/proxy
        wallets, the CLOB does not auto-track a deposit wallet's chain balances,
        so until this is called the cache reads 0 and orders are rejected with
        "not enough balance / allowance: ... balance: 0". Call it for COLLATERAL
        before spending pUSD and for the CONDITIONAL token before selling it.
        Best-effort: a sync hiccup shouldn't crash the quoting loop."""
        try:
            from py_clob_client_v2 import AssetType, BalanceAllowanceParams  # noqa: F401
            params = BalanceAllowanceParams(
                asset_type=asset_type,
                token_id=token_id or "",
                signature_type=int(self.cfg["live"]["signature_type"]),
            )
            with self._client_lock:
                self.client.update_balance_allowance(params)
        except Exception as e:  # noqa: BLE001
            log.debug("CLOB balance sync failed (%s %s): %s",
                      asset_type, (token_id or "")[:12], e)

    def _record_order_event(self, event: str, market: Market | None = None,
                            quote: Quote | None = None, side: str | None = None,
                            order_id: str | None = None,
                            reason: str | None = None,
                            audit: dict | None = None) -> None:
        """Emit one lifecycle record through the asynchronous runtime logger."""
        fields = [f"event={event}"]
        if market is not None:
            fields.append(f"market={market.question!r}")
        if quote is not None:
            fields.extend((f"price={quote.price:.3f}", f"size={quote.size:.2f}"))
        if side is not None:
            fields.append(f"side={side}")
        if order_id:
            fields.append(f"order_id={order_id}")
        if reason:
            fields.append(f"reason={reason!r}")
        log.info("ORDER %s", " ".join(fields))
        if audit is None and order_id:
            audit = LiveBroker._order_audit_context(self, order_id)
        row = {
            "event": event.lower(), "cid": market.condition_id if market else None,
            "market": market.question if market else None,
            "order_id": order_id, "fill_id": None, "trade_hash": None,
            "side": side, "token": quote.token_id if quote else None,
            "price": quote.price if quote else None, "size": quote.size if quote else None,
            "path": (audit or {}).get("path", "normal"),
            "unpaired_cost": (audit or {}).get("unpaired_cost"),
            "pair_cap": (audit or {}).get("pair_cap"),
            "expected_pair_pnl": (audit or {}).get("expected_pair_pnl"),
            "reason": reason,
        }
        getattr(self, "audit", AuditLogger(None)).record(row)
        if (event == "ORDER_PLACED" and market is not None and quote is not None
                and row["path"] == "inventory_recovery"
                and row["unpaired_cost"] is not None):
            try:
                basis = float(row["unpaired_cost"])
                if 0.0 < basis < 1.0:
                    getattr(self, "_recovery_basis_cache", {})[market.condition_id] = (
                        basis, quote.size)
            except (TypeError, ValueError):
                pass

    def _order_audit_context(self, order_id: str) -> dict:
        """Resolve the audit context (path, intent, etc.) for a fill.

        Searches live order books first; falls back to taker-order contexts and
        then to recently-cancelled orders so fills that outrace their cancel
        confirmations still carry the correct tags (e.g. normal_reward).
        """
        for orders in getattr(self, "_open_orders", {}).values():
            for ro in orders:
                if ro.order_id == order_id:
                    return ro.audit
        for ro in getattr(self, "_exit_orders", {}).values():
            if ro.order_id == order_id:
                return ro.audit
        for ro in getattr(self, "_reward_exit_orders", {}).values():
            if ro.order_id == order_id:
                return ro.audit
        ctx = getattr(self, "_taker_order_contexts", {}).get(order_id, None)
        if ctx is not None:
            return ctx
        completed = getattr(self, "_completed_order_contexts", {}).get(order_id, None)
        if completed is not None:
            return completed
        # Last resort: a late-side WebSocket fill that arrived after we already
        # cancelled the order (race between cancel and taker match at the CLOB).
        recent = getattr(self, "_recently_cancelled", {}).get(order_id, None)
        if recent is not None:
            return recent[0]
        return {}

    def _resting_order_context(self, order_id: str) -> tuple[Market | None, Quote | None, str | None]:
        for cid, orders in getattr(self, "_open_orders", {}).items():
            for ro in orders:
                if ro.order_id == order_id:
                    return getattr(self, "_markets", {}).get(cid), ro.quote, "BUY"
        for cid, ro in getattr(self, "_exit_orders", {}).items():
            if ro.order_id == order_id:
                return getattr(self, "_markets", {}).get(cid), ro.quote, "SELL"
        return None, None, None

    @staticmethod
    def _select_collateral(onchain: float | None, clob_cache: float | None) -> float | None:
        """On-chain pUSD is the source of truth for collateral; the CLOB cache
        is only a fallback. For deposit wallets the cache can be stale/zero, so
        never take max() of the two (a stale-high cache would inflate equity)."""
        if onchain is not None:
            return onchain
        return clob_cache

    def _place_buy(self, q: Quote) -> RestingOrder | None:
        from py_clob_client_v2 import OrderArgs, OrderType, Side

        try:
            with self._client_lock:
                expiration = self._gtd_expiration()
                signed = self.client.create_order(OrderArgs(
                    price=q.price, size=q.size, side=Side.BUY, token_id=q.token_id,
                    expiration=expiration,
                ))
                resp = self.client.post_order(signed, OrderType.GTD)
            oid = resp.get("orderID") or resp.get("orderId") or ""
            if oid:
                ro = RestingOrder(oid, q, time.time(), expiration)
                if self.metrics:
                    self.metrics.initialize_order_match_watermark(oid)
                self._record_order_event("ORDER_PLACED", quote=q, side="BUY", order_id=oid)
                return ro
        except Exception as e:  # noqa: BLE001
            log.error("订单提交失败（%s @ %.3f）：%s", q.token_id[:12], q.price, e)
            self._record_order_event("ORDER_POST_FAILED", quote=q, side="BUY", reason=str(e))
        return None

    def _place_sell(self, q: Quote,
                    audit_context: dict | None = None) -> RestingOrder | None:
        from py_clob_client_v2 import AssetType, OrderArgs, OrderType, Side
        from py_clob_client_v2 import BalanceAllowanceParams

        # Selling spends the conditional token; the CLOB must have a fresh view
        # of the deposit wallet's holding of it or it rejects with "balance: 0".
        self._sync_clob_balance(AssetType.CONDITIONAL, q.token_id)
        # Verify the CLOB's server-side cache actually reflects enough tokens.
        # Without this check, a stale on-chain balance from a recently settled
        # exit fill can overwrite the CLOB's correct cache via
        # _sync_clob_balance, causing a spurious "not enough balance" rejection
        # and a misleading RECOVERY_SELL_ORIGINAL_PLACED log in the caller.
        try:
            params = BalanceAllowanceParams(
                asset_type=AssetType.CONDITIONAL,
                token_id=q.token_id,
                signature_type=int(self.cfg["live"]["signature_type"]),
            )
            with self._client_lock:
                bal = self.client.get_balance_allowance(params)
            raw = float(bal.get("balance") or 0)
            needed = q.size * USDC_DECIMALS
            if raw < needed - 1e-9:
                log.warning(
                    "退出卖单跳过（%s @ %.3f size=%.0f）：CLOB 条件代币余额不足 "
                    "(balance=%.0f raw < needed=%.0f raw, ~%.3f shares available)",
                    q.token_id[:12], q.price, q.size, raw, needed,
                    raw / USDC_DECIMALS)
                return None
        except Exception as e:  # noqa: BLE001
            log.debug("无法读取 CLOB 条件代币余额（%s）：%s", q.token_id[:12], e)
            # Fall through — let the order attempt fail naturally

        try:
            with self._client_lock:
                expiration = self._gtd_expiration(self.exit_order_ttl)
                signed = self.client.create_order(OrderArgs(
                    price=q.price, size=q.size, side=Side.SELL, token_id=q.token_id,
                    expiration=expiration,
                ))
                resp = self.client.post_order(signed, OrderType.GTD)
            oid = resp.get("orderID") or resp.get("orderId") or ""
            if oid:
                ro = RestingOrder(oid, q, time.time(), expiration,
                                  audit_context or {})
                market = next((m for m in getattr(self, "_markets", {}).values()
                               if q.token_id in (m.yes_token, m.no_token)), None)
                self._record_order_event("ORDER_PLACED", market, q, "SELL", oid,
                                         audit=audit_context)
                return ro
        except Exception as e:  # noqa: BLE001
            log.error("退出订单提交失败（%s @ %.3f）：%s", q.token_id[:12], q.price, e)
            self._record_order_event("ORDER_POST_FAILED", quote=q, side="SELL", reason=str(e))
        return None

    def _batch_cancel(self, order_ids: list[str], *, reason: str | None = None,
                      audit_contexts: dict[str, dict] | None = None) -> bool:
        if not order_ids:
            return True

        # Initialised here (not in __init__) so test stubs that don't call
        # __init__ still get a safe default.
        if not hasattr(self, "_recently_cancelled"):
            self._recently_cancelled: dict[str, tuple[dict, float]] = {}

        def _do_cancel():
            with self._client_lock:
                self.client.cancel_orders(order_ids)

        try:
            _with_retry("batch cancel", _do_cancel)
            now = time.time()
            for oid in order_ids:
                market, quote, side = self._resting_order_context(oid)
                audit_ctx = (audit_contexts or {}).get(oid)
                self._record_order_event(
                    "ORDER_CANCELLED", market, quote, side, oid, reason=reason,
                    audit=audit_ctx)
                if audit_ctx:
                    self._recently_cancelled[oid] = (audit_ctx, now)
            # Prune entries older than 120 s
            stale = [k for k, (_, ts) in self._recently_cancelled.items()
                     if now - ts > 120]
            for k in stale:
                del self._recently_cancelled[k]
            return True
        except Exception as e:  # noqa: BLE001
            from py_clob_client_v2 import OrderPayload

            ok = True
            now = time.time()
            for oid in order_ids:
                try:
                    with self._client_lock:
                        self.client.cancel_order(OrderPayload(orderID=oid))
                    market, quote, side = self._resting_order_context(oid)
                    audit_ctx = (audit_contexts or {}).get(oid)
                    self._record_order_event(
                        "ORDER_CANCELLED", market, quote, side, oid, reason=reason,
                        audit=audit_ctx)
                    if audit_ctx:
                        self._recently_cancelled[oid] = (audit_ctx, now)
                except Exception as fallback_error:  # noqa: BLE001
                    ok = False
                    log.warning("撤单失败（%s）：%s", oid[:16], fallback_error)
                    market, quote, side = self._resting_order_context(oid)
                    self._record_order_event("ORDER_CANCEL_FAILED", market, quote,
                                             side, oid, str(fallback_error))
            return ok

    def set_quotes(self, market: Market, quotes: list[Quote],
                   audit_context: dict[str, dict] | None = None) -> None:
        from py_clob_client_v2 import (
            OrderArgs, OrderType, PartialCreateOrderOptions, PostOrdersV2Args, Side,
        )

        self._markets[market.condition_id] = market
        desired = {q.token_id: q for q in quotes}
        now = time.time()
        kept: list[RestingOrder] = []
        cancel_now: list[str] = []     # price/size changed or no longer wanted
        cancel_after: list[str] = []   # pure expiry refresh — cancel after repost
        refresh_audits: dict[str, dict] = {}

        for ro in self._open_orders.get(market.condition_id, []):
            d = desired.get(ro.quote.token_id)
            near_expiry = (ro.expiration > 0
                           and ro.expiration - now < GTD_REFRESH_MARGIN_SECS)
            is_recovery = (ro.audit or {}).get("recovery_order") or \
                          ((audit_context or {}).get(ro.quote.token_id, {})).get("recovery_order")
            if d is not None and d.key() == ro.quote.key():
                if not near_expiry or is_recovery:
                    # Recovery orders stay put even near expiry — their goal is
                    # queue depth, not reward scoring.  Price/size changes still
                    # replace them immediately.
                    kept.append(ro)
                    desired.pop(ro.quote.token_id)
                elif near_expiry and self.refresh_overlap:
                    # Same price/size, only expiring: leave it in `desired` so the
                    # replacement posts first, then cancel the old order below. No
                    # off-book gap, so the reward sampler always sees this side.
                    cancel_after.append(ro.order_id)
                    refresh_audits[ro.order_id] = (
                        (audit_context or {}).get(ro.quote.token_id, ro.audit))
            else:
                cancel_now.append(ro.order_id)

        if not self._batch_cancel(cancel_now):
            log.warning("“%s”撤单失败，先对账再提交替换订单",
                        market.question[:45])
            self.reconcile_orders()
            return

        placed: list[RestingOrder] = []
        if desired:
            batch_args = []
            order_map: list[tuple[Quote, int]] = []
            options = PartialCreateOrderOptions(neg_risk=market.neg_risk)
            for q in desired.values():
                try:
                    context = (audit_context or {}).get(q.token_id, {})
                    expiration = (self._gtd_expiration(LiveBroker._buy_order_ttl(self, context))
                                  if context.get("recovery_order")
                                  else self._gtd_expiration())
                    with self._client_lock:
                        signed = self.client.create_order(OrderArgs(
                            price=q.price, size=q.size, side=Side.BUY, token_id=q.token_id,
                            expiration=expiration,
                        ), options)
                    batch_args.append(PostOrdersV2Args(order=signed, orderType=OrderType.GTD))
                    order_map.append((q, expiration))
                except Exception as e:  # noqa: BLE001
                    log.error("构建订单失败（%s @ %.3f）：%s", q.token_id[:12], q.price, e)
                    self._record_order_event("ORDER_POST_FAILED", market, q, "BUY",
                                             reason=str(e))
            if batch_args:
                try:
                    # NOT retried: a POST that times out after the order landed
                    # server-side would double-post on retry. Reconcile from
                    # exchange truth instead (the overlap above keeps the old
                    # order resting meanwhile, so a failed refresh leaves no gap).
                    with self._client_lock:
                        resp = self.client.post_orders(batch_args)
                    orders = resp if isinstance(resp, list) else resp.get("orders", [resp])
                    for i, item in enumerate(orders):
                        if not isinstance(item, dict) or i >= len(order_map):
                            continue
                        oid = (item.get("orderID") or item.get("orderId")
                               or item.get("id") or "")
                        if oid:
                            q, expiration = order_map[i]
                            context = (audit_context or {}).get(q.token_id, {})
                            placed.append(RestingOrder(
                                oid, q, now, expiration, context))
                            if self.metrics:
                                self.metrics.initialize_order_match_watermark(oid)
                            self._record_order_event("ORDER_PLACED", market, q, "BUY", oid,
                                                     audit=context)
                        else:
                            # POST /orders returns a 200 array even for per-order
                            # REJECTIONS: orderID comes back empty with the reason
                            # in errorMsg. Surface it — a swallowed empty orderID is
                            # why a quote never rests (and rewards never accrue).
                            q, _ = order_map[i]
                            reason = (item.get("errorMsg") or item.get("error")
                                      or "empty orderID (no reason given)")
                            log.warning("订单被拒绝（%s @ %.3f×%.0f，市场“%s”）：%s",
                                        q.token_id[:12], q.price, q.size,
                                        market.question[:35], reason)
                            self._record_order_event("ORDER_POST_FAILED", market, q,
                                                     "BUY", reason=reason)
                except Exception as e:  # noqa: BLE001
                    log.error("批量提交失败，先对账而不盲目重试：%s", e)
                    for q, _ in order_map:
                        self._record_order_event("ORDER_POST_FAILED", market, q,
                                                 "BUY", reason=str(e))
                    self.reconcile_orders()
                    return
                if len(placed) < len(batch_args):
                    # One or more legs did not rest (rejected above, or no ID
                    # returned). Rebuild from exchange truth so we neither trust
                    # stale local state nor re-post duplicates next cycle.
                    if not placed and not self._logged_post_shape:
                        # One-shot: if we couldn't parse ANY id, dump the response
                        # shape so an unexpected wrapper is diagnosable from logs.
                        self._logged_post_shape = True
                        keys = sorted(resp.keys()) if isinstance(resp, dict) else type(resp).__name__
                        log.warning("post_orders 未返回可解析的订单 ID；"
                                    "response shape: %s", keys)
                    self.reconcile_orders()
                    return

        # Replacements are now resting; close the overlap by cancelling the
        # expiring originals. Deferred to here (after a successful post) so a
        # failed post never leaves a side off the book — the old order stays.
        if cancel_after and not self._batch_cancel(
                cancel_after, reason="expiry_refresh", audit_contexts=refresh_audits):
            log.warning("刷新重叠：“%s”的旧订单撤销失败，"
                        "reconciling", market.question[:45])
            self.reconcile_orders()
            return

        self._open_orders[market.condition_id] = kept + placed
        if self.metrics:
            self.metrics.record_quotes(market.condition_id, quotes)

    def cancel_all(self, exclude_cids: set[str] | None = None) -> None:
        exclude = exclude_cids or set()
        # Gather order ids for all markets except excluded ones.
        ids = [ro.order_id
               for cid, orders in self._open_orders.items()
               if cid not in exclude
               for ro in orders]
        exit_ids = [ro.order_id
                    for cid, ro in self._exit_orders.items()
                    if cid not in exclude]
        all_ids = ids + exit_ids
        ok = True
        try:
            with self._client_lock:
                if all_ids:
                    self.client.cancel_orders(all_ids)
        except Exception as e:  # noqa: BLE001
            ok = False
            log.error("撤销全部订单失败：%s", e)
            self._record_order_event("ORDER_CANCEL_ALL", reason=str(e))
        if ok:
            self._record_order_event("ORDER_CANCEL_ALL")
            # Only clear the excluded cids' orders.
            for cid in list(self._open_orders):
                if cid not in exclude:
                    self._open_orders.pop(cid, None)
            for cid in list(self._exit_orders):
                if cid not in exclude:
                    self._exit_orders.pop(cid, None)
        else:
            self.reconcile_orders()

    def cancel_quotes(self, exclude_cids: set[str] | None = None) -> None:
        ids = [ro.order_id
               for cid, orders in self._open_orders.items()
               if cid not in (exclude_cids or set())
               for ro in orders]
        if self._batch_cancel(ids):
            for cid in list(self._open_orders):
                if cid not in (exclude_cids or set()):
                    self._open_orders.pop(cid, None)
        else:
            self.reconcile_orders()

    def cancel_quotes_for_market(self, market: Market) -> bool:
        cid = market.condition_id
        ids = [ro.order_id for ro in self._open_orders.get(cid, [])]
        if self._batch_cancel(ids):
            self._open_orders.pop(cid, None)
            return True
        else:
            self.reconcile_orders()
            return False

    def open_quotes(self, market: Market) -> list[Quote]:
        return [ro.quote for ro in self._open_orders.get(market.condition_id, [])]

    def due_for_refresh(self, market: Market) -> bool:
        """True when a resting order is close enough to GTD expiry that it must
        be reposted now even if its price/size is unchanged. The quote loop only
        calls set_quotes on a key-set change, so without this a stable quote is
        never refreshed and silently expires on the book."""
        now = time.time()
        return any(
            ro.expiration > 0 and ro.expiration - now < GTD_REFRESH_MARGIN_SECS
            for ro in self._open_orders.get(market.condition_id, [])
        )

    def set_exit(self, market: Market, quote: Quote | None) -> bool:
        """Place a reduce-only limit SELL (GTD), or cancel an existing one when
        quote is None. Returns True when the sell order was successfully placed
        or successfully cancelled (or was already absent). Returns False when
        the order could not be placed (e.g. balance too low) — callers should
        NOT record a "placed" log entry on False for new placements."""
        cid = market.condition_id
        cur = self._exit_orders.get(cid)
        now = time.time()
        if (cur is not None and quote is not None and cur.quote.key() == quote.key()
                and cur.expiration - now >= GTD_REFRESH_MARGIN_SECS):
            return True  # unchanged resting order
        if cur is not None:
            if self._batch_cancel([cur.order_id]):
                self._exit_orders.pop(cid, None)
            else:
                self.reconcile_orders()
                return False
        if quote is None:
            return True  # cancelled successfully, or nothing to cancel
        self._markets[cid] = market
        ro = self._place_sell(quote)
        if ro:
            self._exit_orders[cid] = ro
            log.info("退出卖单已挂出：“%s” %.0f 股 @ %.3f",
                     market.question[:40], quote.size, quote.price)
            return True
        return False  # _place_sell logged the specific reason

    def place_reward_exit(self, market: Market, batch_id: str,
                          quote: Quote, audit_context: dict) -> RestingOrder | None:
        self._markets[market.condition_id] = market
        context = {**audit_context, "batch_id": batch_id, "intent": "batch_exit"}
        ro = self._place_sell(quote, context)
        if ro:
            self._reward_exit_orders[batch_id] = ro
        return ro

    def cancel_reward_exit(self, batch_id: str) -> bool:
        ro = self._reward_exit_orders.get(batch_id)
        if ro is None:
            return True
        ok = self._batch_cancel([ro.order_id], reason="reward_exit_replace")
        if ok:
            self._reward_exit_orders.pop(batch_id, None)
        return ok

    def exit_quote(self, market: Market) -> Quote | None:
        cur = self._exit_orders.get(market.condition_id)
        return cur.quote if cur else None

    def _register_pending_hedge(self, market: Market, token_id: str, size: float) -> None:
        """Reserve a taker hedge until a REST snapshot contains the fill."""
        with self._state_lock:
            self._pending_hedges[market.condition_id] = PendingHedge(
                token_id=token_id,
                size=size,
                target_token_shares=self._token_shares.get(token_id, 0.0) + size,
                created_ts=time.time(),
            )

    def has_pending_hedge(self, condition_id: str) -> bool:
        with self._state_lock:
            return condition_id in self._pending_hedges

    def _effective_position(self, market: Market) -> dict:
        """Return REST position plus the one unconfirmed taker hedge, if any."""
        with self._state_lock:
            d = dict(self._positions.get(
                market.condition_id, {"yes": 0.0, "no": 0.0, "value": 0.0}))
            pending = self._pending_hedges.get(market.condition_id)
            if pending is not None:
                key = "yes" if pending.token_id == market.yes_token else "no"
                d[key] = d.get(key, 0.0) + pending.size
            return d

    def _apply_position_snapshot(self, positions: dict[str, dict],
                                 token_shares: dict[str, float]) -> None:
        """Atomically replace REST state and retire only observed hedge versions."""
        with self._state_lock:
            for cid, pending in list(self._pending_hedges.items()):
                observed = token_shares.get(pending.token_id, 0.0)
                if observed >= pending.target_token_shares - 0.01:
                    self._pending_hedges.pop(cid, None)
            self._positions = positions
            self._token_shares = dict(token_shares)

    def taker_buy(self, market: Market, token_id: str, size: float, max_price: float,
                  audit_context: dict | None = None) -> float:
        from py_clob_client_v2 import (
            AssetType, MarketOrderArgsV2, OrderType,
            PartialCreateOrderOptions, Side,
        )

        size = round(size, 2)
        if size <= 0 or max_price <= 0:
            return 0.0

        # FAK overfill prevention: using amount = max_price × size can
        # overshoot when the book has ask prices well below max_price
        # (e.g. price=0.99, size=88 → amount=$87.12, but fills at
        # ask=0.51 → 170+ shares instead of 88).  Cap the spend budget
        # at best_ask × remaining so we never buy more than requested.
        context = audit_context or {}
        best_ask = context.get("best_ask")
        effective_price = (best_ask if best_ask is not None and 0 < best_ask <= max_price
                           else max_price)
        amount = round(effective_price * size, 2)
        # Deposit wallets (signature_type 3) need a CLOB balance sync before
        # placing orders, otherwise the CLOB may reject with "not enough
        # balance" even though on-chain collateral is sufficient.
        self._sync_clob_balance(AssetType.COLLATERAL)
        try:
            with self._state_lock:
                with self._client_lock:
                    signed = self.client.create_market_order(MarketOrderArgsV2(
                        token_id=token_id, amount=amount, side=Side.BUY,
                        price=max_price, order_type=OrderType.FAK,
                    ), PartialCreateOrderOptions(neg_risk=market.neg_risk))
                    resp = self.client.post_order(signed, OrderType.FAK)
                filled = min(_parse_fill_amount(resp, size), size)
                order_id = (resp.get("orderID") or resp.get("orderId") or resp.get("id")
                            or "") if isinstance(resp, dict) else ""
                context = {"path": "forced_hedge", **(audit_context or {})}
                if order_id:
                    contexts = getattr(self, "_taker_order_contexts", None)
                    if contexts is None:
                        contexts = self._taker_order_contexts = {}
                    contexts[order_id] = context
                getattr(self, "audit", AuditLogger(None)).record({
                    "event": "order_placed", "cid": market.condition_id,
                    "market": market.question, "order_id": order_id or None,
                    "fill_id": None, "trade_hash": None, "side": "BUY",
                    "token": token_id, "price": max_price, "size": size,
                    "path": context["path"],
                    "unpaired_cost": context.get("unpaired_cost"),
                    "pair_cap": context.get("pair_cap"),
                    "expected_pair_pnl": context.get("expected_pair_pnl"),
                })
                if filled > 0:
                    if context.get("intent") != "batch_take":
                        LiveBroker._register_pending_hedge(self, market, token_id, filled)
                    _notify_live_fill(
                        getattr(self, "notifier", None),
                        {
                            "ts": time.time(), "cid": market.condition_id,
                            "market": market.question[:50],
                            "side": "YES" if token_id == market.yes_token else "NO",
                            "token": token_id, "price": max_price, "size": filled,
                            "taker": True,
                        },
                        "BUY",
                    )
                    with self._state_lock:
                        pending = self._pending_hedges.get(market.condition_id)
                        if pending is not None:
                            pending.notified = True
        except Exception as e:  # noqa: BLE001
            log.error("吃单订单失败（%s @ %.3f）：%s", token_id[:12], max_price, e)
            return 0.0
        if filled > 0 and self.metrics:
            self.metrics.record_hedge(market.condition_id, max_price, filled)
        return filled

    def _apply_fill_to_orders(self, token_id: str, size: float, side: str,
                              order_id: str | None = None) -> None:
        """Decrement resting orders on fill."""
        if side == "BUY":
            for cid, orders in list(self._open_orders.items()):
                new_orders = []
                for ro in orders:
                    if ro.quote.token_id != token_id or size <= 0:
                        new_orders.append(ro)
                        continue
                    order_size = ro.quote.size
                    remaining = order_size - size
                    if remaining > 0.01:
                        ro.quote = Quote(token_id, ro.quote.price, remaining)
                        new_orders.append(ro)
                    size = max(0.0, size - order_size)
                self._open_orders[cid] = new_orders
        elif side == "SELL":
            for cid, ro in list(self._exit_orders.items()):
                if (ro.quote.token_id == token_id
                        and (not order_id or ro.order_id == order_id)):
                    remaining = ro.quote.size - size
                    if remaining <= 0.01:
                        self._exit_orders.pop(cid, None)
                    else:
                        ro.quote = Quote(token_id, ro.quote.price, remaining)
            for batch_id, ro in list(self._reward_exit_orders.items()):
                if (ro.quote.token_id == token_id
                        and order_id and ro.order_id == order_id):
                    remaining = ro.quote.size - size
                    if remaining <= 0.01:
                        self._reward_exit_orders.pop(batch_id, None)
                    else:
                        ro.quote = Quote(token_id, ro.quote.price, remaining)

    def record_user_fill(self, token_id: str, side: str, price: float,
                         size: float, taker: bool = False, order_id: str | None = None,
                         fill_id: str | None = None, trade_hash: str | None = None,
                         fee_usd: float | None = None,
                         matched_total: float | None = None,
                         match_observed_ts: float | None = None,
                         event_ts: float | None = None) -> None:
        if size <= 0:
            return
        ts = time.time()
        market = next(
            (m for m in self._markets.values()
             if token_id in (m.yes_token, m.no_token)), None)
        if market is None:
            return
        key = "yes" if token_id == market.yes_token else "no"
        # Resolve order intent before applying the fill: a fully-filled maker
        # order is removed from _open_orders by _apply_fill_to_orders().
        context = LiveBroker._order_audit_context(self, order_id) if order_id else {}
        if not context and not taker:
            # Fallback: order_id may be missing from the WebSocket event
            # (Polymarket maker_orders sometimes omit it).  Scan open BUY
            # orders by token_id to recover the audit context so that
            # reward-exit batch detection (which depends on path/intent)
            # still works.
            for orders in getattr(self, "_open_orders", {}).values():
                for ro in orders:
                    if ro.quote.token_id == token_id:
                        context = ro.audit
                        break
                if context:
                    break
        if taker and not context:
            context = {"path": "forced_hedge"}
        if order_id and context:
            if not hasattr(self, "_completed_order_contexts"):
                self._completed_order_contexts = {}
            self._completed_order_contexts[order_id] = context
        if not fill_id:
            if trade_hash:
                fill_id = f"trade-{trade_hash}:{order_id or token_id}:{side}"
            elif context.get("intent") == "normal_reward":
                if not order_id or matched_total is None:
                    if order_id:
                        if not hasattr(self, "_pending_unidentified_reward_fills"):
                            self._pending_unidentified_reward_fills = {}
                        self._pending_unidentified_reward_fills[order_id] = {
                            "token_id": token_id, "side": side, "price": price,
                            "size": size, "fee_usd": fee_usd,
                            "event_ts": event_ts or ts,
                        }
                    log.warning(
                        "REWARD_EXIT_UNIDENTIFIED_FILL_HELD order_id=%s: "
                        "缺少累计成交份额，未触发 take",
                        order_id or "unknown",
                    )
                    return
                if match_observed_ts is not None and event_ts is not None and \
                        match_observed_ts + 1e-6 < event_ts:
                    if not hasattr(self, "_pending_unidentified_reward_fills"):
                        self._pending_unidentified_reward_fills = {}
                    self._pending_unidentified_reward_fills[order_id] = {
                        "token_id": token_id, "side": side, "price": price,
                        "size": size, "fee_usd": fee_usd, "event_ts": event_ts,
                    }
                    log.warning(
                        "REWARD_EXIT_UNIDENTIFIED_FILL_HELD order_id=%s: "
                        "累计成交份额早于本次成交，未触发 take", order_id,
                    )
                    return
                claim = getattr(self.metrics, "claim_unidentified_order_match", None)
                added = claim(order_id, matched_total) if claim else None
                if added is None:
                    log.warning(
                        "REWARD_EXIT_UNIDENTIFIED_FILL_HELD order_id=%s: "
                        "订单没有安全水位，未触发 take", order_id,
                    )
                    return
                if added <= 1e-9:
                    log.info("REWARD_EXIT_UNIDENTIFIED_FILL_DUPLICATE order_id=%s "
                             "matched_total=%.8f", order_id, matched_total)
                    return
                size = added
                fill_id = f"order-match:{order_id}:{matched_total:.8f}"
            else:
                fill_id = f"live-{uuid.uuid4().hex}"
        delta = size if side == "BUY" else -size
        is_batch_take = context.get("intent") == "batch_take"
        with self._state_lock:
            pending = self._pending_hedges.get(market.condition_id)
            pending_part = 0.0
            if (pending is not None and not is_batch_take and side == "BUY"
                    and pending.token_id == token_id):
                pending_part = min(size, max(0.0, pending.size - pending.ws_observed))
                pending.ws_observed += pending_part
            # taker_buy 已发送即时通知则跳过，避免同一笔 FAK 对冲重复推送
            skip_notify = pending is not None and not is_batch_take and pending.notified
            local_delta = delta - pending_part
            if abs(local_delta) > 1e-9:
                self._token_shares[token_id] = max(
                    0.0, self._token_shares.get(token_id, 0.0) + local_delta)
                with self._ws_deltas_lock:
                    self._ws_deltas.append(
                        (ts, token_id, local_delta, side, price, market.condition_id))
                d = self._positions.setdefault(
                    market.condition_id, {"yes": 0.0, "no": 0.0, "value": 0.0})
                d[key] = max(0.0, d[key] + local_delta)
        if not taker:
            self._apply_fill_to_orders(token_id, size, side, order_id)
        entry = {
            "ts": ts, "cid": market.condition_id,
            "market": market.question[:50],
            "side": "YES" if key == "yes" else "NO",
            "token": token_id, "price": price, "size": size,
        }
        if taker:
            entry["taker"] = True
        if side == "SELL":
            entry["exit"] = True
        # Carry the path and fill_id so the reward-exit batch controller can
        # identify normal reward fills vs recovery/hedge fills.
        path = context.get("path", "")
        intent = context.get("intent") or {
            "normal": "normal_reward",
            "forced_hedge": "forced_hedge",
            "reward_exit_take": "batch_take",
            "reward_exit_exit": "batch_exit",
        }.get(path, "")
        if fee_usd is None and taker and market is not None:
            rate = market.fee_bps / 10000.0
            fee_usd = rate * (price * (1.0 - price)) ** market.fee_exponent * size
        if context.get("path"):
            entry["path"] = context["path"]
        if intent:
            entry["intent"] = intent
        if context.get("batch_id"):
            entry["batch_id"] = context["batch_id"]
        if fee_usd is not None and fee_usd > 0:
            entry["fee"] = fee_usd
        if order_id:
            entry["order_id"] = order_id
        entry["fill_id"] = fill_id
        self.fills_log.append(entry)
        self.fills_log = self.fills_log[-500:]
        if self.metrics:
            self.metrics.record_fill(entry)
            if intent in {"normal_reward"} and \
                    hasattr(self.metrics, "record_reward_exit_fill"):
                self.metrics.record_reward_exit_fill(
                    fill_id=str(entry.get("fill_id") or
                               f"live-{uuid.uuid4().hex}"),
                    batch_id=str(entry.get("batch_id") or ""),
                    order_id=str(entry.get("order_id") or ""),
                    intent=intent, cid=market.condition_id,
                    token_id=token_id, side=entry["side"], price=price, size=size,
                    fee_usd=float(entry.get("fee") or 0.0), ts=ts)
        log.info("LIVE FILL（WebSocket）：%s %s %s %.1f 股 @ %.3f",
                 market.question[:40], side, entry["side"], size, price)
        getattr(self, "audit", AuditLogger(None)).record({
            "event": "ws_fill", "cid": market.condition_id, "market": market.question,
            "order_id": order_id, "fill_id": fill_id, "trade_hash": trade_hash,
            "side": side, "token": token_id, "price": price, "size": size,
            "path": context.get("path", "unknown"),
            "unpaired_cost": context.get("unpaired_cost"),
            "pair_cap": context.get("pair_cap"),
            "expected_pair_pnl": context.get("expected_pair_pnl"),
        })
        if not skip_notify:
            _notify_live_fill(getattr(self, "notifier", None), entry, side)

    def observe_order_match(self, order_id: str, token_id: str, side: str,
                            price: float, matched_total: float,
                            observed_ts: float) -> None:
        """Cache exchange cumulative maker shares for a following no-id trade."""
        if not order_id or matched_total < 0:
            return
        if not hasattr(self, "_order_match_snapshots"):
            self._order_match_snapshots = {}
        self._order_match_snapshots[order_id] = (matched_total, observed_ts)
        pending = getattr(self, "_pending_unidentified_reward_fills", {}).get(order_id)
        if pending and observed_ts + 1e-6 >= pending["event_ts"]:
            self._pending_unidentified_reward_fills.pop(order_id, None)
            LiveBroker.record_user_fill(
                self,
                pending["token_id"], pending["side"], pending["price"], pending["size"],
                order_id=order_id, fee_usd=pending["fee_usd"],
                matched_total=matched_total, match_observed_ts=observed_ts,
                event_ts=pending["event_ts"],
            )

    def _record_inferred_maker_fill(
            self, market: Market, token_id: str, size: float, ts: float,
    ) -> dict | None:
        """Persist a poll-inferred normal maker fill so batch detection can replay it."""
        order = next(
            (ro for ro in self._open_orders.get(market.condition_id, [])
             if ro.quote.token_id == token_id and ro.audit.get("intent") == "normal_reward"),
            None,
        )
        if order is None:
            return None
        entry = {
            "ts": ts, "cid": market.condition_id, "market": market.question[:50],
            "side": "YES" if token_id == market.yes_token else "NO",
            "token": token_id, "price": order.quote.price, "size": size,
            "path": "normal", "intent": "normal_reward",
            "order_id": order.order_id,
            "fill_id": f"inferred-{uuid.uuid4().hex}",
            "inferred": True,
        }
        self.fills_log.append(entry)
        if self.metrics:
            self.metrics.record_fill(entry)
            if hasattr(self.metrics, "record_reward_exit_fill"):
                self.metrics.record_reward_exit_fill(
                    fill_id=entry["fill_id"], batch_id="", order_id=order.order_id,
                    intent="normal_reward", cid=market.condition_id,
                    token_id=token_id, side=entry["side"], price=order.quote.price,
                    size=size, fee_usd=0.0, ts=ts,
                )
        return entry

    def reconcile_orders(self) -> bool:
        """Rebuild local order state from exchange truth."""
        def _do_fetch():
            with self._client_lock:
                return self.client.get_open_orders()

        try:
            remote = _with_retry("order reconcile", _do_fetch)
        except Exception as e:  # noqa: BLE001
            log.warning("订单对账失败：%s", e)
            return False
        old_audits = {
            ro.order_id: ro.audit
            for orders in self._open_orders.values() for ro in orders
        }
        by_cid: dict[str, list[RestingOrder]] = {}
        exit_by_cid: dict[str, RestingOrder] = {}
        for o in remote:
            oid = o.get("id") or o.get("orderID") or o.get("orderId") or ""
            token = str(o.get("asset_id") or o.get("assetId") or "")
            if not oid or not token:
                continue
            market = next(
                (m for m in self._markets.values()
                 if token in (m.yes_token, m.no_token)), None)
            if market is None:
                continue
            try:
                price = float(o.get("price") or 0)
                orig = float(o.get("original_size") or o.get("size") or 0)
                matched = float(o.get("size_matched") or o.get("sizeMatched") or 0)
                remaining = max(0.0, orig - matched)
            except (TypeError, ValueError):
                continue
            if remaining <= 0:
                continue
            side = str(o.get("side") or "").upper()
            exp = int(o.get("expiration") or 0)
            ro = RestingOrder(oid, Quote(token, price, remaining), time.time(), exp,
                              old_audits.get(oid, {}))
            cid = market.condition_id
            if side == "SELL":
                exit_by_cid[cid] = ro
            else:
                by_cid.setdefault(cid, []).append(ro)
        self._open_orders = by_cid
        self._exit_orders = exit_by_cid
        return True

    def refresh_state(self) -> bool:
        import httpx

        poll_start = time.time()
        try:
            resp = httpx.get(
                f"{self.DATA_API}/positions",
                params={"user": self.address, "limit": 500},
                timeout=10.0,
            )
            resp.raise_for_status()
            rows = resp.json()
        except Exception as e:  # noqa: BLE001
            log.warning("position refresh failed: %s", e)
            return False

        positions: dict[str, dict] = {}
        token_shares: dict[str, float] = {}
        for r in rows:
            try:
                size = float(r.get("size") or 0)
                if size <= 0:
                    continue
                cid = str(r.get("conditionId") or "")
                token = str(r.get("asset") or "")
                cur = float(r.get("curPrice") or 0)
                outcome_index = int(r.get("outcomeIndex") or 0)
            except (TypeError, ValueError):
                continue
            m = self._markets.get(cid)
            is_yes = (token == m.yes_token) if m else (outcome_index == 0)
            d = positions.setdefault(cid, {"yes": 0.0, "no": 0.0, "value": 0.0,
                                           "yes_cost": 0.0, "no_cost": 0.0,
                                           "yes_cost_shares": 0.0, "no_cost_shares": 0.0})
            key = "yes" if is_yes else "no"
            d[key] += size
            try:
                avg = float(r.get("avgPrice"))
            except (TypeError, ValueError):
                avg = None
            if avg is not None and avg > 0:
                d[f"{key}_cost"] += size * avg
                d[f"{key}_cost_shares"] += size
            d["value"] += size * cur
            token_shares[token] = token_shares.get(token, 0.0) + size

        self._hydrate_held_markets(set(positions))

        if self._synced and not self.ws_fills_active:
            now = time.time()
            for token, shares in token_shares.items():
                gained = shares - self._token_shares.get(token, 0.0)
                if gained <= 1e-9:
                    continue
                market = next(
                    (m for m in self._markets.values()
                     if token in (m.yes_token, m.no_token)), None)
                if market is None:
                    continue
                entry = self._record_inferred_maker_fill(market, token, gained, now)
                if entry is None:
                    entry = {
                        "ts": now, "cid": market.condition_id,
                        "market": market.question[:50],
                        "side": "YES" if token == market.yes_token else "NO",
                        "token": token, "size": gained, "inferred": True,
                    }
                    for ro in self._open_orders.get(market.condition_id, []):
                        if ro.quote.token_id == token:
                            entry["price"] = ro.quote.price
                            break
                    self.fills_log.append(entry)
                    if self.metrics:
                        self.metrics.record_fill(entry)
                    log.warning(
                        "REWARD_EXIT_INFERRED_FILL_UNATTRIBUTED cid=%s token=%s size=%.1f "
                        "说明=断线轮询发现增仓但无法证明来自普通奖励挂单，未创建奖励退出批次",
                        market.condition_id, token[:12], gained,
                    )
                log.info("LIVE FILL detected %s %s +%.1f shares",
                         market.question[:40],
                         "YES" if token == market.yes_token else "NO", gained)
                skip_notify = False
                if "price" in entry:
                    with self._state_lock:
                        pending = self._pending_hedges.get(market.condition_id)
                        if pending is not None and pending.notified:
                            skip_notify = True
                    if not skip_notify:
                        _notify_live_fill(getattr(self, "notifier", None), entry, "BUY")
        self.fills_log = self.fills_log[-500:]

        post_poll: dict[str, float] = {}
        post_poll_cid: dict[tuple[str, str], float] = {}
        with self._ws_deltas_lock:
            while self._ws_deltas and self._ws_deltas[0][0] <= poll_start:
                self._ws_deltas.popleft()
            deltas = list(self._ws_deltas)
        for ts, token, delta, _side, _price, cid in deltas:
            if ts <= poll_start:
                continue
            post_poll[token] = post_poll.get(token, 0.0) + delta
            m = self._markets.get(cid)
            if m:
                key = "yes" if token == m.yes_token else "no"
                post_poll_cid[(cid, key)] = post_poll_cid.get((cid, key), 0.0) + delta

        for (cid, key), delta in post_poll_cid.items():
            d = positions.setdefault(cid, {"yes": 0.0, "no": 0.0, "value": 0.0})
            d[key] = max(0.0, d[key] + delta)
        for token, delta in post_poll.items():
            token_shares[token] = max(0.0, token_shares.get(token, 0.0) + delta)

        self._apply_position_snapshot(positions, token_shares)

        self._synced = True

        if poll_start - self._last_order_reconcile >= ORDER_RECONCILE_SECONDS:
            self._last_order_reconcile = poll_start
            self.reconcile_orders()

        # On-chain pUSD held by the wallet is the source of truth for collateral
        # (and thus equity, sizing, and loss limits). The CLOB's balance cache is
        # only used as a fallback: for deposit wallets it can be stale/zero unless
        # we push it an update, and taking max() of a stale-high cache and the
        # real balance inflates equity. We still push the cache an update so the
        # CLOB admits our BUY orders.
        from py_clob_client_v2 import AssetType, BalanceAllowanceParams
        self._sync_clob_balance(AssetType.COLLATERAL)
        onchain = clob_cache = None
        try:
            onchain = self._erc20_balance(PUSD, self.address)
        except Exception as e:  # noqa: BLE001
            log.warning("pUSD balance refresh failed: %s", e)
        try:
            with self._client_lock:
                bal = self.client.get_balance_allowance(
                    BalanceAllowanceParams(
                        asset_type=AssetType.COLLATERAL,
                        signature_type=int(self.cfg["live"]["signature_type"]),
                    )
                )
            clob_cache = float(bal.get("balance") or 0) / USDC_DECIMALS
        except Exception as e:  # noqa: BLE001
            log.warning("CLOB balance refresh failed: %s", e)
        picked = self._select_collateral(onchain, clob_cache)
        if picked is not None:
            self._collateral = picked
        return True

    def _yes_mid(self, market: Market) -> float | None:
        book = self.tracker.books.get(market.yes_token)
        return book.mid if book else None

    def position_tokens(self) -> list[str]:
        tokens = []
        with self._state_lock:
            cids = set(self._positions) | set(self._pending_hedges)
        for cid in cids:
            m = self._markets.get(cid)
            if m:
                tokens.extend((m.yes_token, m.no_token))
        return tokens

    def _hydrate_held_markets(self, condition_ids: set[str]) -> list[Market]:
        """Resolve held markets so the usual inventory controls can manage them."""
        added: list[Market] = []
        unresolved: set[str] = set()
        for cid in condition_ids:
            position = self._positions.get(cid, {})
            net_shares = abs(float(position.get("yes", 0.0))
                             - float(position.get("no", 0.0)))
            if net_shares < 5:
                continue
            if cid in self._markets:
                continue
            market = gamma.fetch_market(cid)
            if market is None:
                unresolved.add(cid)
                continue
            self._markets[cid] = market
            added.append(market)
            log.warning("adopting held position market '%s' into inventory management",
                        market.question[:60])
        self._unmanaged_position_cids = unresolved
        return added

    def equity(self) -> float:
        if self._collateral != self._collateral or not self._synced:
            return float("nan")
        total = self._collateral
        for cid, d in self._positions.items():
            m = self._markets.get(cid)
            mid = self._yes_mid(m) if m else None
            if mid is not None:
                total += d["yes"] * mid + d["no"] * (1 - mid)
            else:
                total += d["value"]
        return total

    def net_yes_exposure_usd(self, market: Market) -> float:
        d = LiveBroker._effective_position(self, market)
        mid = LiveBroker._yes_mid(self, market)
        if mid is None:
            mid = 0.5
        return d["yes"] * mid - d["no"] * (1 - mid)

    def unpaired_shares(self, market: Market) -> float:
        d = LiveBroker._effective_position(self, market)
        return d["yes"] - d["no"]

    def unpaired_cost_basis(self, market: Market) -> float | None:
        """Average entry price of the excess leg, if supplied by the Data API."""
        d = LiveBroker._effective_position(self, market)
        key = "yes" if d["yes"] > d["no"] else "no"
        shares = float(d.get(f"{key}_cost_shares") or 0.0)
        if shares > 0:
            return float(d.get(f"{key}_cost") or 0.0) / shares
        cached = getattr(self, "_recovery_basis_cache", {}).get(market.condition_id)
        unpaired = abs(float(d.get("yes") or 0.0) - float(d.get("no") or 0.0))
        if cached is not None and abs(cached[1] - unpaired) <= 1e-9:
            return cached[0]
        # Fallback: Data API omitted cost fields for this market's excess leg.
        # Estimate the average entry price from our own fill log so recovery
        # and forced hedging are not blocked by missing upstream data.
        excess_side = "YES" if key == "yes" else "NO"
        total_cost = 0.0
        total_shares = 0.0
        for entry in self.fills_log:
            if (entry.get("cid") == market.condition_id
                    and entry.get("side") == excess_side
                    and not entry.get("exit")
                    and "price" in entry
                    and "size" in entry):
                total_cost += float(entry["price"]) * float(entry["size"])
                total_shares += float(entry["size"])
        if total_shares > 0:
            basis = total_cost / total_shares
            if 0.0 < basis < 1.0:
                return basis
        return None

    def held_markets(self) -> list[Market]:
        with self._state_lock:
            cids = set(self._positions) | set(self._pending_hedges)
        return [self._markets[cid] for cid in cids if cid in self._markets]

    def position_shares(self, market: Market) -> tuple[float, float]:
        """Return (yes_shares, no_shares) for the market, or (0, 0)."""
        d = LiveBroker._effective_position(self, market)
        return (float(d.get("yes", 0.0)), float(d.get("no", 0.0)))

    def last_fill_ts(self, cid: str) -> float | None:
        """Timestamp of the most recent fill for this market, if any.

        Only returns timestamps from confirmed fills (entries with a price),
        not from Data API inferred position changes.
        """
        for entry in reversed(self.fills_log):
            if entry.get("cid") == cid and "price" in entry:
                return float(entry["ts"])
        return getattr(self, "unpaired_since", {}).get(cid)

    def _load_persisted(self) -> None:
        """Restore unpaired_since so escalation windows survive restarts."""
        try:
            if not self._live_data_path.exists():
                return
            data = json.loads(self._live_data_path.read_text())
            self.unpaired_since = {
                str(k): float(v) for k, v in data.get("unpaired_since", {}).items()
            }
            log.info("loaded %d unpaired_since entries from %s",
                     len(self.unpaired_since), self._live_data_path)
        except (json.JSONDecodeError, OSError, ValueError) as e:
            log.warning("could not load live_state.json: %s", e)

    def _persist_unpaired_since(self) -> None:
        """Persist unpaired_since so escalate windows survive restarts."""
        try:
            self._live_data_path.write_text(json.dumps({
                "unpaired_since": self.unpaired_since,
            }, indent=2))
        except OSError as e:
            log.warning("could not persist live_state.json: %s", e)

    def total_inventory_usd(self) -> float:
        managed = LiveBroker.held_markets(self)
        managed_cids = {m.condition_id for m in managed}
        with self._state_lock:
            unmanaged_value = sum(
                abs(float(d.get("value") or 0.0))
                for cid, d in self._positions.items()
                if cid not in managed_cids
            )
        return (sum(abs(LiveBroker.net_yes_exposure_usd(self, m)) for m in managed)
                + unmanaged_value)

    def merge_pairs(self, min_pairs: float) -> None:
        if self.merger is None or self.merger.disabled or not self._synced:
            return
        for cid, d in list(self._positions.items()):
            pairs = float(int(min(d["yes"], d["no"])))
            if pairs < min_pairs:
                continue
            m = self._markets.get(cid)
            if m is None:
                continue
            log.info("merging %.0f pairs in '%s' (recovers $%.0f)",
                     pairs, m.question[:40], pairs)
            if self.merger.merge(m.condition_id, m.neg_risk, pairs,
                                 audit_context={"market": m.question}):
                d["yes"] -= pairs
                d["no"] -= pairs
                self._token_shares[m.yes_token] = max(
                    0.0, self._token_shares.get(m.yes_token, 0.0) - pairs)
                self._token_shares[m.no_token] = max(
                    0.0, self._token_shares.get(m.no_token, 0.0) - pairs)
                if self.metrics:
                    self.metrics.record_merge(cid, pairs)

    def accrue_rewards(self, usd: float) -> None:
        if self.metrics:
            self.metrics.record_est_reward(usd)

    def check_crossed_books(self) -> None:
        """Detect resting orders that look crossed (should have filled) and
        force an order reconcile on the next refresh_state. Runs on the event
        loop, so it must not block — it only flags, never calls the network."""
        for orders in self._open_orders.values():
            for ro in orders:
                book = self.tracker.books.get(ro.quote.token_id)
                ask = book.best_ask if book else None
                if ask is not None and ask <= ro.quote.price:
                    log.warning("live bid appears crossed; forcing order reconcile")
                    self._last_order_reconcile = 0.0
                    return
        for ro in self._exit_orders.values():
            book = self.tracker.books.get(ro.quote.token_id)
            bid = book.best_bid if book else None
            if bid is not None and bid >= ro.quote.price:
                log.warning("live exit ask appears crossed; forcing order reconcile")
                self._last_order_reconcile = 0.0
                return
