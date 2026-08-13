"""Hand selected completed reward-exit batches to manual management safely."""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

from pmbot.books import BookTracker
from pmbot.brokers import LiveBroker
from pmbot import gamma, reward_exit
from pmbot.gamma import Market
from pmbot.main import _build_live_notifier, load_config
from pmbot.metrics import MetricsStore
from pmbot.reward_exit import TakeFill


BATCH_IDS = (
    "reward-exit-c977ac66-fbdc-43cb-ae4b-e91126e3f6c7",
    "reward-exit-8ae7c703-6952-4190-b97e-619fb1d8bf40",
    "reward-exit-b120e3fc-f296-4182-b5b7-74505c72bdc7",
)
SUBMINIMUM_BATCH_IDS = (
    "reward-exit-0d3ea7bb-0742-404d-8be8-d953165bc578",
)


def _market(batch: dict) -> Market:
    return Market(
        question=str(batch["market_name"]), condition_id=str(batch["cid"]),
        yes_token=str(batch["origin_token_id"]),
        no_token=str(batch["complement_token_id"]), min_size=5.0,
        max_spread_cents=0.0, daily_pool=0.0, liquidity=0.0, volume_24h=0.0,
        tick=0.01, end_date=None, neg_risk=False,
    )


def _has_open_orders(broker: LiveBroker, cid: str) -> bool:
    return bool(broker._open_orders.get(cid) or broker._exit_orders.get(cid))


def _notify(notifier, batch: dict) -> None:
    if notifier is None:
        print(f"NOTIFY_SKIPPED batch_id={batch['batch_id']} reason=no_notifier")
        return
    price = float(batch.get("exit_target_price") or 0.0)
    size = float(batch["exit_initial_size"])
    size_text = f"{size:.0f}" if abs(size - round(size)) < 1e-9 else f"{size:.2f}"
    suggestion = (f"{size_text} 股 @ {price:.4f}"
                  if price > 0 else "无法计算有效建议价")
    notifier.send_markdown(
        "[PMBot] 市场转人工管理",
        "## 市场转人工管理\n"
        f"- 市场：{batch['market_name']}\n"
        f"- CID：{batch['cid']}\n"
        f"- 建议卖出：{suggestion}\n"
        "- 机器人已撤销该市场订单，后续不再操作。",
    )
    time.sleep(1.0)  # allow the existing asynchronous notifier to drain once


def _prepare_subminimum_handoff(store: MetricsStore, batch: dict) -> tuple[Market, dict] | None:
    """Recompute a manual-hold target from confirmed incomplete take fills."""
    take_fills = [
        TakeFill(
            fill_id=str(fill["fill_id"]), ts=float(fill["ts"]),
            price=float(fill["price"]), size=float(fill["size"]),
            notional=float(fill["price"]) * float(fill["size"]),
            fee_usd=float(fill.get("fee_usd") or 0.0),
        )
        for fill in store.list_reward_exit_fills(batch["batch_id"], "batch_take")
    ]
    total_take = sum(fill.size for fill in take_fills)
    origin_size = float(batch["origin_size"])
    residual = float(batch["take_target_size"]) - total_take
    if total_take <= origin_size or residual <= 0:
        print(f"SKIP batch_id={batch['batch_id']} reason=not_subminimum_residual")
        return None
    market = gamma.fetch_market(str(batch["cid"]))
    if market is None:
        print(f"ABORT batch_id={batch['batch_id']} reason=market_fetch_failed")
        return None
    market.fee_bps, market.fee_exponent = gamma._fetch_market_fees(market.condition_id, {})
    tracker = BookTracker([str(batch["complement_token_id"])])
    asyncio.run(tracker._rest_refresh_all())
    book = tracker.books[str(batch["complement_token_id"])]
    if book.min_order_size is None or residual >= book.min_order_size:
        print(
            f"SKIP batch_id={batch['batch_id']} reason=residual_still_tradable "
            f"residual={residual:.6f} min_order_size={book.min_order_size}"
        )
        return None
    split = reward_exit.split_take_fills_fifo(take_fills, origin_size)
    paired_loss = reward_exit.compute_paired_loss(
        origin_notional_usd=float(batch["origin_notional_usd"]),
        origin_fee_usd=float(batch.get("origin_fee_usd") or 0.0),
        paired_complement_notional=split.paired_notional,
        paired_complement_fee=split.paired_fee,
        origin_size=origin_size,
    )
    exit_size = total_take - origin_size
    target = reward_exit.compute_sell_target(
        market=market, exit_size=exit_size,
        exit_cost_notional=split.exit_notional, exit_cost_fee=split.exit_fee,
        paired_loss_usd=paired_loss, best_bid=book.best_bid,
    )
    updated = {
        **batch, "market_name": market.question, "paired_size": origin_size,
        "paired_loss_usd": paired_loss, "exit_initial_size": exit_size,
        "exit_target_price": float(target.price or 0.0),
    }
    print(
        f"READY_SUBMIN batch_id={batch['batch_id']} market={market.question!r} "
        f"cid={market.condition_id} take={total_take:.6f} residual={residual:.6f} "
        f"min_order_size={book.min_order_size:.6f} exit={exit_size:.6f} "
        f"best_bid={book.best_bid} suggestion={updated['exit_target_price']:.4f}"
    )
    return market, updated


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--include-subminimum", action="store_true")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    cfg = load_config(str(ROOT / "config.yaml"))
    store = MetricsStore(str(ROOT / "data" / "metrics.db"))
    notifier = _build_live_notifier(cfg)
    broker = LiveBroker(cfg, BookTracker([]), notifier)
    try:
        if not broker.reconcile_orders():
            print("ABORT: initial exchange order reconciliation failed")
            return 2
        for batch_id in BATCH_IDS:
            batch = store.get_reward_exit_batch(batch_id)
            if batch is None:
                print(f"SKIP batch_id={batch_id} reason=missing")
                continue
            if batch["status"] == "MANUAL_HOLD":
                print(f"SKIP batch_id={batch_id} reason=already_manual_hold")
                continue
            if (batch["status"] != "SELL_PENDING"
                    or float(batch["take_filled_size"]) + 1e-9 < float(batch["take_target_size"])):
                print(f"SKIP batch_id={batch_id} reason=not_completed status={batch['status']}")
                continue
            market = _market(batch)
            open_before = _has_open_orders(broker, market.condition_id)
            print(
                f"READY batch_id={batch_id} market={batch['market_name']!r} "
                f"cid={market.condition_id} suggestion={float(batch['exit_target_price']):.4f} "
                f"open_before={open_before}"
            )
            if not args.execute:
                continue
            if not broker.cancel_all_for_market(market):
                print(f"ABORT batch_id={batch_id} reason=cancel_failed")
                continue
            if not broker.reconcile_orders() or _has_open_orders(broker, market.condition_id):
                print(f"ABORT batch_id={batch_id} reason=post_cancel_reconcile_not_clear")
                continue
            store.update_reward_exit_batch(
                batch_id=batch_id, status="MANUAL_HOLD",
                manual_reason="take_complete_manual_hold", updated_ts=time.time(),
            )
            _notify(notifier, batch)
            print(f"HANDED_OFF batch_id={batch_id} market={batch['market_name']!r}")
        if args.include_subminimum:
            for batch_id in SUBMINIMUM_BATCH_IDS:
                batch = store.get_reward_exit_batch(batch_id)
                if batch is None:
                    print(f"SKIP batch_id={batch_id} reason=missing")
                    continue
                if batch["status"] == "MANUAL_HOLD":
                    print(f"SKIP batch_id={batch_id} reason=already_manual_hold")
                    continue
                prepared = _prepare_subminimum_handoff(store, batch)
                if prepared is None:
                    continue
                market, updated = prepared
                if not args.execute:
                    continue
                if not broker.cancel_all_for_market(market):
                    print(f"ABORT batch_id={batch_id} reason=cancel_failed")
                    continue
                if not broker.reconcile_orders() or _has_open_orders(broker, market.condition_id):
                    print(f"ABORT batch_id={batch_id} reason=post_cancel_reconcile_not_clear")
                    continue
                store.update_reward_exit_batch(
                    batch_id=batch_id, paired_size=updated["paired_size"],
                    paired_loss_usd=updated["paired_loss_usd"],
                    exit_initial_size=updated["exit_initial_size"],
                    exit_target_price=updated["exit_target_price"],
                    status="MANUAL_HOLD",
                    manual_reason="take_residual_below_min_order_size",
                    closed_ts=None, updated_ts=time.time(),
                )
                _notify(notifier, updated)
                print(f"HANDED_OFF_SUBMIN batch_id={batch_id} market={updated['market_name']!r}")
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
