"""Closed-loop per-market accounting: MarketOutcome and build_market_outcomes().

The module provides a read-only, evidence-based decomposition of one market's
trading cashflows, merge proceeds, fees, unpaired inventory, and explicitly
attributed rewards — all from the SQLite metrics database.  It does not
change any order, quote, or configuration; it only reads.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal


@dataclass(frozen=True)
class MarketOutcome:
    """Auditable per-market economic result over a time window.

    Every dollar is traceable: cashflows are from fills/merges (logged at
    execution), paired value counts unmerged YES+NO pairs at $1/pair, and
    unpaired MTM uses an independently verifiable mid price.  Explicit
    market rewards come from the ``market_rewards`` table (per-condition-id
    attribution), never from an account-level allocation.

    When the window has carry-in (pre-window inventory), carry-in marks the
    position at its last-snapshot cost basis so the window's trading PnL
    isolates only the value change that occurred *during* the window.
    """

    cid: str
    state: Literal[
        "realized",           # all pairs merged/exited; no open inventory
        "paired_unmerged",    # balanced YES/NO but not yet merged
        "unpaired_marked",    # has unpaired shares with verifiable MTM
        "incomplete",         # insufficient data to determine outcome
    ]
    buy_cash_usd: float       # gross cash out for all buys (maker + taker)
    sell_cash_usd: float      # gross cash in for all exits/sells
    merge_cash_usd: float     # $1 per merged pair
    fees_usd: float           # total fees paid
    held_pairs: float         # unmerged YES+NO pairs (shares)
    unpaired_shares: float    # net |YES - NO| (unsigned, shares)
    unpaired_mtm_usd: float | None   # mark-to-market of unpaired shares
    trading_pnl_usd: float | None    # total trading P&L (cash + pairs + MTM)
    market_reward_usd: float | None  # explicitly attributed realized reward
    net_outcome_usd: float | None    # trading_pnl + market_reward
    evidence_flags: tuple[str, ...]  # labels explaining data quality
    carry_in_cost_usd: float  # cost basis of pre-window inventory (0 if none)


def build_market_outcomes(
    conn: sqlite3.Connection,
    start_ts: float,
    end_ts: float,
    *,
    snapshot_max_age_seconds: float | None = None,
) -> list[MarketOutcome]:
    """Create one ``MarketOutcome`` per market active in [start_ts, end_ts).

    The function is read-only: it queries fills, merges, market_rewards,
    and inventory_snapshots, then applies the fixed accounting formulas.

    Accounting (window-only PnL):
      1. Carry-in position is read from the latest pre-window snapshot's
         ``yes_shares`` and ``no_shares`` (total position, including paired
         shares).  Only the *unpaired* portion is marked at its last-snapshot
         cost basis; the paired portion is self-hedging at $1/pair.
      2. Window fills and merges adjust the position normally.
      3. Ending MTM (when a persistent order-book mid exists *and* the
         snapshot is fresh enough) values the remaining unpaired inventory.
      4. PnL = ending_value - carry_in_cost + merges + sells - buys - fees

    ``snapshot_max_age_seconds`` rejects ending MTM snapshots older than this
    relative to ``end_ts``.  When None (default) any snapshot timestamp is
    accepted.  Cycle counting should pass a freshness threshold so
    stall/stale markets don't masquerade as complete.

    Carry-in of +10 YES bought at $0.50 before the window, still open at end
    marked at $0.60: the window contributed +$1.00 (10 x $0.10), not $6.00.

    Important: a single fill price is NOT an order-book mid.  MTM requires
    a persistent mid from inventory_snapshots (recorded at regular intervals
    from the live order book).  When no such mid exists, the market is
    ``incomplete`` regardless of whether a recent fill happened to trade.
    This prevents mark-to-fill bias where the fill that gave us inventory
    also sets the price we mark it at.

    Account-level realized rewards (the ``rewards`` table) are never
    allocated to individual market outcomes.  Only rows from
    ``market_rewards`` (per-condition-id attribution) enter
    ``market_reward_usd``.
    """
    # ---- 1. per-market cashflows (window only) ----
    fill_rows = conn.execute(
        "SELECT cid, side, taker, exit, price, size, merged, fee "
        "FROM fills WHERE ts >= ? AND ts < ?",
        (start_ts, end_ts),
    ).fetchall()

    merge_rows = conn.execute(
        "SELECT cid, COALESCE(SUM(pairs), 0) FROM merges "
        "WHERE ts >= ? AND ts < ? GROUP BY cid",
        (start_ts, end_ts),
    ).fetchall()

    # ---- 2. per-market realized rewards (explicit attribution only) ----
    dates: list[str] = []
    day_start = datetime.fromtimestamp(start_ts, timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0)
    while day_start.timestamp() < end_ts:
        dates.append(day_start.strftime("%Y-%m-%d"))
        day_start += timedelta(days=1)

    reward_rows: list[tuple[str, float]] = []
    if dates:
        placeholders = ",".join("?" for _ in dates)
        reward_rows = conn.execute(
            f"SELECT cid, COALESCE(SUM(realized), 0) FROM market_rewards "
            f"WHERE date IN ({placeholders}) GROUP BY cid",
            dates,
        ).fetchall()

    # ---- 3. carry-in: opening position from pre-window snapshot ----
    # Use the latest inventory snapshot at or before start_ts for the
    # opening position.  ``yes_shares`` and ``no_shares`` are the total
    # position (paired + unpaired); ``unpaired_shares`` is the unsigned
    # net imbalance.  The unpaired portion is cost-marked at its per-share
    # cost basis (multiplied by |unpaired| for total USD).  The paired
    # portion (min(yes, no) × 2) is implicitly self-hedging — when these
    # pairs are merged or resolved during the window they contribute +$1
    # per pair and the merged quantity offsets both YES and NO legs.
    carry_rows = conn.execute(
        "SELECT s.cid, s.unpaired_shares, s.cost_basis, "
        "  s.yes_shares, s.no_shares "
        "FROM inventory_snapshots s "
        "WHERE s.id = ("
        "  SELECT s2.id FROM inventory_snapshots s2 "
        "  WHERE s2.cid = s.cid AND s2.ts <= ? "
        "  ORDER BY s2.ts DESC, s2.id DESC LIMIT 1"
        ")",
        (start_ts,),
    ).fetchall()
    # Per-cid opening position.
    carry_unpaired: dict[str, float] = {}   # signed: + = long YES
    carry_cost: dict[str, float] = {}       # USD total cost of carry-in UNPAIRED position
    carry_yes: dict[str, float] = {}        # total YES shares at open
    carry_no: dict[str, float] = {}         # total NO shares at open
    carry_paired_value: dict[str, float] = {}  # $1 per pre-window pair — NOT window PnL
    for r in carry_rows:
        cid = r[0]
        yes_s = float(r[3] or 0.0)
        no_s = float(r[4] or 0.0)
        if yes_s < 1e-9 and no_s < 1e-9:
            continue  # effectively flat — no carry-in
        carry_yes[cid] = yes_s
        carry_no[cid] = no_s
        unpaired_val = float(r[1] or 0.0)
        if abs(unpaired_val) > 1e-9:
            if r[2] is None:
                # cost_basis = NULL on a non-zero unpaired position means we
                # cannot know the true carry-in cost, so the window PnL would
                # be biased — "cost unknown" inflates ending_value to profit.
                # Mark the market incomplete and propagate no outcome.
                carry_unpaired[cid] = unpaired_val
                carry_cost[cid] = None  # distinctive marker for step 9
            else:
                carry_cost[cid] = float(r[2]) * abs(unpaired_val)
        # Pre-window paired shares have $1/pair intrinsic value independent of any
        # window fills.  When they are merged during the window the merge cashflow
        # of $1/pair is the realization of this already-held value, not new PnL.
        # We record it here and subtract from trading_pnl in step 9.
        paired_open = min(yes_s, no_s)
        if paired_open > 1e-9:
            carry_paired_value[cid] = paired_open  # $1 per pair

    # ---- 4. ending inventory snapshot (within window, for MTM mid) ----
    # The mid comes from the independently observed order-book YES mid stored
    # in ``inventory_snapshots.yes_mid``.  When this is NULL the book was
    # unavailable at snapshot time — the market is incomplete, regardless of
    # what fill prices say.  We deliberately do NOT derive mid from
    # exposure_usd because exposure_usd includes paired-position components
    # and falls back to 0.5 when the book is silent, which would masquerade
    # as a verified book observation.
    #
    # When ``snapshot_max_age_seconds`` is set, the ending snapshot must be
    # no older than that threshold relative to ``end_ts``.  A stale snapshot
    # (e.g. from a market we stopped sampling days ago) is NOT a valid MTM
    # anchor and the market is treated as incomplete.
    end_inv_rows = conn.execute(
        "SELECT s.cid, s.unpaired_shares, s.yes_mid, s.book_updated_ts "
        "FROM inventory_snapshots s "
        "WHERE s.id = ("
        "  SELECT s2.id FROM inventory_snapshots s2 "
        "  WHERE s2.cid = s.cid AND s2.ts >= ? AND s2.ts < ? "
        "  ORDER BY s2.ts DESC, s2.id DESC LIMIT 1"
        ")",
        (start_ts, end_ts),
    ).fetchall()
    end_mid_by_cid: dict[str, float] = {}
    for r in end_inv_rows:
        cid = r[0]
        unpaired = float(r[1] or 0.0)
        mid = r[2]
        book_ts = float(r[3] or 0.0)
        if abs(unpaired) > 1e-9 and mid is not None and 0.0 < mid < 1.0:
            if snapshot_max_age_seconds is not None:
                age = end_ts - book_ts
                if book_ts <= 0 or age > snapshot_max_age_seconds:
                    # Stale or missing book timestamp — mid is not verified fresh.
                    continue  # market is incomplete
            end_mid_by_cid[cid] = float(mid)

    # ---- 5. collect all CIDs ----
    cids: set[str] = set()
    for r in fill_rows:
        cids.add(r[0])
    for r in merge_rows:
        cids.add(r[0])
    for r in end_inv_rows:
        cids.add(r[0])
    for r in reward_rows:
        cids.add(r[0])
    for cid in carry_unpaired:
        cids.add(cid)
    for cid in carry_yes:
        cids.add(cid)

    if not cids:
        return []

    # ---- 6. aggregate per CID ----
    # Start from carry-in total position (paired + unpaired) so that pre-window
    # paired shares are correctly offset when merged during the window.  Without
    # this a market that enters with 10 pairs and merges them during the window
    # would see net_yes = net_no = -10 after the merge — a phantom short.
    agg: dict[str, dict[str, float]] = {}
    for cid in cids:
        agg[cid] = {
            "net_yes": carry_yes.get(cid, 0.0),
            "net_no": carry_no.get(cid, 0.0),
            "buys": 0.0, "sells": 0.0, "fees": 0.0,
            "merged_shares": 0.0,
        }

    for row in fill_rows:
        cid, side, taker, exit_, price, size, merged, fee = row
        a = agg[cid]
        side_key = str(side).upper()
        if exit_:
            a["sells"] += (price or 0.0) * (size or 0.0)
            if side_key == "YES":
                a["net_yes"] -= size or 0.0
            else:
                a["net_no"] -= size or 0.0
        else:
            a["buys"] += (price or 0.0) * (size or 0.0)
            if side_key == "YES":
                a["net_yes"] += size or 0.0
            else:
                a["net_no"] += size or 0.0
        a["fees"] += fee or 0.0
        a["merged_shares"] += merged or 0.0

    # Apply merges (reduce both sides equally)
    merge_by_cid: dict[str, float] = {r[0]: r[1] for r in merge_rows}
    for cid, pairs in merge_by_cid.items():
        if cid not in agg:
            agg[cid] = {
                "net_yes": 0.0, "net_no": 0.0,
                "buys": 0.0, "sells": 0.0, "fees": 0.0,
                "merged_shares": 0.0,
            }
        agg[cid]["net_yes"] -= pairs
        agg[cid]["net_no"] -= pairs

    # ---- 7. rewards ----
    reward_by_cid: dict[str, float] = {r[0]: r[1] for r in reward_rows}

    # ---- 8. per-market MTM mid from ending inventory snapshot ----
    # Already computed above as end_mid_by_cid.

    # ---- 9. build outcomes ----
    outcomes: list[MarketOutcome] = []
    for cid in sorted(cids):
        a = agg[cid]
        # Window-only position (does NOT include carry-in)
        win_net_yes = a["net_yes"]
        win_net_no = a["net_no"]
        buy_cash = a["buys"]
        sell_cash = a["sells"]
        fees = a["fees"]
        merged_cash = merge_by_cid.get(cid, 0.0)

        # Total position = carry-in (total yes/no from snapshot) + window activity.
        # carry_yes/carry_no already include the paired portion, so a pre-window
        # pair that gets merged during the window correctly nets to 0 yes/0 no.
        total_net_yes = win_net_yes  # already seeded with carry_yes
        total_net_no = win_net_no    # already seeded with carry_no

        # Window cash PnL (from window fills/merges only)
        cash_pnl = merged_cash + sell_cash - buy_cash - fees

        # Paired value: unmerged balanced YES+NO pairs at $1/pair
        paired_shares = max(min(total_net_yes, total_net_no), 0.0)
        paired_value = paired_shares * 1.0

        # Unpaired shares (absolute net imbalance)
        unpaired_signed = total_net_yes - total_net_no
        unpaired = abs(unpaired_signed)

        evidence: list[str] = []
        carry_in_yes = carry_unpaired.get(cid, 0.0)
        carry_in_cost = carry_cost.get(cid, 0.0)
        carry_yes_open = carry_yes.get(cid, 0.0)
        carry_no_open = carry_no.get(cid, 0.0)
        carry_paired = carry_paired_value.get(cid, 0.0)

        # cost_basis missing on a non-zero carry-in unpaired position.
        # Without the real cost, ending_value would include the fair-value
        # change of a position whose entry cost we don't know —
        # misattributing mark-to-market to the window.
        if carry_in_cost is None:
            evidence.append("carry_in_cost_unknown")
            outcomes.append(MarketOutcome(
                cid=cid,
                state="incomplete",
                buy_cash_usd=buy_cash,
                sell_cash_usd=sell_cash,
                merge_cash_usd=merged_cash,
                fees_usd=fees,
                held_pairs=paired_shares,
                unpaired_shares=unpaired,
                unpaired_mtm_usd=None,
                trading_pnl_usd=None,
                market_reward_usd=reward_by_cid.get(cid),
                net_outcome_usd=None,
                evidence_flags=tuple(evidence),
                carry_in_cost_usd=None,
            ))
            continue

        # Determine MTM if a persistent book mid is available.
        # The mid comes from inventory_snapshots (recorded from the live
        # order book), NOT from a single fill price.  A fill at $0.40 when
        # the book is 0.39/0.41 does not make 0.40 a "mid" — it's the taker
        # crossing the spread, exactly the wrong datum for MTM.
        unpaired_mtm: float | None = None
        mid = end_mid_by_cid.get(cid)
        if unpaired_signed != 0 and mid is not None and 0.0 < mid < 1.0:
            if unpaired_signed > 0:
                unpaired_mtm = unpaired * mid
            else:
                unpaired_mtm = unpaired * (1.0 - mid)

        has_activity = (abs(buy_cash) > 1e-9 or abs(sell_cash) > 1e-9
                        or merged_cash > 1e-9 or abs(unpaired) > 1e-9
                        or abs(carry_in_yes) > 1e-9
                        or carry_yes_open > 1e-9 or carry_no_open > 1e-9)
        if not has_activity:
            continue

        if abs(carry_in_yes) > 1e-9:
            evidence.append(f"carry_in_net_yes_{carry_in_yes:.1f}")
        if carry_yes_open > 1e-9 or carry_no_open > 1e-9:
            evidence.append(f"carry_in_pos_{carry_yes_open:.1f}y_{carry_no_open:.1f}n")

        reward = reward_by_cid.get(cid)

        # PnL: window-only value change.
        # = (ending_value - carry_in_cost - carry_in_paired_value) + window_cash_pnl
        # where ending_value = paired_value + unpaired_mtm
        # This isolates only the value change during [start_ts, end_ts).
        #
        # carry_in_paired_value is the $1/pair value of pre-window pairs (held
        # at open).  When those pairs are merged during the window the merge
        # cashflow is NOT new profit — it's realization of already-held value.
        # Without this subtraction, a market entering with 10 pairs and merging
        # them all during the window would show +$10 PnL from thin air.
        if unpaired_mtm is not None or abs(unpaired) < 1e-9:
            ending_value = paired_value + (unpaired_mtm or 0.0)
            trading_pnl = ending_value - carry_in_cost - carry_paired + cash_pnl
            net_outcome: float | None = trading_pnl + (reward or 0.0)
        else:
            trading_pnl = None
            net_outcome = None

        if abs(unpaired) < 1e-9 and abs(total_net_yes) < 1e-9 and abs(total_net_no) < 1e-9:
            state: str = "realized"
            evidence.append("inventory_closed")
        elif abs(unpaired) < 1e-9 and paired_shares > 1e-9:
            state = "paired_unmerged"
            evidence.append("balanced_yes_no_unmerged")
            evidence.append(f"paired_shares_{paired_shares:.1f}")
        elif abs(unpaired) > 1e-9 and unpaired_mtm is not None:
            state = "unpaired_marked"
            evidence.append("unpaired_with_mtm")
        else:
            state = "incomplete"
            if abs(unpaired) > 1e-9:
                evidence.append("unpaired_no_persistent_mid")
            else:
                evidence.append("insufficient_data")
            trading_pnl = None
            net_outcome = None

        if reward is not None:
            evidence.append(f"market_reward_{reward:.4f}")

        outcomes.append(MarketOutcome(
            cid=cid,
            state=state,
            buy_cash_usd=buy_cash,
            sell_cash_usd=sell_cash,
            merge_cash_usd=merged_cash,
            fees_usd=fees,
            held_pairs=paired_shares,
            unpaired_shares=unpaired,
            unpaired_mtm_usd=unpaired_mtm,
            trading_pnl_usd=trading_pnl,
            market_reward_usd=reward,
            net_outcome_usd=net_outcome,
            evidence_flags=tuple(evidence),
            carry_in_cost_usd=carry_in_cost,
        ))

    return outcomes
