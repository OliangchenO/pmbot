"""Tests for broker fill models and live order diff."""

import json
import time
import threading
from collections import deque
from unittest.mock import MagicMock

import pytest

from pmbot.books import Book, BookTracker
from pmbot.brokers import (
    LiveBroker,
    PaperBroker,
    Position,
    _parse_erc20_balance,
    _parse_fill_amount,
)
from pmbot.gamma import Market
from pmbot.strategy import Quote


def _market() -> Market:
    return Market(
        question="Test?", condition_id="cid1",
        yes_token="yes1", no_token="no1", min_size=10,
        max_spread_cents=3, daily_pool=50, liquidity=1000,
        volume_24h=500, tick=0.01, end_date=None, neg_risk=False,
    )


def test_is_transient_matches_real_live_errors():
    """Exact strings seen in the live terminal during the slowdown."""
    from pmbot.brokers import _is_transient
    assert _is_transient(RuntimeError("_ssl.c:983: The handshake operation timed out"))
    assert _is_transient(RuntimeError("The read operation timed out"))
    assert _is_transient(RuntimeError("[Errno 65] No route to host"))
    assert _is_transient(RuntimeError(
        "PolyApiException[status_code=None, error_message=Request exception!]"))
    assert _is_transient(RuntimeError("connection reset by peer"))
    # A genuine API rejection must NOT be retried.
    assert not _is_transient(RuntimeError("not enough balance / allowance"))
    assert not _is_transient(RuntimeError("order rejected: invalid price"))


def test_refresh_overlap_keeps_old_order_when_repost_fails():
    """If the replacement post fails, the expiring order must NOT be cancelled
    — the side stays on the book (no gap) and we reconcile from truth."""
    from pmbot.brokers import GTD_REFRESH_MARGIN_SECS, RestingOrder

    stub = _order_book_stub()
    stub._client_lock = threading.RLock()
    stub.client = MagicMock()
    stub.refresh_overlap = True
    stub.metrics = None
    stub._markets = {}
    stub._logged_post_shape = False
    stub._gtd_expiration = lambda: int(time.time()) + 240
    stub.reconcile_orders = MagicMock()

    cancels = []
    stub._batch_cancel = lambda ids: (cancels.append(tuple(ids)) or True) if ids else True
    stub.client.create_order.return_value = object()
    stub.client.post_orders.return_value = [{}]  # rejection: empty orderID

    q = Quote("yes1", 0.47, 10)
    near = RestingOrder("oldid", q, time.time(),
                        int(time.time()) + GTD_REFRESH_MARGIN_SECS - 5)
    stub._open_orders = {"cid1": [near]}

    LiveBroker.set_quotes(stub, _market(), [q])

    stub.reconcile_orders.assert_called_once()
    assert cancels == []  # the still-resting old order was never cancelled
    assert [ro.order_id for ro in stub._open_orders["cid1"]] == ["oldid"]


def test_with_retry_retries_transient_then_succeeds(monkeypatch):
    from pmbot import brokers
    monkeypatch.setattr(brokers.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("read operation timed out")
        return "ok"

    assert brokers._with_retry("x", fn) == "ok"
    assert calls["n"] == 3


def test_with_retry_reraises_non_transient_without_retrying(monkeypatch):
    import pytest
    from pmbot import brokers
    monkeypatch.setattr(brokers.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise RuntimeError("order rejected: invalid price")

    with pytest.raises(RuntimeError):
        brokers._with_retry("x", fn)
    assert calls["n"] == 1  # fail fast, no retry


def test_refresh_overlap_posts_replacement_before_cancelling_old():
    """A pure GTD refresh must post the new order BEFORE cancelling the old
    one, so the side is never off the book when rewards are sampled."""
    from pmbot.brokers import GTD_REFRESH_MARGIN_SECS, RestingOrder

    stub = _order_book_stub()
    stub._client_lock = threading.RLock()
    stub.client = MagicMock()
    stub.refresh_overlap = True
    stub.metrics = None
    stub._markets = {}
    stub._logged_post_shape = False
    stub._gtd_expiration = lambda: int(time.time()) + 240
    stub.reconcile_orders = MagicMock()

    calls = []

    def fake_cancel(ids, **_kwargs):
        if ids:
            calls.append(("cancel", tuple(ids)))
        return True

    stub._batch_cancel = fake_cancel
    stub.client.create_order.return_value = object()
    stub.client.post_orders.side_effect = lambda args: (
        calls.append(("post", len(args))) or [{"orderID": "newid"}])

    q = Quote("yes1", 0.47, 10)
    near = RestingOrder("oldid", q, time.time(),
                        int(time.time()) + GTD_REFRESH_MARGIN_SECS - 5)
    stub._open_orders = {"cid1": [near]}

    LiveBroker.set_quotes(stub, _market(), [q])

    kinds = [c[0] for c in calls]
    assert "post" in kinds and "cancel" in kinds
    assert kinds.index("post") < kinds.index("cancel")  # overlap: no gap
    assert calls[-1] == ("cancel", ("oldid",))
    assert [ro.order_id for ro in stub._open_orders["cid1"]] == ["newid"]


def test_live_quote_post_logs_order_lifecycle_event(caplog):
    """A confirmed CLOB quote must be recorded in the runtime log."""
    stub = _order_book_stub()
    stub._client_lock = threading.RLock()
    stub.client = MagicMock()
    stub.refresh_overlap = True
    stub.metrics = None
    stub._markets = {}
    stub._logged_post_shape = False
    stub._gtd_expiration = lambda: 1_700_000_240
    stub.reconcile_orders = MagicMock()
    stub._batch_cancel = lambda ids: True
    stub.client.create_order.return_value = object()
    stub.client.post_orders.return_value = [{"orderID": "newid"}]

    caplog.set_level("INFO", logger="pmbot.broker")
    LiveBroker.set_quotes(stub, _market(), [Quote("yes1", 0.47, 10)])

    assert "ORDER event=ORDER_PLACED" in caplog.text
    assert "order_id=newid" in caplog.text
    assert "cid=" not in caplog.text
    assert "token=" not in caplog.text
    assert "side=BUY" in caplog.text


def test_live_quote_cancel_logs_order_lifecycle_event(caplog):
    """A successful cancellation must retain its original context in the log."""
    from pmbot.brokers import RestingOrder

    stub = _order_book_stub()
    stub._client_lock = threading.RLock()
    stub.client = MagicMock()
    stub.metrics = None
    market = _market()
    stub._markets = {market.condition_id: market}
    stub._open_orders = {
        market.condition_id: [RestingOrder(
            "oldid", Quote("yes1", 0.47, 10), time.time(), 0)]}

    caplog.set_level("INFO", logger="pmbot.broker")
    assert LiveBroker._batch_cancel(stub, ["oldid"]) is True

    assert "ORDER event=ORDER_CANCELLED" in caplog.text
    assert "order_id=oldid" in caplog.text
    assert "cid=" not in caplog.text
    assert "token=" not in caplog.text
    assert "side=BUY" in caplog.text


def test_order_cancellation_audit_keeps_order_id_and_pair_economics(tmp_path):
    """Cancellation evidence must retain the economics captured at submission."""
    from pmbot.audit import AuditLogger
    from pmbot.brokers import RestingOrder

    stub = _order_book_stub()
    stub._client_lock = threading.RLock()
    stub.client = MagicMock()
    market = _market()
    stub._markets = {market.condition_id: market}
    stub.audit = AuditLogger(str(tmp_path / "audit.jsonl"))
    stub._open_orders = {market.condition_id: [RestingOrder(
        "order-9", Quote(market.yes_token, 0.329, 50), time.time(), 0,
        {"path": "inventory_recovery", "unpaired_cost": 0.669,
         "pair_cap": 0.329, "expected_pair_pnl": 0.002})]}

    assert LiveBroker._batch_cancel(stub, ["order-9"])

    row = json.loads((tmp_path / "audit.jsonl").read_text(encoding="utf-8"))
    assert row["event"] == "order_cancelled"
    assert row["order_id"] == "order-9"
    assert row["path"] == "inventory_recovery"
    assert row["unpaired_cost"] == 0.669
    assert row["pair_cap"] == 0.329


def test_refresh_cancellation_inherits_recovery_audit_context(tmp_path):
    """A rehydrated expiring quote must not be recorded as a normal cancel."""
    from pmbot.audit import AuditLogger
    from pmbot.brokers import GTD_REFRESH_MARGIN_SECS, RestingOrder

    stub = _order_book_stub()
    stub._client_lock = threading.RLock()
    stub.client = MagicMock()
    stub.refresh_overlap = True
    stub.metrics = None
    stub._logged_post_shape = False
    stub.reconcile_orders = MagicMock()
    stub._gtd_expiration = lambda: 1_700_000_240
    stub._batch_cancel = LiveBroker._batch_cancel.__get__(stub, LiveBroker)
    stub.audit = AuditLogger(str(tmp_path / "audit.jsonl"))
    market = _market()
    stub._markets = {market.condition_id: market}
    old = Quote(market.no_token, 0.34, 15)
    stub._open_orders = {market.condition_id: [RestingOrder(
        "old-recovery", old, time.time(), time.time() + GTD_REFRESH_MARGIN_SECS - 1,
    )]}
    stub.client.create_order.return_value = object()
    stub.client.post_orders.return_value = [{"orderID": "new-recovery"}]
    recovery_audit = {
        "path": "inventory_recovery", "unpaired_cost": 0.65,
        "pair_cap": 0.34, "expected_pair_pnl": 0.001,
    }

    LiveBroker.set_quotes(stub, market, [old], {market.no_token: recovery_audit})

    rows = [json.loads(line) for line in
            (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    cancelled = next(row for row in rows if row["event"] == "order_cancelled")
    assert cancelled["path"] == "inventory_recovery"
    assert cancelled["reason"] == "expiry_refresh"
    assert cancelled["pair_cap"] == 0.34


def test_parse_fill_amount_from_taking_amount():
    assert _parse_fill_amount({"takingAmount": "5.0"}, 10.0) == 5.0


def test_parse_fill_amount_zero_on_error():
    assert _parse_fill_amount({"success": False, "error": "rejected"}, 10.0) == 0.0


def test_parse_erc20_balance_uses_six_decimals():
    assert _parse_erc20_balance(hex(100_512_253)) == 100.512253


def test_paper_queue_consumes_ahead_before_fill():
    tracker = BookTracker(["yes1", "no1"])
    tracker.books["yes1"].bids[0.47] = 100.0
    broker = PaperBroker(500.0, tracker)
    m = _market()
    broker.set_quotes(m, [Quote("yes1", 0.47, 10)])

    import asyncio
    asyncio.run(broker._on_trade("yes1", 0.47, "SELL", 50))
    assert broker.open_quotes(m)
    assert not broker.fills_log


def test_paper_fill_on_strictly_below_price():
    tracker = BookTracker(["yes1", "no1"])
    broker = PaperBroker(500.0, tracker)
    m = _market()
    broker.set_quotes(m, [Quote("yes1", 0.47, 10)])

    import asyncio
    asyncio.run(broker._on_trade("yes1", 0.46, "SELL", 10))
    assert not broker.open_quotes(m)
    assert len(broker.fills_log) == 1


def test_paper_latency_blocks_fill_before_placement_lands():
    tracker = BookTracker(["yes1", "no1"])
    broker = PaperBroker(500.0, tracker, latency_secs=60.0)
    m = _market()
    broker.set_quotes(m, [Quote("yes1", 0.47, 10)])

    import asyncio
    asyncio.run(broker._on_trade("yes1", 0.45, "SELL", 10))
    assert not broker.fills_log  # order still in flight, not on the book yet


def test_paper_replaced_quote_picked_off_during_cancel_latency():
    tracker = BookTracker(["yes1", "no1"])
    broker = PaperBroker(500.0, tracker, latency_secs=0.0)
    m = _market()
    broker.set_quotes(m, [Quote("yes1", 0.47, 10)])  # lands instantly

    broker.latency = 30.0  # subsequent ops now take 30s to land
    broker.set_quotes(m, [Quote("yes1", 0.45, 10)])  # requote down

    import asyncio
    asyncio.run(broker._on_trade("yes1", 0.46, "SELL", 10))
    # The stale 0.47 bid (cancel in flight) gets picked off; the new 0.45
    # bid hasn't landed yet.
    assert len(broker.fills_log) == 1
    assert broker.fills_log[0]["price"] == 0.47


def test_paper_fill_capped_by_trade_size():
    tracker = BookTracker(["yes1", "no1"])
    broker = PaperBroker(500.0, tracker)
    m = _market()
    broker.set_quotes(m, [Quote("yes1", 0.47, 50)])

    import asyncio
    asyncio.run(broker._on_trade("yes1", 0.46, "SELL", 5))
    assert len(broker.fills_log) == 1
    assert broker.fills_log[0]["size"] == 5
    # Remainder still resting at the front of its level.
    assert broker.open_quotes(m)[0].size == 45


def test_paper_taker_buy_respects_displayed_depth():
    tracker = BookTracker(["yes1", "no1"])
    tracker.books["no1"].asks = {0.50: 5.0, 0.52: 20.0}
    broker = PaperBroker(500.0, tracker)
    m = _market()
    filled = broker.taker_buy(m, "no1", 10.0, max_price=0.51)
    assert filled == 5.0  # only the 0.50 level is inside the price cap


def test_paper_unpaired_cost_basis_survives_pair_merge_and_exit():
    """Paper recovery must use the average cost of the still-unpaired leg."""
    tracker = BookTracker(["yes1", "no1"])
    broker = PaperBroker(500.0, tracker)
    m = _market()

    broker._fill(m, Quote("no1", 0.669, 50), 50)
    broker._fill(m, Quote("yes1", 0.300, 10), 10)
    broker._fill_exit(m, Quote("no1", 0.700, 10), 10)

    assert broker.unpaired_shares(m) == -30.0
    assert broker.unpaired_cost_basis(m) == pytest.approx(0.669)


def test_paper_maker_fill_charges_no_fee():
    """Makers are never charged fees on Polymarket, even in fee-enabled
    markets — a resting quote fill costs only price × size."""
    tracker = BookTracker(["yes1", "no1"])
    broker = PaperBroker(500.0, tracker)
    m = _market()
    m.fee_bps = 200
    broker._fill(m, Quote("yes1", 0.40, 10), 10)
    assert abs(broker.state.cash - (500.0 - 4.0)) < 1e-9
    assert "fee" not in broker.fills_log[0]


def test_paper_maker_exit_charges_no_fee():
    tracker = BookTracker(["yes1", "no1"])
    broker = PaperBroker(500.0, tracker)
    m = _market()
    m.fee_bps = 200
    broker.state.positions["cid1"] = Position(yes_shares=10)
    broker._fill_exit(m, Quote("yes1", 0.60, 10), 10)
    assert abs(broker.state.cash - (500.0 + 6.0)) < 1e-9
    assert "fee" not in broker.fills_log[0]


def test_paper_taker_buy_charges_fee():
    """Only the taker path crosses the spread, so it is the only place a
    Polymarket fee applies."""
    tracker = BookTracker(["yes1", "no1"])
    tracker.books["no1"].asks = {0.40: 10.0}
    broker = PaperBroker(500.0, tracker)
    m = _market()
    m.fee_bps = 200
    filled = broker.taker_buy(m, "no1", 10.0, max_price=0.41)
    assert filled == 10.0
    # cost 4.0 + fee 200/10000 * (0.40 * 0.60) * 10 = 0.048
    assert abs(broker.state.cash - (500.0 - 4.0 - 0.048)) < 1e-9
    assert abs(broker.fills_log[0]["fee"] - 0.048) < 1e-9


def test_paper_exit_requires_queue_or_through_print():
    tracker = BookTracker(["yes1", "no1"])
    tracker.books["yes1"].asks = {0.53: 40.0}
    broker = PaperBroker(500.0, tracker)
    m = _market()
    broker.state.positions["cid1"] = Position(yes_shares=10)
    broker.set_exit(m, Quote("yes1", 0.53, 10))

    import asyncio
    # Trade at our price only consumes the 40 shares queued ahead.
    asyncio.run(broker._on_trade("yes1", 0.53, "BUY", 30))
    assert not broker.fills_log
    # A print above our ask guarantees the fill.
    asyncio.run(broker._on_trade("yes1", 0.54, "BUY", 30))
    assert broker.fills_log and broker.fills_log[0]["exit"] is True


def test_paper_cancel_quotes_keeps_exits():
    tracker = BookTracker(["yes1", "no1"])
    broker = PaperBroker(500.0, tracker)
    m = _market()
    broker.set_quotes(m, [Quote("yes1", 0.47, 10)])
    broker.set_exit(m, Quote("yes1", 0.53, 5))
    broker.cancel_quotes()
    assert not broker.open_quotes(m)
    assert broker.exit_quote(m) is not None


def test_live_order_diff_keeps_unchanged():
    """Unit test the set_quotes keep/cancel decision without CLOB imports."""
    from pmbot.brokers import GTD_REFRESH_MARGIN_SECS, RestingOrder

    q = Quote("yes1", 0.47, 10)
    ro = RestingOrder("oid1", q, time.time(),
                      int(time.time()) + GTD_REFRESH_MARGIN_SECS + 60)
    desired = {q.token_id: q}
    near_expiry = ro.expiration - time.time() < GTD_REFRESH_MARGIN_SECS
    should_keep = (
        desired.get(ro.quote.token_id) is not None
        and desired[ro.quote.token_id].key() == ro.quote.key()
        and not near_expiry
    )
    assert should_keep
    desired2 = {q.token_id: Quote("yes1", 0.45, 10)}
    should_keep2 = (
        desired2.get(ro.quote.token_id) is not None
        and desired2[ro.quote.token_id].key() == ro.quote.key()
    )
    assert not should_keep2


def test_gtd_refresh_margin_covers_security_threshold():
    """An order must be refreshed before its effective (expiration - 60s)
    expiry, not its nominal expiration."""
    from pmbot.brokers import GTD_REFRESH_MARGIN_SECS, GTD_SECURITY_THRESHOLD_SECS

    assert GTD_REFRESH_MARGIN_SECS > GTD_SECURITY_THRESHOLD_SECS


def _order_book_stub():
    """LiveBroker order-tracking methods exercised without a CLOB client."""
    class Stub:
        pass

    stub = Stub()
    stub._open_orders = {}
    stub._exit_orders = {}
    stub._record_order_event = lambda *args, **kwargs: LiveBroker._record_order_event(
        stub, *args, **kwargs)
    stub._resting_order_context = lambda order_id: LiveBroker._resting_order_context(
        stub, order_id)
    return stub


def _live_fill_stub():
    """Minimal live broker state for the real fill-receipt path."""
    stub = _order_book_stub()
    market = _market()
    stub._state_lock = threading.RLock()
    stub._pending_hedges = {}
    stub._positions = {market.condition_id: {"yes": 0.0, "no": 0.0, "value": 0.0}}
    stub._token_shares = {}
    stub._markets = {market.condition_id: market}
    stub._ws_deltas = deque()
    stub._ws_deltas_lock = threading.Lock()
    stub.metrics = None
    stub.fills_log = []
    stub._apply_fill_to_orders = lambda token_id, size, side: LiveBroker._apply_fill_to_orders(
        stub, token_id, size, side)
    return stub


def test_ws_fill_audit_retains_exchange_identifiers_and_hedge_path(tmp_path):
    """A user-feed receipt must preserve its external IDs and audit path."""
    from pmbot.audit import AuditLogger

    stub = _live_fill_stub()
    stub.audit = AuditLogger(str(tmp_path / "audit.jsonl"))
    market = stub._markets["cid1"]

    LiveBroker.record_user_fill(
        stub, market.yes_token, "BUY", 0.372, 50.0, taker=True,
        order_id="order-7", fill_id="fill-8", trade_hash="0xabc")

    row = json.loads((tmp_path / "audit.jsonl").read_text(encoding="utf-8"))
    assert row["event"] == "ws_fill"
    assert row["order_id"] == "order-7"
    assert row["fill_id"] == "fill-8"
    assert row["trade_hash"] == "0xabc"
    assert row["path"] == "forced_hedge"


def test_due_for_refresh_flags_near_expiry_orders():
    """A stable quote (unchanged key-set) must still be reposted before its GTD
    order expires, or it silently disappears from the book."""
    from types import SimpleNamespace
    from pmbot.brokers import GTD_REFRESH_MARGIN_SECS, RestingOrder

    stub = _order_book_stub()
    mkt = SimpleNamespace(condition_id="cid1")
    now = time.time()

    fresh = RestingOrder("o1", Quote("yes1", 0.47, 10), now,
                         int(now) + GTD_REFRESH_MARGIN_SECS + 60)
    stub._open_orders = {"cid1": [fresh]}
    assert LiveBroker.due_for_refresh(stub, mkt) is False

    expiring = RestingOrder("o1", Quote("yes1", 0.47, 10), now,
                            int(now) + GTD_REFRESH_MARGIN_SECS - 5)
    stub._open_orders = {"cid1": [expiring]}
    assert LiveBroker.due_for_refresh(stub, mkt) is True

    stub._open_orders = {}
    assert LiveBroker.due_for_refresh(stub, mkt) is False


def test_apply_fill_partial_decrements_resting_order():
    from pmbot.brokers import RestingOrder

    stub = _order_book_stub()
    ro = RestingOrder("o1", Quote("yes1", 0.47, 10), time.time(), 0)
    stub._open_orders = {"cid1": [ro]}
    LiveBroker._apply_fill_to_orders(stub, "yes1", 4.0, "BUY")
    assert stub._open_orders["cid1"][0].quote.size == 6.0


def test_apply_fill_full_removes_resting_order():
    from pmbot.brokers import RestingOrder

    stub = _order_book_stub()
    ro = RestingOrder("o1", Quote("yes1", 0.47, 10), time.time(), 0)
    stub._open_orders = {"cid1": [ro]}
    LiveBroker._apply_fill_to_orders(stub, "yes1", 10.0, "BUY")
    assert stub._open_orders["cid1"] == []


def test_apply_fill_does_not_leak_to_other_orders():
    """A partial fill must consume the whole fill against its own order,
    never carrying phantom leftover size into other resting orders."""
    from pmbot.brokers import RestingOrder

    stub = _order_book_stub()
    ro1 = RestingOrder("o1", Quote("yes1", 0.47, 10), time.time(), 0)
    ro2 = RestingOrder("o2", Quote("yes1", 0.46, 10), time.time(), 0)
    stub._open_orders = {"cid1": [ro1], "cid2": [ro2]}
    LiveBroker._apply_fill_to_orders(stub, "yes1", 6.0, "BUY")
    assert stub._open_orders["cid1"][0].quote.size == 4.0
    assert stub._open_orders["cid2"][0].quote.size == 10.0


def test_apply_fill_sell_decrements_exit_order():
    from pmbot.brokers import RestingOrder

    stub = _order_book_stub()
    stub._exit_orders = {
        "cid1": RestingOrder("o1", Quote("yes1", 0.53, 8), time.time(), 0)}
    LiveBroker._apply_fill_to_orders(stub, "yes1", 3.0, "SELL")
    assert stub._exit_orders["cid1"].quote.size == 5.0
    LiveBroker._apply_fill_to_orders(stub, "yes1", 5.0, "SELL")
    assert "cid1" not in stub._exit_orders


def test_cancel_quotes_keeps_local_state_when_cancel_fails():
    from pmbot.brokers import RestingOrder

    stub = _order_book_stub()
    stub._client_lock = threading.RLock()
    stub.client = MagicMock()
    stub.client.cancel_orders.side_effect = RuntimeError("down")
    stub.client.cancel_order.side_effect = RuntimeError("down")
    stub.reconcile_orders = MagicMock()
    stub._batch_cancel = lambda ids: LiveBroker._batch_cancel(stub, ids)
    ro = RestingOrder("o1", Quote("yes1", 0.47, 10), time.time(), 0)
    stub._open_orders = {"cid1": [ro]}

    LiveBroker.cancel_quotes(stub)

    assert stub._open_orders == {"cid1": [ro]}
    stub.reconcile_orders.assert_called_once()


def test_live_crossed_book_forces_reconcile_without_blocking():
    """A crossed resting bid must flag a reconcile for the next off-thread
    refresh, not call the network on the event loop."""
    from pmbot.brokers import RestingOrder

    tracker = BookTracker(["yes1"])
    tracker.books["yes1"].asks = {0.46: 10.0}
    stub = _order_book_stub()
    stub.tracker = tracker
    stub._last_order_reconcile = time.time()
    stub.reconcile_orders = MagicMock()
    stub._open_orders = {
        "cid1": [RestingOrder("o1", Quote("yes1", 0.47, 10), time.time(), 0)]}

    LiveBroker.check_crossed_books(stub)

    stub.reconcile_orders.assert_not_called()
    assert stub._last_order_reconcile == 0.0


# --- deposit-wallet (signature_type 3) balance-cache sync regression -------
# Without these, the CLOB's cache reads 0 for the deposit wallet and rejects
# orders with "not enough balance / allowance: ... balance: 0".

def _live_stub(sig_type=3):
    stub = _order_book_stub()
    stub._client_lock = threading.RLock()
    stub._state_lock = threading.RLock()
    stub.client = MagicMock()
    stub.cfg = {"live": {"signature_type": sig_type}}
    stub._gtd_expiration = lambda _ttl=None: 123
    stub.exit_order_ttl = 600
    stub.metrics = None
    stub._markets = {}
    stub._token_shares = {}
    stub._pending_hedges = {}
    stub.sync_calls = []
    stub._sync_clob_balance = lambda at, tid=None: stub.sync_calls.append((at, tid))
    return stub


def test_place_sell_syncs_conditional_balance_first():
    from py_clob_client_v2 import AssetType
    stub = _live_stub()
    stub.client.post_order.return_value = {"orderID": "oidS"}
    ro = LiveBroker._place_sell(stub, Quote("tok9", 0.62, 50))
    assert ro is not None and ro.order_id == "oidS"
    # the conditional token was synced for exactly the token being sold
    assert stub.sync_calls == [(AssetType.CONDITIONAL, "tok9")]


def test_live_place_sell_uses_exit_order_ttl(monkeypatch):
    from pmbot.brokers import GTD_SECURITY_THRESHOLD_SECS

    stub = _live_stub()
    stub.order_ttl = 180
    stub.exit_order_ttl = 600
    now = 1_700_000_000
    monkeypatch.setattr("pmbot.brokers.time.time", lambda: now)
    stub._gtd_expiration = LiveBroker._gtd_expiration.__get__(stub, LiveBroker)
    stub.client.post_order.return_value = {"orderID": "exit-1"}

    LiveBroker._place_sell(stub, Quote("yes1", 0.53, 10))

    args = stub.client.create_order.call_args.args[0]
    assert args.expiration == now + 600 + GTD_SECURITY_THRESHOLD_SECS


def test_live_exit_keeps_unchanged_order_before_refresh(monkeypatch):
    from pmbot.brokers import GTD_REFRESH_MARGIN_SECS, RestingOrder

    stub = _live_stub()
    quote = Quote("yes1", 0.53, 10)
    now = 1_700_000_000
    monkeypatch.setattr("pmbot.brokers.time.time", lambda: now)
    stub._exit_orders["cid1"] = RestingOrder(
        "exit-old", quote, now - 60, now + GTD_REFRESH_MARGIN_SECS + 1,
    )
    stub._batch_cancel = MagicMock(return_value=True)
    stub._place_sell = MagicMock()

    LiveBroker.set_exit(stub, _market(), quote)

    stub._batch_cancel.assert_not_called()
    stub._place_sell.assert_not_called()


def test_live_exit_replaces_at_refresh_after_cancel(monkeypatch):
    from pmbot.brokers import GTD_REFRESH_MARGIN_SECS, RestingOrder

    stub = _live_stub()
    quote = Quote("yes1", 0.53, 10)
    now = 1_700_000_000
    monkeypatch.setattr("pmbot.brokers.time.time", lambda: now)
    stub._exit_orders["cid1"] = RestingOrder(
        "exit-old", quote, now - 590, now + GTD_REFRESH_MARGIN_SECS - 1,
    )
    calls = []
    stub._batch_cancel = lambda ids: calls.append(("cancel", ids)) or True
    stub._place_sell = lambda q: calls.append(("post", q)) or RestingOrder(
        "exit-new", q, now, now + 600,
    )

    LiveBroker.set_exit(stub, _market(), quote)

    assert calls == [("cancel", ["exit-old"]), ("post", quote)]


def test_live_exit_quantity_change_cancels_before_replacement(monkeypatch):
    from pmbot.brokers import RestingOrder

    stub = _live_stub()
    now = 1_700_000_000
    monkeypatch.setattr("pmbot.brokers.time.time", lambda: now)
    old = Quote("yes1", 0.53, 10)
    new = Quote("yes1", 0.53, 5)
    stub._exit_orders["cid1"] = RestingOrder("exit-old", old, now - 1, now + 600)
    calls = []
    stub._batch_cancel = lambda ids: calls.append(("cancel", ids)) or True
    stub._place_sell = lambda q: calls.append(("post", q)) or RestingOrder(
        "exit-new", q, now, now + 600,
    )

    LiveBroker.set_exit(stub, _market(), new)

    assert calls == [("cancel", ["exit-old"]), ("post", new)]


def test_taker_buy_syncs_collateral_first():
    from py_clob_client_v2 import AssetType
    stub = _live_stub()
    stub.client.post_order.return_value = {"takingAmount": "10.0"}
    market = _market()
    filled = LiveBroker.taker_buy(stub, market, "tok9", 10.0, 0.6)
    assert filled == 10.0
    assert (AssetType.COLLATERAL, None) in stub.sync_calls
    assert LiveBroker.has_pending_hedge(stub, market.condition_id)
    assert stub._pending_hedges[market.condition_id].token_id == "tok9"


def test_pending_hedge_survives_stale_snapshot_without_double_counting():
    """A confirmed FAK hedge must freeze a market until REST observes it.

    The Data API can return the pre-fill position after the user WebSocket has
    delivered the fill. That stale snapshot must neither re-open exposure nor
    make the eventual snapshot count the hedge twice.
    """
    stub = _order_book_stub()
    market = _market()
    stub._state_lock = threading.RLock()
    stub._pending_hedges = {}
    stub._positions = {market.condition_id: {"yes": 0.0, "no": 31.0, "value": 0.0}}
    stub._token_shares = {market.no_token: 31.0}
    stub._markets = {market.condition_id: market}
    stub._ws_deltas = deque()
    stub._ws_deltas_lock = threading.Lock()
    stub._open_orders = {}
    stub._exit_orders = {}
    stub.metrics = None
    stub.fills_log = []

    LiveBroker._register_pending_hedge(stub, market, market.yes_token, 31.0)
    assert LiveBroker.has_pending_hedge(stub, market.condition_id)
    assert LiveBroker.unpaired_shares(stub, market) == 0.0

    LiveBroker.record_user_fill(stub, market.yes_token, "BUY", 0.61, 31.0, taker=True)
    assert stub._positions[market.condition_id]["yes"] == 0.0
    assert LiveBroker.unpaired_shares(stub, market) == 0.0

    LiveBroker._apply_position_snapshot(
        stub,
        {market.condition_id: {"yes": 0.0, "no": 31.0, "value": 0.0}},
        {market.no_token: 31.0},
    )
    assert LiveBroker.has_pending_hedge(stub, market.condition_id)
    assert LiveBroker.unpaired_shares(stub, market) == 0.0

    LiveBroker._apply_position_snapshot(
        stub,
        {market.condition_id: {"yes": 31.0, "no": 31.0, "value": 0.0}},
        {market.yes_token: 31.0, market.no_token: 31.0},
    )
    assert not LiveBroker.has_pending_hedge(stub, market.condition_id)
    assert LiveBroker.unpaired_shares(stub, market) == 0.0


def test_live_fill_sends_dingtalk_notification():
    """Removing the notification call after a live fill must fail this test."""
    class RecordingNotifier:
        def __init__(self):
            self.messages = []

        def send_text(self, content):
            self.messages.append(content)

    stub = _live_fill_stub()
    stub.notifier = RecordingNotifier()
    market = stub._markets["cid1"]

    LiveBroker.record_user_fill(stub, market.yes_token, "BUY", 0.47, 3.0)

    assert stub.notifier.messages == [
        "[PMBot LIVE FILL] Test? | BUY YES 3.00 @ 0.470"
    ]


def test_live_fill_keeps_state_when_dingtalk_notification_fails(caplog):
    """A notification outage must not unwind an already received live fill."""
    class FailingNotifier:
        def send_text(self, content):
            raise RuntimeError("network unavailable")

    stub = _live_fill_stub()
    stub.notifier = FailingNotifier()
    market = stub._markets["cid1"]

    LiveBroker.record_user_fill(stub, market.yes_token, "BUY", 0.47, 3.0)

    assert stub.fills_log[-1]["size"] == 3.0
    assert stub._positions[market.condition_id]["yes"] == 3.0
    assert "钉钉成交通知发送失败" in caplog.text


def test_sync_clob_balance_builds_params_and_swallows_errors():
    from py_clob_client_v2 import AssetType
    stub = _order_book_stub()
    stub._client_lock = threading.RLock()
    stub.client = MagicMock()
    stub.cfg = {"live": {"signature_type": 3}}
    stub.client.update_balance_allowance.side_effect = RuntimeError("relayer down")
    # a sync hiccup must never bubble up into the quoting loop
    LiveBroker._sync_clob_balance(stub, AssetType.CONDITIONAL, "tok9")
    params = stub.client.update_balance_allowance.call_args.args[0]
    assert params.token_id == "tok9"
    assert params.signature_type == 3


def test_select_collateral_prefers_onchain_over_stale_cache():
    # on-chain pUSD wins even when the (stale) CLOB cache reads higher
    assert LiveBroker._select_collateral(70.5, 100.0) == 70.5
    # falls back to the cache only when the on-chain read is unavailable
    assert LiveBroker._select_collateral(None, 100.0) == 100.0
    assert LiveBroker._select_collateral(None, None) is None


def test_held_position_is_hydrated_and_included_in_inventory(monkeypatch):
    """A position outside the active quote set must enter normal management."""
    from pmbot import brokers

    market = _market()
    stub = _live_stub()
    stub._markets = {}
    stub._positions = {market.condition_id: {"yes": 20.0, "no": 9.0, "value": 13.048}}
    stub.tracker = BookTracker([market.yes_token, market.no_token])
    stub.tracker.books[market.yes_token].last_trade_price = 0.4
    monkeypatch.setattr(brokers.gamma, "fetch_market", lambda cid: market)

    # Until Gamma responds, the position is conservatively visible rather than
    # disappearing from the inventory total.
    assert LiveBroker.total_inventory_usd(stub) == pytest.approx(13.048)
    added = LiveBroker._hydrate_held_markets(stub, {market.condition_id})

    assert added == [market]
    assert LiveBroker.held_markets(stub) == [market]
    assert LiveBroker.unpaired_shares(stub, market) == 11.0
    assert LiveBroker.total_inventory_usd(stub) == pytest.approx(2.6)


def test_live_unpaired_cost_basis_uses_exchange_average_price():
    market = _market()
    stub = _live_stub()
    stub._positions = {
        market.condition_id: {"yes": 20.0, "no": 9.0, "value": 0.0,
                              "yes_cost": 7.96, "yes_cost_shares": 20.0,
                              "no_cost": 5.74, "no_cost_shares": 9.0},
    }
    stub._pending_hedges = {}
    stub._state_lock = threading.RLock()

    assert LiveBroker.unpaired_cost_basis(stub, market) == pytest.approx(0.398)


def test_live_unpaired_cost_basis_falls_back_to_matching_audit_when_average_missing():
    """A restart without Data API avgPrice retains a verified matching basis."""
    market = _market()
    stub = _live_stub()
    stub._positions = {market.condition_id: {"yes": 15.0, "no": 0.0, "value": 0.0,
                                              "yes_cost": 0.0, "no_cost": 0.0,
                                              "yes_cost_shares": 0.0, "no_cost_shares": 0.0}}
    stub._pending_hedges = {}
    stub._state_lock = threading.RLock()
    stub._recovery_basis_cache = {market.condition_id: (0.65, 15.0)}

    assert LiveBroker.unpaired_cost_basis(stub, market) == pytest.approx(0.65)
