"""Orchestrator CLI.

    python -m pmbot.main scan         # show current best reward markets
    python -m pmbot.main run          # run the market maker (paper or live per config)
    python -m pmbot.main report       # daily PnL decomposition from metrics.db
    python -m pmbot.main trades       # recent fill log from metrics.db
    python -m pmbot.main performance  # per-market breakdown for tuning
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import contextlib
import csv
import json
import logging
import math
import os
import queue
import time
try:
    import tomllib
except ImportError:  # Python < 3.11
    import tomli as tomllib
from datetime import date, datetime, timedelta, timezone
from logging.handlers import QueueHandler, QueueListener
from pathlib import Path

import yaml
from dotenv import load_dotenv
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from . import gamma, reward_exit, strategy
from .books import Book, BookTracker
from .brokers import LiveBroker, PaperBroker
from .controller import AdaptiveController
from .metrics import MetricsStore
from .recovery import choose_recovery_action, RecoveryQuote
from .reward_exit import (
    RewardExitBatch, TakeFill, create_batch,
    compute_sell_target, transition_to_seal_pending,
    transition_to_closed, transition_to_manual_hold,
    remaining_take, remaining_exit,
)
from .risk import MarketGuards, MarkoutTracker, RiskAction, RiskManager

console = Console()
log = logging.getLogger("pmbot")
_LOG_LISTENER: QueueListener | None = None
BEIJING_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")

LOOP_SECONDS = 2.0
REWARD_SAMPLE_SECONDS = 60.0
STATUS_SECONDS = 30.0
MINUTES_PER_DAY = 1440.0
POSITION_REFRESH_SECONDS = 12.0
MERGE_CHECK_SECONDS = 60.0
FLATTEN_RETRY_SECONDS = 15.0
MIN_TAKER_SHARES = 5.0
# Refresh realized rewards every 30s — matches the status-print cadence so the
# report is at most one tick stale. Each fetch is two light API calls (today +
# yesterday) on a background thread, so it never blocks quoting. Rewards accrue
# in ~1-min epochs, so this already polls at/above the source's update rate;
# going faster just re-pulls identical data.
REALIZED_REWARD_FETCH_SECONDS = 30.0
SCAN_RETRY_SECONDS = 60.0
# When a market trips its guard, rotate into the next-best market instead of
# leaving the slot idle for the whole cooldown. Debounced so a burst of trips
# can't thrash the book-tracker (each rotation resubscribes the feed).
ROTATE_MIN_INTERVAL_SECS = 60.0
# When the live quote set holds fewer markets than the active tier's top_n
# (e.g. a transient scan/API hiccup left a slot empty), rescan at this cadence
# instead of waiting the full refresh_minutes — recovers an unfilled slot in
# ~1-2 min rather than up to 30. Debounced so we don't rescan every loop when
# the market universe genuinely only has fewer than top_n eligible books.
UNFILLED_RESCAN_INTERVAL_SECS = 90.0
# Only rotate OUT of a tripped market if it is essentially flat — a tripped
# market still holding inventory stays in the set so the normal de-risk/exit
# path manages it (dropping it would force an immediate liquidation).
ROTATE_FLAT_USD = 1.0


def _build_live_notifier(cfg: dict):
    """Build the optional, asynchronous notifier for live fills only."""
    settings = (cfg.get("notifications") or {}).get("dingtalk") or {}
    if not settings.get("enabled", False):
        return None
    webhook_url = os.environ.get("DINGTALK_WEBHOOK_URL", "").strip()
    if not webhook_url:
        log.warning("钉钉通知已启用，但 DINGTALK_WEBHOOK_URL 未配置")
        return None
    from .dingtalk import DingTalkNotifier
    return DingTalkNotifier(webhook_url, os.environ.get("DINGTALK_SECRET") or None)


class BeijingFormatter(logging.Formatter):
    """Format runtime log timestamps explicitly in Asia/Shanghai time."""

    def formatTime(self, record, datefmt=None):  # noqa: N802 - logging API name
        timestamp = datetime.fromtimestamp(record.created, BEIJING_TZ)
        return timestamp.strftime(datefmt or "%Y-%m-%d %H:%M:%S")


class DailyFileHandler(logging.FileHandler):
    """Append each record to its date-named log without renaming an open file.

    Windows cannot rename a file while another bot process has it open.  Unlike
    TimedRotatingFileHandler, this handler switches directly to a new daily
    filename, so independent pmbot instances can share a log directory.
    """

    def __init__(self, directory: Path, *, encoding: str = "utf-8") -> None:
        self.directory = directory.resolve()
        now = datetime.now(BEIJING_TZ)
        self._day: date = now.date()
        super().__init__(self._filename_for(now), encoding=encoding)

    def _filename_for(self, timestamp: datetime) -> Path:
        return self.directory / f"pmbot.{timestamp:%Y-%m-%d}.log"

    def emit(self, record: logging.LogRecord) -> None:
        timestamp = datetime.fromtimestamp(record.created, BEIJING_TZ)
        if timestamp.date() != self._day:
            if self.stream is not None:
                self.stream.flush()
                self.stream.close()
            self.baseFilename = os.fspath(self._filename_for(timestamp))
            self.stream = self._open()
            self._day = timestamp.date()
        super().emit(record)


def stop_logging() -> None:
    """Drain and stop the asynchronous runtime-log writer, if configured."""
    global _LOG_LISTENER
    if _LOG_LISTENER is not None:
        _LOG_LISTENER.stop()
        for handler in _LOG_LISTENER.handlers:
            handler.close()
        _LOG_LISTENER = None


def configure_logging(log_dir: Path | str = "logs") -> logging.Logger:
    """Configure console and permanent daily-rotated runtime logs."""
    global _LOG_LISTENER
    stop_logging()
    directory = Path(log_dir)
    directory.mkdir(parents=True, exist_ok=True)
    file_handler = DailyFileHandler(directory, encoding="utf-8")
    file_handler.setFormatter(BeijingFormatter(
        "%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    log_queue: queue.SimpleQueue[logging.LogRecord] = queue.SimpleQueue()
    _LOG_LISTENER = QueueListener(log_queue, file_handler)
    _LOG_LISTENER.start()
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        handlers=[RichHandler(console=console, show_path=False), QueueHandler(log_queue)],
        force=True,
    )
    return logging.getLogger()


atexit.register(stop_logging)


def hours_to_end(market: gamma.Market, now: float) -> float | None:
    if market.end_date is None:
        return None
    return (market.end_date.timestamp() - now) / 3600.0


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def cmd_scan(cfg: dict) -> None:
    sc = cfg["scanner"]
    mode = str(sc.get("ranking_mode", "density"))
    if mode == "capture":
        gamma_val = sc.get("competition_gamma", 0.027)
        title = f"Top reward markets (expected captured reward, γ={gamma_val})"
    else:
        title = "Top reward markets (pool / liquidity)"
    markets = gamma.scan(cfg)
    table = Table(title=title)
    for col in ("Market", "Mid", "Pool/day", "Liquidity", "Fee",
                "Min size", "Band", "Density", "Capture/d", "Score"):
        table.add_column(col)
    for m in markets:
        capture_str = f"${m.capture:.2f}" if mode == "capture" else "-"
        table.add_row(
            m.question[:55], f"{m.mid_hint:.2f}", f"${m.daily_pool:,.0f}",
            f"${m.liquidity:,.0f}", f"{m.fee_bps}bps",
            f"{m.min_size:.0f} sh", f"{m.max_spread_cents}c",
            f"{m.density:.4f}", capture_str, f"{m.score:.4f}",
        )
    console.print(table)

def _compute_sell_realized_loss(
        broker, m: gamma.Market, cid: str,
        basis: float | None, since_ts: float,
) -> float:
    """Sum actual loss from exit fills for *cid* that occurred >= *since_ts*.

    Loss formula matches choose_recovery_action's sell_original path:
        loss_per_share = max(0, basis - fill_price + fee_per_share)

    The reservation uses a conservative taker-fee assumption for budgeting;
    actual fills are typically maker (no fee).  We charge a fee only when
    the broker recorded the fill as taker, so the closed-episode total
    reflects real execution rather than a worst-case estimate.
    """
    if basis is None:
        return 0.0
    fills = getattr(broker, "fills_log", [])
    total = 0.0
    for entry in fills:
        if (entry.get("cid") != cid
                or not entry.get("exit")
                or entry.get("ts", 0.0) < since_ts):
            continue
        price = entry.get("price")
        size = entry.get("size")
        if price is None or size is None:
            continue
        price_f = float(price)
        size_f = float(size)
        fee = 0.0
        if entry.get("taker"):
            fee = m.fee_bps / 10_000.0 * (price_f * (1.0 - price_f)) ** m.fee_exponent
        loss_per_share = max(0.0, basis - price_f + fee)
        total += loss_per_share * size_f
    return total


def _metrics_store(cfg: dict, *, read_only: bool = False) -> MetricsStore:
    m = cfg.get("metrics") or {}
    db_path = m.get("db_path", "data/metrics.db")
    # Paper mode uses a separate DB so simulated data doesn't mix with live.
    if cfg.get("mode") == "paper" and "db_path" not in m:
        db_path = "data/metrics_paper.db"
    return MetricsStore(db_path,
                        trades_log=m.get("trades_log"),
                        inception_date=m.get("inception_date"),
                        read_only=read_only)


def cmd_report(cfg: dict, date: str | None = None) -> None:
    store = _metrics_store(cfg)
    report = store.daily_report(date)
    rewards = store.reward_totals(date)
    ledger = store.trading_pnl_ledger(date)
    store.close()
    table = Table(title=f"PnL report — {report['date']}")
    table.add_column("Component")
    table.add_column("USD")
    for key, label in [
        ("merge_proceeds_usd", "Merge proceeds (gross, $1/pair)"),
        ("buys_usd", "Buys (gross cash out)"),
        ("sells_usd", "Exits/sells (gross cash in)"),
        ("fees_usd", "Fees paid"),
        ("trading_pnl_usd", "Trading P&L (net, ledger)"),
        ("est_rewards_usd", "Est. rewards"),
        ("realized_rewards_usd", "Realized rewards"),
        ("equity_pnl_usd", "Equity PnL"),
    ]:
        table.add_row(label, f"${report[key]:+.4f}")
    table.add_row("Maker fills", str(report["maker_fills"]))
    table.add_row("In-band uptime", f"{report['uptime_pct']:.1f}%")
    console.print(table)

    rec = report.get("recovery", {})
    if rec is not None:
        rec_table = Table(title="Recovery stats — today")
        rec_table.add_column("Metric")
        rec_table.add_column("Count")
        rec_table.add_row("Recovery skips", str(rec.get("total_skips", 0)))
        rec_table.add_row("Recovery quotes placed", str(rec.get("quotes_placed", 0)))
        rec_table.add_row("Forced hedges", str(rec.get("forced_hedges", 0)))
        if rec.get("hedge_success_rate") is not None:
            rec_table.add_row("Hedge success rate", f"{rec['hedge_success_rate']:.0%}")
        else:
            rec_table.add_row("Hedge success rate", "—")
        if rec.get("premium_avg_cents") is not None:
            rec_table.add_row("Avg premium over cap", f"{rec['premium_avg_cents']:+.2f}¢")
        if rec.get("premium_max_cents") is not None:
            rec_table.add_row("Max premium over cap", f"{rec['premium_max_cents']:+.2f}¢")
        if rec.get("skips_by_reason"):
            for reason, n in sorted(rec["skips_by_reason"].items(), key=lambda x: -x[1]):
                rec_table.add_row(f"  └ skip: {reason}", str(n))
        console.print(rec_table)

    score = Table(title="Scoreboard — rewards vs trading P&L")
    score.add_column("Metric")
    score.add_column("All-time", justify="right")
    score.add_column("Selected day" if date else "Last 24h", justify="right")
    score.add_row("Realized rewards (exact)",
                  f"${rewards['realized_total']:+.2f}",
                  f"${rewards['realized_24h']:+.2f}")
    score.add_row("Trading P&L (ledger: merges+sells-buys-fees)",
                  f"${ledger['mtm_total']:+.2f} mtm",
                  f"${ledger['realized_24h']:+.2f}")
    console.print(score)
    console.print(
        f"[dim]Both figures are from logged cashflows. Trading P&L (ledger) is the "
        f"ground-truth match for your Polymarket history (sum of +/- trades - "
        f"deposits - rewards): realized ${ledger['realized_total']:+.2f} all-time "
        f"plus the current inventory mark ${ledger['inventory_usd']:.2f} = "
        f"${ledger['mtm_total']:+.2f} mark-to-market. Realized "
        f"{'selected-day' if date else '24h'} reads low while "
        f"bought pairs await merge/resolution. For the audited bottom line compare "
        f"deposits vs wallet balance.[/]"
    )


def _fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def cmd_trades(cfg: dict, limit: int, hours: float | None,
               export_csv: str | None) -> None:
    store = _metrics_store(cfg)
    since = time.time() - hours * 3600 if hours is not None else None
    fills = store.recent_fills(limit=limit, since_ts=since)
    store.close()
    if export_csv:
        with open(export_csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["time_utc", "market", "type", "side", "price", "size",
                        "merged", "fee_usd", "cid"])
            for fill in reversed(fills):
                if fill["exit"]:
                    kind = "exit"
                elif fill["taker"]:
                    kind = "taker"
                else:
                    kind = "maker"
                w.writerow([
                    _fmt_ts(fill["ts"]), fill["market"], kind, fill["side"],
                    f"{fill['price']:.4f}", f"{fill['size']:.1f}",
                    f"{fill['merged']:.1f}", f"{fill['fee']:.4f}", fill["cid"],
                ])
        console.print(f"exported {len(fills)} fills to {export_csv}")
        return
    if not fills:
        console.print("no fills recorded yet — run the bot in paper mode first")
        return
    table = Table(title="Recent fills")
    table.add_column("Time (UTC)")
    table.add_column("Market", overflow="fold")
    for col in ("Type", "Side", "Price", "Size", "Merged", "Fee"):
        table.add_column(col)
    for fill in fills:
        if fill["exit"]:
            kind = "exit"
        elif fill["taker"]:
            kind = "taker"
        else:
            kind = "maker"
        table.add_row(
            _fmt_ts(fill["ts"]),
            fill["market"],
            kind,
            fill["side"],
            f"{fill['price']:.3f}",
            f"{fill['size']:.0f}",
            f"{fill['merged']:.0f}" if fill["merged"] else "—",
            f"${fill['fee']:.2f}" if fill["fee"] else "—",
        )
    console.print(table)


def cmd_performance(cfg: dict, date: str | None) -> None:
    store = _metrics_store(cfg)
    report = store.performance_report(date)
    store.close()
    summary = report["summary"]
    # Show recovery totals in summary line if any exist
    rec_sum = summary.get("recovery", {})
    extra = ""
    if rec_sum.get("total_skips") or rec_sum.get("quotes_placed") or rec_sum.get("forced_hedges"):
        extra = (f"  recovery skips {rec_sum['total_skips']}  "
                 f"quotes {rec_sum['quotes_placed']}  "
                 f"forced hedges {rec_sum['forced_hedges']}")
    console.print(
        f"[bold]Session summary — {report['date']}[/]  "
        f"equity PnL ${summary['equity_pnl_usd']:+.2f}  "
        f"spread ${summary['spread_capture_usd']:+.2f}  "
        f"hedges ${summary['hedge_cost_usd']:+.2f}  "
        f"fees ${summary['fees_usd']:+.2f}  "
        f"est. rewards ${summary['est_rewards_usd']:+.4f}  "
        f"realized rewards ${summary['realized_rewards_usd']:+.4f} (account)  "
        f"maker fills {summary['maker_fills']}  "
        f"uptime {summary['uptime_pct']:.1f}%"
        + extra
    )


    shadow = report["shadow_selection"]
    if shadow["status"] == "no_shadow_scan_data":
        console.print("[dim]Net shadow candidates: no shadow scan data for this UTC date.[/]")
    else:
        shadow_table = Table(title="Net shadow candidates (observation only)")
        shadow_table.add_column("Legacy top N")
        shadow_table.add_column("Score")
        shadow_table.add_column("Shadow top N")
        shadow_table.add_column("Expected net/hr")
        legacy_top = shadow["legacy_top"]
        shadow_top = shadow["shadow_top"]
        for i in range(max(len(legacy_top), len(shadow_top))):
            old = legacy_top[i] if i < len(legacy_top) else None
            new = shadow_top[i] if i < len(shadow_top) else None
            shadow_table.add_row(
                (old["market"] or old["cid"][:12]) if old else "—",
                f"{old['legacy_score']:.4f}" if old else "—",
                (new["market"] or new["cid"][:12]) if new else "—",
                f"${new['net_shadow_score']:+.4f}" if new else "—",
            )
        console.print(shadow_table)
    markets = report["markets"]
    if not markets:
        console.print("no per-market activity yet — run the bot in paper mode first")
        return
    console.print("[bold]Markets (full question / condition ID)[/]")
    for index, m in enumerate(markets):
        if index:
            console.print("─" * 72)
        console.print(f"  {m['market'] or '—'}\n    {m['cid']}")
    table = Table(title=f"Per-market performance — {report['date']}", show_lines=True)
    table.add_column("市场", overflow="fold")
    for col in ("成交", "资金", "收益", "风险"):
        table.add_column(col)
    table.add_column("恢复 / 证据", overflow="fold", min_width=18)
    for m in markets:
        markout = "—"
        if m["markout_cents"] is not None:
            markout = f"{m['markout_cents']:+.1f}c (n={m['markout_n']})"
        reco = ""
        if m.get("recovery_skips") or m.get("recovery_quotes") or m.get("forced_hedges"):
            reco = f"{m.get('recovery_skips',0)}s/{m.get('recovery_quotes',0)}q/{m.get('forced_hedges',0)}h"
        event = m.get("last_inventory_event")
        if event:
            reco = f"{reco} {event}".strip()
        terminal = m.get("inventory_terminal_status")
        if terminal:
            reco = f"{reco} -> {terminal}".strip()
        if m.get("reward_attribution_status") == "attributed":
            calibration = m.get("reward_calibration_ratio")
            reward_evidence = f"reward ${m['realized_rewards_usd']:.2f}"
            if calibration is not None:
                reward_evidence += f"/{calibration:.0%}"
            reco = f"{reco} {reward_evidence}".strip()
        elif m.get("reward_attribution_status") == "account_total_only":
            reco = f"{reco} reward account-only".strip()
        if m.get("cashflow_attribution_status") == "mixed_with_carry_in":
            reco = (f"{reco} carry merge<={m['cross_day_merge_pairs_upper_bound']:.0f} "
                    f"exit<={m['cross_day_exit_shares_upper_bound']:.0f}").strip()
        table.add_row(
            m["market"] or m["cid"][:12],
            f"挂/吃/退 {m['maker_fills']}/{m['taker_fills']}/{m['exits']}",
            f"买/合/盈亏 ${m['buy_cost_usd']:.2f}/${m['merge_proceeds_usd']:.2f}/"
            f"${m['trading_pnl_usd']:+.2f}",
            f"奖/净 ${m['est_rewards_usd']:+.2f}/${m['net_pnl_est_usd']:+.2f}",
            f"对冲/费 ${m['hedge_cost_usd']:.2f}/${m['fees_usd']:.2f}\n"
            f"偏离 {markout}\n在线 {m['uptime_pct']:.0f}%",
            reco,
        )
    console.print(table)
    console.print(
        "\n[dim]Est Net = trading cashflow + estimated reward; it excludes "
        "market-level realized rewards and inventory MTM. Account rewards are not "
        "allocated to markets without an explicit market-level source. A terminal status needs "
        "a flat snapshot plus enough observed merge/exit/hedge quantity; otherwise "
        "it remains unresolved. Carry means today's closing cashflow may include "
        "pre-day inventory, so it is not attributed to today's selection.[/]"
    )


def cmd_quote_risk_report(cfg: dict, date: str | None) -> None:
    """Display active adverse-selection guard observations for one Beijing day."""
    date = date or datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")
    day_start = datetime.strptime(date, "%Y-%m-%d").replace(
        tzinfo=BEIJING_TZ).timestamp()
    store = _metrics_store(cfg, read_only=True)
    report = store.quote_risk_report(
        since_ts=day_start, until_ts=day_start + 86400, mode="active")
    store.close()

    console.print(f"[bold]Quote risk validation — {date} (active, Beijing time)[/]")
    console.print(
        f"intercepted {report['active_intercepted']}  "
        f"avg score {report['avg_score'] if report['avg_score'] is not None else '—'}  "
        f"intercepted avg score "
        f"{report['avg_score_intercepted'] if report['avg_score_intercepted'] is not None else '—'}"
    )
    markout_table = Table(title="Paired markout (cents; active decisions only)")
    for col in ("Horizon", "Intercepted", "Allow", "Negative hit / false-positive"):
        markout_table.add_column(col)
    for horizon, stats in (("30s", report["markout_30s"]),
                           ("300s", report["markout_300s"])):
        intercepted = (f"{stats['intercepted_avg_cents']:+.2f}c "
                       f"(n={stats['intercepted_paired_samples']})"
                       if stats["intercepted_avg_cents"] is not None else "—")
        allowed = (f"{stats['allow_avg_cents']:+.2f}c "
                   f"(n={stats['allow_paired_samples']})"
                   if stats["allow_avg_cents"] is not None else "—")
        rates = (f"{stats['intercepted_neg_hit_rate']:.0%} / "
                 f"{stats['allow_neg_rate']:.0%}"
                 if stats["intercepted_neg_hit_rate"] is not None
                 and stats["allow_neg_rate"] is not None else "—")
        markout_table.add_row(horizon, intercepted, allowed, rates)
    console.print(markout_table)

    if not report["decisions"]:
        console.print("no active quote-risk decisions recorded in this UTC day")
        return
    recent = Table(title="Recent active decisions")
    for col in ("时间（北京）", "市场 / CID", "动作", "分数", "原因"):
        recent.add_column(col, overflow="fold")
    for decision in report["decisions"]:
        recent.add_row(
            datetime.fromtimestamp(decision["ts"], BEIJING_TZ).strftime(
                "%Y-%m-%d %H:%M:%S"),
            f"{decision['market'] or 'Unknown market'}\n{decision['cid']}",
            f"{decision['yes_action']}/{decision['no_action']}",
            f"{decision['score']:.2f}", decision["reason"],
        )
    console.print(recent)


def cmd_reward_calibration(cfg: dict, days: int) -> None:
    """Display a read-only market/day reward calibration shadow report."""
    store = _metrics_store(cfg)
    report = store.reward_calibration_report(days=days)
    store.close()
    rows = report["market_days"]
    summary = report["summary"]
    console.print(
        f"[bold]Reward calibration shadow — {report['start_date']} to "
        f"{report['end_date']}[/]  calibrated {summary['calibrated_market_days']}/"
        f"{summary['market_days']} market-days"
    )
    if not rows:
        console.print("no market reward samples in this window")
        return
    table = Table(title="Per-market daily reward calibration")
    for col in ("UTC Date", "Market", "Estimated $", "Realized $", "Ratio", "In-band", "Quote interruption", "Status"):
        table.add_column(col)
    for row in rows:
        realized = (f"${row['realized_usd']:.4f}"
                    if row["realized_usd"] is not None else "—")
        ratio = (f"{row['calibration_ratio']:.1%}"
                 if row["calibration_ratio"] is not None else "—")
        uptime = (f"{row['uptime_pct']:.0f}%/{row['uptime_samples']}m"
                  if row["uptime_pct"] is not None else "—")
        guard_reasons = ",".join(
            f"{reason}x{count}"
            for reason, count in row["guard_interruption_reasons"].items())
        recovery_reasons = ",".join(
            f"{reason}x{count}"
            for reason, count in row["recovery_skip_reasons"].items())
        interruption = (f"guard {guard_reasons}" if guard_reasons
                        else (f"recovery {recovery_reasons}"
                              if recovery_reasons else "guard unrecorded"))
        table.add_row(row["date"], row["cid"][:16],
                      f"${row['estimated_usd']:.4f}", realized, ratio, uptime,
                      interruption, row["status"])
    console.print(table)
    console.print(
        "[dim]Only explicitly attributed market rewards can be calibrated. "
        "Account-level reward totals and missing estimates remain inconclusive. "
        "Recovery skips and observed guard-pull actions are recorded; a "
        "missing guard event remains unrecorded rather than inferred. This report "
        "does not change selection or quoting.[/]"
    )


def cmd_outcomes(cfg: dict, date: str | None, days: int) -> None:
    """Display a read-only closed-loop per-market outcome completeness report."""
    store = _metrics_store(cfg)
    report = store.outcome_report(date, lookback_days=max(1, days))
    store.close()

    console.print(
        f"[bold]Closed-loop outcomes — {report['date']}"
        f"{' (single day)' if days == 1 else f' ({days} days)'}[/]"
    )
    ratio_str = (f"{report['complete_ratio']:.0%}"
                 if report['complete_ratio'] is not None else "n/a")
    console.print(
        f"Realized: {report['realized_count']}  "
        f"Resolved: {report['resolved_count']}  "
        f"Incomplete: {report['incomplete_count']}  "
        f"Complete ratio: {ratio_str}"
    )
    console.print(
        f"Total trading P&L (complete): ${report['total_trading_pnl_usd']:+.4f}  "
        f"Market rewards (attributed): ${report['market_reward_total_usd']:.4f}  "
        f"Account rewards (separate): ${report['account_reward_total_usd']:.4f}"
    )

    if report["missing_reasons"]:
        reasons_table = Table(title="Missing data reasons (incomplete markets)")
        reasons_table.add_column("Reason")
        reasons_table.add_column("Count")
        for reason, count in sorted(report["missing_reasons"].items(),
                                    key=lambda x: -x[1]):
            reasons_table.add_row(reason, str(count))
        console.print(reasons_table)

    if report["outcomes"]:
        table = Table(title="Complete markets (ranked by net outcome)")
        for col in ("Market (condition_id)", "State", "Trading P&L",
                    "Market reward", "Net outcome", "Evidence"):
            table.add_column(col)
        for o in report["outcomes"]:
            table.add_row(
                o["cid"][:24],
                o["state"],
                f"${o['trading_pnl_usd']:+.4f}" if o["trading_pnl_usd"] is not None else "—",
                f"${o['market_reward_usd']:+.4f}" if o["market_reward_usd"] is not None else "—",
                f"${o['net_outcome_usd']:+.4f}",
                ",".join(o.get("evidence_flags", [])),
            )
        console.print(table)

    if report["incomplete"]:
        incomplete_table = Table(title="Incomplete markets")
        incomplete_table.add_column("Condition ID")
        incomplete_table.add_column("Missing reasons")
        for o in report["incomplete"]:
            incomplete_table.add_row(
                o["cid"][:32],
                ",".join(o.get("evidence_flags", [])),
            )
        console.print(incomplete_table)

    console.print(
        "[dim]Account-level rewards are never allocated to market outcomes. "
        "Incomplete markets have no net_outcome and are excluded from "
        "rankings. Use --days to widen the lookback window (more evidence "
        "resolves more markets).[/]"
    )


def cmd_recovery_history(cfg: dict, cid: str) -> None:
    """Display a read-only recovery/hedge timeline for one condition id."""
    store = _metrics_store(cfg)
    history = store.recovery_history(cid)
    store.close()
    console.print(f"[bold]补仓流程 — {cid}[/]")
    if not history["events"]:
        console.print("未找到该市场的补仓或强平事件。")
    else:
        table = Table(title="补仓与强平决策时间线")
        for column in ("时间（北京时间）", "事件", "原因", "裸仓", "候选/实际价",
                       "成本/硬上限", "费用", "预期单对PnL"):
            table.add_column(column)
        for event in history["events"]:
            ts = datetime.fromtimestamp(event["ts"], BEIJING_TZ).strftime(
                "%Y-%m-%d %H:%M:%S")
            proposed = event["proposed_price"]
            quote = event["quote_price"]
            prices = (f"候选 {proposed:.3f}" if proposed is not None else "候选 —")
            prices += f" / 实际 {quote:.3f}" if quote is not None else " / 实际 —"
            basis = event["cost_basis"]
            cap = event["hard_cap"] if event["hard_cap"] is not None else event["pair_cap"]
            economics = (f"成本 {basis:.3f}" if basis is not None else "成本 —")
            economics += f" / 上限 {cap:.3f}" if cap is not None else " / 上限 —"
            fee = (f"{event['fee_per_share']:.6f}"
                   if event["fee_per_share"] is not None else "—")
            pnl = (f"{event['expected_pair_pnl']:+.6f}"
                   if event["expected_pair_pnl"] is not None else "—")
            table.add_row(ts, event["event"], event["reason"] or "—",
                          f"{event['unpaired']:.6g}", prices, economics, fee, pnl)
        console.print(table)
        for event in history["events"]:
            console.print(
                f"[dim]事件详情 event={event['event']} "
                f"reason={event['reason'] or '—'} "
                f"path={event['recovery_path'] or '—'}[/]"
            )
    inventory = history["inventory"]
    if inventory is None:
        console.print("[dim]最新库存：无本地快照（不能据此断言已配平）。[/]")
    else:
        basis = "—" if inventory["cost_basis"] is None else f"{inventory['cost_basis']:.4f}"
        console.print(
            f"[bold]最新库存[/] 市场={inventory['market'] or '—'} "
            f"状态={inventory['status']} 裸仓={inventory['unpaired_shares']:.6g} "
            f"成本={basis} 敞口=${inventory['exposure_usd']:.4f}"
        )
    console.print(
        "[dim]这是本地审计时间线：quote_placed 是挂单，不是成交；"
        "forced_hedge_filled 是本地成交结果，仍应结合后续库存快照确认配平。[/]"
    )


def cmd_recovery_episodes(cfg: dict, limit: int = 50) -> None:
    """Read-only listing of all recovery episodes with duration/peak/path."""
    store = _metrics_store(cfg)
    episodes = store.list_recovery_episodes(limit=limit)
    store.close()

    if not episodes:
        console.print("[dim]暂无 recovery episode 记录。[/]")
        return

    table = Table(title="Recovery Episodes")
    for col in ("CID", "开始时间（北京）", "持续/秒", "峰值敞口/$",
                "阶段", "路径", "预期损失/$", "状态"):
        table.add_column(col)

    for ep in episodes:
        started = datetime.fromtimestamp(ep["started_ts"], BEIJING_TZ).strftime(
            "%m-%d %H:%M:%S")
        duration = ("—" if ep["duration_secs"] is None
                    else f"{ep['duration_secs']:.0f}")
        peak = f"{ep['peak_abs_exposure_usd']:.2f}"
        path = ep.get("chosen_path") or "—"
        loss = ("—" if ep.get("expected_loss_usd") is None
                else f"{ep['expected_loss_usd']:.4f}")
        status = "closed" if ep["is_closed"] else "open"
        table.add_row(
            ep["cid"][:24], started, duration, peak,
            ep["stage"], path, loss, status,
        )
    console.print(table)


def cmd_recovery_replay(cfg: dict) -> None:
    """Replay old recovery_events into per-CID summaries; compare old vs new."""
    store = _metrics_store(cfg)

    # Build market hints from the current config so the replay can
    # re-run choose_recovery_action() with real fee parameters and
    # token IDs.
    market_hints: dict = {}
    try:
        markets = gamma.scan(cfg)
        for m in markets:
            market_hints[m.condition_id] = m
    except Exception:
        pass

    max_loss = float(
        (cfg.get("risk") or {}).get("recovery_max_loss_usd_per_market", 3.0))
    market_hints["_max_loss_usd"] = max_loss

    replay = store.replay_old_recovery_events(market_hints=market_hints)
    episode_summary = store.recovery_episode_summary()
    store.close()

    if not replay:
        console.print("[dim]暂无历史恢复事件可以回放。[/]")
        return

    # Episode-level summary (new strategy)
    console.print(
        f"[bold]Episode 统计（新策略）[/] "
        f"总计 {episode_summary['total_episodes']} 个 episode，"
        f"其中 {episode_summary['open_episodes']} 个进行中，"
        f"{episode_summary['closed_episodes']} 个已关闭"
    )

    # Old events folded into per-CID summaries
    table = Table(title="旧策略恢复事件折叠回放")
    for col in ("CID", "事件数", "持续/秒", "最大裸仓", "有证据",
                "已完成", "样本报价", "新策略对比"):
        table.add_column(col)

    for r in replay:
        evidence = "否" if r["insufficient_evidence"] else "是"
        filled = "是" if r["filled"] else "否"
        sample = ""
        if r["sample_quotes"]:
            q = r["sample_quotes"][-1]
            sample = (f"报{q['proposed_price']:.3f}"
                      f" @成本{q['cost_basis']:.3f}"
                      if q.get("cost_basis") is not None
                      else f"报{q['proposed_price']:.3f}")
        comp = r.get("comparison")
        comp_str = ""
        if comp is not None:
            if comp.get("status") == "no_market_hints":
                comp_str = "—"
            else:
                comp_str = (
                    f"{comp.get('path','?')} "
                    f"loss={comp.get('expected_loss_usd','?'):.4f}"
                    if comp.get("expected_loss_usd") is not None
                    else f"{comp.get('path','?')}")
        table.add_row(
            r["cid"][:24],
            str(r["event_count"]),
            f"{r['duration_secs']:.0f}",
            f"{r['max_abs_unpaired']:.0f}",
            evidence,
            filled,
            sample,
            comp_str,
        )
    console.print(table)

    # Comparison summary
    with_comp = sum(1 for r in replay if r.get("comparison") and isinstance(
        r["comparison"], dict) and "path" in r["comparison"])
    insufficient = sum(1 for r in replay if r["insufficient_evidence"])
    filled_old = sum(1 for r in replay if r["filled"])
    console.print(
        f"[dim]旧策略回放：{len(replay)} 个唯一 CID，"
        f"其中 {filled_old} 个有成交记录，"
        f"{insufficient} 个证据不足，"
        f"{with_comp} 个可对比。"
        f"[/]"
    )
    if with_comp > 0:
        console.print(
            "[dim]「新策略对比」列由 choose_recovery_action 在旧事件"
            "的双边报价快照上重新运行得出。"
            "[/]"
        )
    console.print(
        "[dim]说明：旧策略将「等待报价→轮询→成交」编码为独立事件，"
        "回放将它们折叠为 per-CID episode。"
        "quote_placed 是挂单（非成交）；forced_hedge_filled 是本地成交结果。"
        "[/]"
    )


class Bot:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.paper = cfg["mode"] != "live"
        self.markets: list[gamma.Market] = []
        self.tracker: BookTracker | None = None
        self.broker = None
        self.userfeed = None
        self.risk: RiskManager | None = None
        self.guards = MarketGuards(cfg)
        self.markouts = MarkoutTracker(cfg)
        self.metrics = _metrics_store(cfg)
        self.controller = AdaptiveController(cfg, self.guards, self.markouts,
                                             self.metrics)
        self._token_market: dict[str, gamma.Market] = {}
        self._size_factors: dict[str, float] = {}
        self._last_scan = 0.0
        self._last_reward_min_size_refresh = 0.0
        self._last_rotate = 0.0
        self._rotate_pending = False
        self._last_reward_sample = 0.0
        self._last_inventory_sample = 0.0
        self._last_status = 0.0
        self._last_pos_refresh = 0.0
        self._last_merge_check = 0.0
        self._last_realized_reward = 0.0
        self._merge_task: asyncio.Task | None = None
        # P2.2 cached snapshot: last successful (shadow_inputs, outcome_report).
        # Refreshed in the background so _rescan never waits for the ledger.
        self._cached_shadow_inputs: dict[str, dict] = {}
        self._cached_outcome_report: dict | None = None
        self._outcome_refresh_task: asyncio.Task | None = None
        self._last_outcome_refresh = 0.0
        self._over_since: dict[str, float] = {}
        self._last_flatten: dict[str, float] = {}
        # P1.3: markout-trip banned markets — 持久化到 data/banned_markets.json
        self._banned_cids: set[str] = set()
        self._banned_path = (
            Path((cfg.get("metrics") or {}).get("db_path", "data/metrics.db")).parent
            / "banned_markets.json"
        )
        self._load_banned_cids()
        # P1.x reward exit batch: per-fill double-take + maker SELL
        r_cfg = cfg.get("risk") or {}
        self._reward_exit_mode: str = r_cfg.get("reward_exit_batch_mode", "shadow")
        self._reward_exit_locked: set[str] = set()  # CID → reward_exit_locked
        self._awaiting_top_n_rescan: set[str] = set()
        self._batch_task_tracker: dict[str, asyncio.Task] = {}  # batch_id → async task
        self._batch_last_take_attempt: dict[str, float] = {}  # batch_id → last attempt ts
        self._batch_take_errors: dict[str, tuple[str, int]] = {}  # batch_id → (last_error, count)
        self._batch_pending_take_until: dict[str, float] = {}
        self._batch_exit_orders: dict[str, object] = {}  # batch_id → RestingOrder
        self._batch_exit_cancel_pending: dict[str, str] = {}  # batch_id → cancelled order_id
        self._batch_sell_failures: dict[str, tuple[float, int]] = {}  # batch_id → (last failure ts, count)
        self._reward_exit_cancel_retries: dict[str, tuple[gamma.Market, float, int]] = {}
        self._recovery_skip_logged_at: dict[str, float] = {}
        self._recovery_phase_logged: dict[str, str] = {}
        self._recovery_pricing: dict[str, dict[str, float | str]] = {}
        self._quote_block_reasons: dict[str, str] = {}
        self._scale = 1.0
        self._was_paused = False
        self._pause_day_active = False
        # TOML file for persisting new market slugs discovered by the scanner.
        self._markets_toml: Path | None = None
        scanner_cfg = cfg.get("scanner") or {}
        if scanner_cfg.get("markets_toml"):
            self._markets_toml = Path(scanner_cfg["markets_toml"])
        # Event-driven quote pulls: guards fire these between loop ticks so we
        # don't stay quoted on an endangered side for up to LOOP_SECONDS.
        self._pull_tasks: set[asyncio.Task] = set()
        self._market_locks: dict[str, asyncio.Lock] = {}
        self.guards.on_trip = self._schedule_market_pull
        self.guards.on_side_block = self._schedule_side_pull

    def _load_banned_cids(self) -> None:
        """从 JSON 文件加载持久化的 banned 市场，使 markout-ban 在重启后不丢失。"""
        try:
            if not self._banned_path.exists():
                return
            data = json.loads(self._banned_path.read_text())
            self._banned_cids = set(data.get("banned_cids", []))
            log.info("加载 %d 个 banned 市场（文件: %s）",
                     len(self._banned_cids), self._banned_path)
        except (json.JSONDecodeError, OSError, ValueError) as e:
            log.warning("无法加载 banned_markets.json: %s", e)

    def _persist_banned_cids(self) -> None:
        """持久化 banned 市场列表，重启后不丢失。"""
        try:
            self._banned_path.parent.mkdir(parents=True, exist_ok=True)
            self._banned_path.write_text(json.dumps({
                "banned_cids": sorted(self._banned_cids),
            }, indent=2))
        except OSError as e:
            log.warning("无法持久化 banned_markets.json: %s", e)

    def _sync_markets_toml(self, markets: list[gamma.Market]) -> None:
        """Append newly discovered market slugs to the TOML file if not already present."""
        if self._markets_toml is None:
            return
        try:
            existing_slugs: set[str] = set()
            if self._markets_toml.exists():
                raw = self._markets_toml.read_text(encoding="utf-8")
                try:
                    data = tomllib.loads(raw)
                except Exception:
                    log.warning("无法解析 %s，跳过 TOML 同步", self._markets_toml)
                    return
                for entry in data.get("markets", []):
                    slug = entry.get("slug", "")
                    if slug:
                        existing_slugs.add(str(slug))
            new_entries: list[str] = []
            for m in markets:
                if m.slug and m.slug not in existing_slugs:
                    new_entries.append(
                        f"\n[[markets]]\n"
                        f'slug    = "{m.slug}"\n'
                        f'profile = "newsom-mm"\n'
                        f"enabled = true\n"
                    )
                    existing_slugs.add(m.slug)
            if new_entries:
                with self._markets_toml.open("a", encoding="utf-8") as f:
                    for entry in new_entries:
                        f.write(entry)
                log.info("已将 %d 个新市场 slug 写入 %s", len(new_entries), self._markets_toml)
        except OSError as e:
            log.warning("无法写入 %s：%s", self._markets_toml, e)

    def _manual_hold_cids(self) -> set[str]:
        """Return markets that the bot must leave entirely to manual handling.

        Includes both config-driven manual_hold_cids and permanently banned CIDs
        (markout-ban + recovery-loss-ban), so every call site gets a single
        authoritative set and banned checks don't scatter.
        """
        risk_cfg = self.cfg.get("risk") or {}
        manual = {
            hex(cid) if isinstance(cid, int) else str(cid)
            for cid in risk_cfg.get("manual_hold_cids") or []
        }
        manual.update(self._banned_cids)
        return manual

    async def run(self) -> None:
        if not self.paper:
            await self._bootstrap_live_broker()
        while True:
            await self._rescan(initial=True)
            if self.markets:
                break
            # A scanner drought must never leave an already-held market
            # unmanaged. It may still need a complement quote, passive exit,
            # or forced hedge even though no new reward market is eligible.
            if not self.paper:
                await asyncio.to_thread(self.broker.refresh_state)
                self._last_pos_refresh = time.time()
                await self._ensure_held_market_books()
            now = time.time()
            await self._run_reward_exit_batch_tick(now)
            await self._manage_inventory(now)
            log.warning("扫描器未找到符合条件的市场，%.0f 秒后重试"
                        "（可适当放宽配置筛选条件）",
                        SCAN_RETRY_SECONDS)
            await asyncio.sleep(SCAN_RETRY_SECONDS)
        assert self.broker and self.risk
        try:
            while True:
                await asyncio.sleep(LOOP_SECONDS)
                now = time.time()
                due_refresh = (now - self._last_scan
                               > self.cfg["scanner"]["refresh_minutes"] * 60)
                # An unfilled slot (fewer held markets than the tier's top_n)
                # recovers on a short cadence instead of waiting for the next
                # full refresh — sticky selection keeps what we hold and just
                # backfills the empty slot with the best fresh candidate.
                top_n = int(self.cfg["scanner"]["top_n_markets"])
                unfilled = (len(self.markets) < top_n
                            and now - self._last_scan > UNFILLED_RESCAN_INTERVAL_SECS)
                if due_refresh or unfilled:
                    await self._rescan()
                elif (self._rotate_pending
                      and now - self._last_rotate > ROTATE_MIN_INTERVAL_SECS):
                    await self._rescan(rotate=True)
                min_size_refresh = float(
                    self.cfg["scanner"].get("reward_min_size_refresh_seconds", 60.0))
                if (min_size_refresh > 0
                        and now - self._last_reward_min_size_refresh >= min_size_refresh):
                    self._last_reward_min_size_refresh = now
                    await self._refresh_reward_min_sizes()
                self.broker.check_crossed_books()
                if not self.paper and now - self._last_pos_refresh >= POSITION_REFRESH_SECONDS:
                    await asyncio.to_thread(self.broker.refresh_state)
                    self._last_pos_refresh = now
                    await self._ensure_held_market_books()
                if not self.paper and now - self._last_merge_check >= MERGE_CHECK_SECONDS:
                    self._last_merge_check = now
                    if self._merge_task is None or self._merge_task.done():
                        min_pairs = float(self.cfg["live"].get("merge_min_pairs", 20))
                        self._merge_task = asyncio.create_task(
                            asyncio.to_thread(self.broker.merge_pairs, min_pairs))
                if (not self.paper and now - self._last_realized_reward
                        >= REALIZED_REWARD_FETCH_SECONDS):
                    self._last_realized_reward = now
                    # Today plus the prior UTC day: a day's rewards finalize
                    # shortly after midnight UTC, so refreshing yesterday keeps
                    # the realized-vs-estimated record accurate.
                    await asyncio.to_thread(
                        self.metrics.backfill_realized_rewards, self.broker.client, 2)

                equity = self.broker.equity()
                self.controller.maybe_apply(now, equity)
                self._scale = self.risk.scale(equity)
                action = self.risk.check(equity, self.broker.total_inventory_usd(),
                                         self._scale)
                self.metrics.record_equity(equity, self.broker.total_inventory_usd())
                observation = self.risk.last_observation
                if action == RiskAction.PAUSE_DAY and not self._pause_day_active:
                    self._pause_day_active = True
                    self.metrics.record_pause_day_event(
                        "triggered", reason="daily_loss_limit", equity=equity,
                        smoothed_equity=float(observation["smoothed_equity"]),
                        day_loss=float(observation["day_loss"]),
                        inventory_usd=float(observation["inventory_usd"]), ts=now)
                    log.warning(
                        "PAUSE_DAY_TRIGGERED equity=%.4f smoothed_equity=%.4f "
                        "day_loss=%.4f inventory=%.4f limit=%.4f "
                        "说明=停止普通双边报价；保留受成本约束的库存回收",
                        equity, float(observation["smoothed_equity"]),
                        float(observation["day_loss"]),
                        float(observation["inventory_usd"]),
                        self.cfg["risk"]["daily_loss_limit_usd"])
                elif action != RiskAction.PAUSE_DAY and self._pause_day_active:
                    self._pause_day_active = False
                    self.metrics.record_pause_day_event(
                        "resumed", reason="new_utc_day", equity=equity,
                        smoothed_equity=float(observation["smoothed_equity"]),
                        day_loss=float(observation["day_loss"]),
                        inventory_usd=float(observation["inventory_usd"]), ts=now)
                    log.info(
                        "PAUSE_DAY_RESUMED equity=%.4f smoothed_equity=%.4f "
                        "day_loss=%.4f inventory=%.4f 说明=新UTC日已恢复普通报价资格",
                        equity, float(observation["smoothed_equity"]),
                        float(observation["day_loss"]),
                        float(observation["inventory_usd"]))
                if now - self._last_inventory_sample >= REWARD_SAMPLE_SECONDS:
                    self._sample_inventory(now)
                    self._last_inventory_sample = now

                if action == RiskAction.KILL:
                    break

                if action in (RiskAction.PAUSE_DAY, RiskAction.PAUSE_QUOTES):
                    if not self._was_paused:
                        await self._broker_call(
                            self.broker.cancel_quotes,
                            exclude_cids=self._manual_hold_cids())
                        self._was_paused = True
                    for m in self.markets:
                        self.metrics.sample_uptime(m.condition_id, False)
                    # Reward-exit batches still need to run during pauses so
                    # active SELL orders and shadow tracking continue.
                    await self._run_reward_exit_batch_tick(now)
                    await self._manage_inventory(now)
                    continue

                self._was_paused = False
                # Run reward-exit tick first so newly created batches lock
                # before _quote_all and _manage_inventory run.
                await self._run_reward_exit_batch_tick(now)
                await self._quote_all()
                await self._manage_inventory(now)

                if now - self._last_reward_sample >= REWARD_SAMPLE_SECONDS:
                    self._sample_rewards()
                    self._last_reward_sample = now
                if now - self._last_status >= STATUS_SECONDS:
                    self._print_status()
                    self._last_status = now
        finally:
            log.info("正在退出，撤销全部订单")
            for task in list(self._pull_tasks):
                task.cancel()
            if self._pull_tasks:
                await asyncio.gather(*self._pull_tasks, return_exceptions=True)
            if self.userfeed:
                await self.userfeed.stop()
            if self._merge_task and not self._merge_task.done():
                with contextlib.suppress(asyncio.CancelledError):
                    await self._merge_task
            await self._broker_call(
                self.broker.cancel_all, exclude_cids=self._manual_hold_cids())
            if self.tracker:
                await self.tracker.stop()
            self._print_status()
            self.metrics.close()

    async def _bootstrap_live_broker(self) -> None:
        """Authenticate and reconcile inventory before the first market scan."""
        if self.broker is not None:
            return
        self.tracker = BookTracker([])
        self.broker = LiveBroker(self.cfg, self.tracker, _build_live_notifier(self.cfg))
        await asyncio.to_thread(self.broker.refresh_state)
        self._last_pos_refresh = time.time()
        self.risk = RiskManager(self.cfg, self.broker.equity())
        from .userfeed import UserFeed
        self.userfeed = UserFeed(self.broker)
        self.broker.metrics = self.metrics
        restore_pending = getattr(self.broker, "restore_pending_batch_take", None)
        if callable(restore_pending) and self.metrics is not None:
            for batch in self.metrics.get_open_reward_exit_batches():
                pending = self.metrics.get_pending_batch_take(batch["batch_id"])
                if pending is not None:
                    restore_pending(pending)
        self.userfeed.start()
        self.tracker.on_trade(self._on_market_trade)
        await self.tracker.start()
        await self._ensure_held_market_books()

    async def _ensure_held_market_books(self) -> None:
        """Subscribe position-only markets before routing them to inventory logic."""
        if self.paper or self.broker is None or self.tracker is None:
            return
        manual_hold = self._manual_hold_cids()
        held = [m for m in self.broker.held_markets() if m.condition_id not in manual_hold]
        for market in held:
            self._token_market[market.yes_token] = market
            self._token_market[market.no_token] = market
        held_tokens = {token for market in held for token in (market.yes_token, market.no_token)}
        missing = [token for token in self.broker.position_tokens()
                   if token in held_tokens and token not in self.tracker.books]
        if not missing:
            return
        log.warning("为库存管理订阅 %d 个持仓代币订单簿",
                    len(missing))
        await self.tracker.resubscribe([*self.tracker.books, *missing])

    async def _broker_call(self, fn, *args, **kwargs):
        """Dispatch broker order ops off the event loop in live mode."""
        if self.paper:
            return fn(*args, **kwargs)
        return await asyncio.to_thread(fn, *args, **kwargs)

    def _market_lock(self, cid: str) -> asyncio.Lock:
        lock = self._market_locks.get(cid)
        if lock is None:
            lock = self._market_locks[cid] = asyncio.Lock()
        return lock

    async def _set_quotes_locked(self, market: gamma.Market,
                                 quotes: list[strategy.Quote],
                                 audit_context: dict[str, dict] | None = None) -> None:
        """Serialize quote ops per market so an event-driven pull cannot race
        a concurrent replace from the main loop."""
        async with self._market_lock(market.condition_id):
            await self._broker_call(self.broker.set_quotes, market, quotes, audit_context)

    def _spawn_pull(self, coro) -> None:
        try:
            task = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:  # no loop (tests / shutdown)
            coro.close()
            return
        self._pull_tasks.add(task)
        task.add_done_callback(self._pull_tasks.discard)

    def _schedule_market_pull(self, cid: str) -> None:
        self._spawn_pull(self._pull_market_quotes(cid))
        # A fresh trip frees (or idles) a quoting slot — ask the loop to look
        # for a replacement market on its next tick.
        if (self.cfg.get("scanner") or {}).get("rotate_on_trip", True):
            self._rotate_pending = True

    def _schedule_side_pull(self, token_id: str) -> None:
        self._spawn_pull(self._pull_side_quote(token_id))

    async def _pull_market_quotes(self, cid: str) -> None:
        """Immediately cancel all quotes in a guard-tripped market."""
        if self.broker is None:
            return
        m = next((mm for mm in self.markets if mm.condition_id == cid), None)
        if m is None:
            return
        async with self._market_lock(cid):
            if not self.broker.open_quotes(m):
                return
            log.warning("市场风控触发，立即撤下“%s”的报价", m.question[:45])
            self.metrics.record_guard_event(
                cid, "market", "market_guard_pull", market=m.question)
            self.metrics.sample_uptime(cid, False)
            await self._broker_call(self.broker.set_quotes, m, [])

    async def _pull_side_quote(self, token_id: str) -> None:
        """Immediately cancel the quote on a blocked side."""
        if self.broker is None:
            return
        m = self._token_market.get(token_id)
        if m is None:
            return
        async with self._market_lock(m.condition_id):
            current = self.broker.open_quotes(m)
            remaining = [q for q in current if q.token_id != token_id]
            if len(remaining) == len(current):
                return
            log.warning("单边保护触发，立即撤下“%s”的 %s 买单",
                        m.question[:45],
                        "YES" if token_id == m.yes_token else "NO")
            self.metrics.record_guard_event(
                m.condition_id, "side", "side_guard_pull", market=m.question)
            await self._broker_call(self.broker.set_quotes, m, remaining)

    def _rotatable_tripped_cids(self) -> set[str]:
        """Currently-quoted markets that are guard-tripped AND flat. These are
        the slots worth rotating out of — a tripped market still holding
        inventory is kept so the de-risk/exit path manages it instead of being
        force-liquidated the moment it drops from the quote set."""
        if not self.broker or not (self.cfg.get("scanner") or {}).get("rotate_on_trip", True):
            return set()
        paused = self.guards.paused_cids(time.time())
        if not paused:
            return set()
        quoted = {m.condition_id: m for m in self.markets}
        out = set()
        for cid in paused:
            m = quoted.get(cid)
            if m is not None and abs(self.broker.net_yes_exposure_usd(m)) < ROTATE_FLAT_USD:
                out.add(cid)
        return out

    def _locked_inventory_markets(self, markets: list[gamma.Market]) -> list[gamma.Market]:
        """Return markets whose unpaired inventory must keep a quote slot."""
        if self.broker is None:
            return []
        return [m for m in markets
                if abs(self.broker.unpaired_shares(m)) >= MIN_TAKER_SHARES]

    def _select_markets(self, ranked: list[gamma.Market],
                        locked: list[gamma.Market] | None = None) -> list[gamma.Market]:
        """Pick the quote set from the full ranked candidate list, stickily.

        For a reward farmer the cost of leaving a market is real (a feed/quote
        gap, lost queue position, ramp-up on the new book), so we do NOT churn
        the set just because the pool÷liquidity ranking reshuffled. A market we
        are already quoting is kept as long as it stays eligible — guard-tripped
        markets         are already removed upstream via ``exclude``, so risk signals
        remain the primary reason a market leaves. A fresh candidate only
        displaces a held one if it beats it by ``swap_score_margin`` AND the
        held market is underperforming (recent in-band uptime below
        ``underperform_uptime_pct``) — a market farming well is never evicted on
        score alone. With no held markets (startup) this is just the top-N by
        score, as before.
        """
        sc = self.cfg["scanner"]
        top_n = int(sc["top_n_markets"])
        # Inventory is managed through ``broker.held_markets()`` even when a
        # market leaves the quote set.  It must not bypass ranked/sticky
        # selection and become a normal two-sided market merely because it has
        # a recovery order outstanding.  Keep the argument temporarily for
        # callers on older revisions, but deliberately do not use it here.
        del locked
        locked_cids: set[str] = set()
        slots = top_n
        if not bool(sc.get("sticky_swap", True)):
            return [m for m in ranked if m.condition_id not in locked_cids][:slots]
        margin = float(sc.get("swap_score_margin", 0.0))
        by_cid = {m.condition_id: m for m in ranked}
        held = [m.condition_id for m in self.markets if m.condition_id not in locked_cids]
        # Currently-quoted markets still eligible this scan, freshest score first.
        if str(sc.get("selection_mode", "legacy")).lower() == "net_outcome":
            survivors = sorted((by_cid[c] for c in held if c in by_cid),
                               key=lambda m: (m.net_shadow_score, m.score),
                               reverse=True)[:slots]
        else:
            survivors = sorted((by_cid[c] for c in held if c in by_cid),
                               key=lambda m: m.score, reverse=True)[:slots]
        chosen = list(survivors)
        chosen_cids = {m.condition_id for m in chosen}
        survivor_cids = set(chosen_cids)
        # Backfill empty slots with the best candidates we are not already in.
        for m in ranked:
            if len(chosen) >= slots:
                break
            if m.condition_id not in chosen_cids and m.condition_id not in locked_cids:
                chosen.append(m)
                chosen_cids.add(m.condition_id)
        # A held market may only be evicted on score when it is BOTH (a) beaten
        # by a materially-better candidate and (b) actually underperforming —
        # i.e. its recent in-band uptime is low, so it isn't farming the rewards
        # its rank implies. A market farming well at high uptime is protected
        # regardless of how the ranking reshuffled (the anti-churn guarantee).
        # P2.2: When selection_mode is net_outcome, displacement comparisons use
        # net_shadow_score instead of legacy score so a net-result-better
        # candidate isn't blocked by a market with higher legacy density.
        net_mode = str(sc.get("selection_mode", "legacy")).lower() == "net_outcome"
        if margin > 0 and survivor_cids:
            min_uptime = float(sc.get("underperform_uptime_pct", 60.0))
            lookback_min = float(sc.get("underperform_lookback_minutes", 30.0))
            uptime: dict[str, float] = {}
            if self.metrics is not None:
                since_min = int((time.time() - lookback_min * 60.0) // 60)
                uptime = self.metrics.uptime_pct_by_market(since_min)

            def _underperforming(cid: str) -> bool:
                pct = uptime.get(cid)  # absent => too little history => protected
                return pct is not None and pct < min_uptime

            for cand in ranked:  # best first
                if cand.condition_id in chosen_cids:
                    continue
                displaceable = [m for m in chosen
                                if m.condition_id in survivor_cids
                                and _underperforming(m.condition_id)]
                if not displaceable:
                    break  # every held market is performing — never churn
                weak = min(displaceable, key=lambda m: (
                    m.net_shadow_score if net_mode else m.score))
                cand_score = cand.net_shadow_score if net_mode else cand.score
                weak_score = weak.net_shadow_score if net_mode else weak.score
                if cand_score < weak_score * (1.0 + margin):
                    break  # sorted desc — nothing further clears the margin
                chosen.remove(weak)
                chosen.append(cand)
                chosen_cids = (chosen_cids - {weak.condition_id}) | {cand.condition_id}
                survivor_cids.discard(weak.condition_id)
        return chosen

    async def _rescan(self, initial: bool = False, rotate: bool = False,
                      exclude_cids: set[str] | None = None) -> None:
        self._rotate_pending = False
        if rotate:
            self._last_rotate = time.time()
        guard_tripped_cids = set() if initial else self._rotatable_tripped_cids()
        explicit_exclude_cids = exclude_cids or set()
        reward_exit_cids: set[str] = set()
        if self.metrics is not None:
            # Reward-exit locks are rebuilt from durable batches after a restart.
            # Include them before the initial scan so a pending exit cannot retake
            # a normal reward-quote slot during that startup window.
            reward_exit_cids = {
                str(batch["cid"])
                for batch in self.metrics.get_open_reward_exit_batches()
            }
        # Manual-hold markets are excluded from selection; their inventory and
        # orders remain untouched by the bot.
        manual_hold = self._manual_hold_cids()
        recovery_cids: set[str] = set()
        if self.broker is not None:
            recovery_cids = {
                market.condition_id for market in self.broker.held_markets()
                if abs(self.broker.unpaired_shares(market)) >= MIN_TAKER_SHARES
            }
            # Inventory remains managed through held_markets() in _quote_all(),
            # but an unpaired market must release its normal reward quote slot.
        exclude = (
            guard_tripped_cids
            | explicit_exclude_cids
            | reward_exit_cids
            | manual_hold
            | recovery_cids
        )
        log.info(
            "正在扫描奖励市场… (exclude: unique=%d guard_tripped=%d "
            "reward_exit_locked=%d inventory_recovery=%d manual_hold=%d "
            "explicit_exclude=%d)",
            len(exclude),
            len(guard_tripped_cids),
            len(reward_exit_cids),
            len(recovery_cids),
            len(manual_hold),
            len(explicit_exclude_cids),
        )
        # P2.2: read the most recent cached outcome snapshot (refreshed in
        # background AFTER the previous _rescan finished).  When the metrics db
        # is new or the cache is stale, scan() falls back to legacy silently.
        outcome_report = self._cached_outcome_report
        shadow_inputs = self._cached_shadow_inputs
        if outcome_report is None:
            log.info("无缓存净收益报告，使用传统评分")
        ranked = await asyncio.to_thread(
            gamma.scan, self.cfg, exclude, True, shadow_inputs, outcome_report)
        if recovery_cids:
            ranked = [market for market in ranked
                      if market.condition_id not in recovery_cids]
        if not ranked and not exclude_cids:
            if not initial:
                log.warning("重新扫描未找到市场，保留当前市场集合")
            self._last_scan = time.time()
            return
        # Serialise SQLite access: wait for any running background refresh to
        # finish before _process_scan_result writes to the metrics DB.
        if self._outcome_refresh_task and not self._outcome_refresh_task.done():
            try:
                await asyncio.wait_for(self._outcome_refresh_task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                pass
        shadow_cfg = (self.cfg.get("scanner") or {}).get("net_shadow") or {}
        await self._process_scan_result(ranked, initial, manual_hold, recovery_cids, shadow_cfg)
        # Schedule the next background cache refresh now that DB writes are done.
        # This guarantees the refresh never races with record_net_shadow_snapshot.
        now_ts = time.time()
        refresh_interval = float(shadow_cfg.get(
            "outcome_refresh_seconds", 120.0))
        if (self.metrics is not None
                and (self._outcome_refresh_task is None
                     or self._outcome_refresh_task.done())):
            if now_ts - self._last_outcome_refresh >= refresh_interval:
                self._last_outcome_refresh = now_ts
                self._outcome_refresh_task = asyncio.ensure_future(
                    self._refresh_outcome_cache(shadow_cfg))

    async def _refresh_outcome_cache(self, shadow_cfg: dict) -> None:
        """Background: refresh cached shadow_inputs and outcome_report.

        Runs with a 5-second timeout so a locked or slow database never holds
        up the quote/cancel loop.  On failure the cache keeps its last good
        snapshot; ``old cache`` is always better than ``no data`` — the gate
        fallback is the same legacy path either way.
        """
        lookback_hours = float(shadow_cfg.get("lookback_hours", 24.0))
        snapshot_age_secs = float(shadow_cfg.get(
            "snapshot_max_age_seconds", 43200.0))
        today = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        metrics = self.metrics  # capture ref for closure

        def _fetch_both() -> tuple[dict, dict | None]:
            si = metrics.net_shadow_inputs(lookback_hours)
            orpt = metrics.outcome_report(
                today, lookback_days=14,
                snapshot_max_age_seconds=snapshot_age_secs)
            return si, orpt

        deadline = 5.0
        try:
            task = asyncio.ensure_future(asyncio.to_thread(_fetch_both))
            si, orpt = await asyncio.wait_for(task, timeout=deadline)
        except Exception as exc:  # noqa: BLE001
            log.warning("净收益缓存后台刷新失败（%.0fs timeout）：%s", deadline, exc)
            return  # keep last good snapshot

        if si:
            self._cached_shadow_inputs = si
        self._cached_outcome_report = orpt
        log.debug("净收益缓存已刷新：%d shadow cid，outcome=%s",
                  len(si), "ok" if orpt else "none")

    # ── post-scan processing (shared between _rescan and caller) ──

    async def _process_scan_result(self, ranked, initial, manual_hold, recovery_cids, shadow_cfg):
        # P2.1: persist the passive net-economic ranking and shadow scores
        # that gamma.scan() already computed (always-on, regardless of mode).
        # The scores live on market.net_shadow_score / .net_shadow_inputs.
        try:
            self.metrics.record_net_shadow_snapshot(
                ranked, time.time(), {"top_n": self.cfg["scanner"]["top_n_markets"],
                                      "net_shadow": shadow_cfg})
        except Exception as exc:  # noqa: BLE001 - observation must fail open
            log.warning("净收益影子扫描记录失败，原有选择不变：%s", exc)
        # Teach the guards which event each candidate belongs to, then refuse to
        # enter a fresh bracket whose event has a sibling in guard cooldown —
        # correlated neg-risk brackets pick off makers together, so re-entering
        # one mid-cooldown just repeats the loss. Markets we already quote are
        # kept (their inventory is wound down by the de-risk/exit path).
        self.guards.register_markets(ranked)
        held_cids = {m.condition_id for m in self.markets}
        paused_events = self.guards.paused_event_ids(time.time())
        if paused_events:
            ranked = [
                m for m in ranked
                if getattr(m, "event_id", None) not in paused_events
                or m.condition_id in held_cids
            ]
            if not ranked:
                if not initial:
                    log.warning("重新扫描仅找到冷却中的市场，保留当前市场集合")
                self._last_scan = time.time()
                return
        locked = self._locked_inventory_markets(self.markets)
        markets = self._select_markets(ranked, locked)
        ranked_cids = {m.condition_id for m in ranked}

        old_markets = list(self.markets)
        new_cids = {m.condition_id for m in markets}
        old_cids = {m.condition_id for m in old_markets}
        new_tokens = {t for m in markets for t in (m.yes_token, m.no_token)}
        old_tokens = {t for m in old_markets for t in (m.yes_token, m.no_token)}
        set_changed = new_cids != old_cids

        for m in markets:
            log.info("开始报价：%s（奖励池 $%.0f/天，评分 %.3f）",
                     m.question[:60], m.daily_pool, m.score)

        # 将新增市场的 slug 写入 TOML 配置，供人工监督或其他程序使用
        self._sync_markets_toml(markets)

        self.markets = markets
        # A completed reward-exit market may resume ordinary quoting only
        # after it re-enters the current ranked top-N scan.
        self._awaiting_top_n_rescan.difference_update(new_cids & ranked_cids)
        self._token_market = {}
        for m in markets:
            self._token_market[m.yes_token] = m
            self._token_market[m.no_token] = m

        if self.tracker and not initial and not set_changed:
            self._last_scan = time.time()
            self._compute_size_factors()
            return

        carry_books: dict = {}
        if self.broker and not initial:
            for old_m in old_markets:
                if (old_m.condition_id in old_cids - new_cids
                        and old_m.condition_id not in manual_hold
                        and old_m.condition_id not in recovery_cids):
                    async with self._market_lock(old_m.condition_id):
                        try:
                            if hasattr(self.broker, "cancel_quotes_for_market"):
                                await self._broker_call(
                                    self.broker.cancel_quotes_for_market, old_m)
                            else:
                                await self._broker_call(
                                    self.broker.set_quotes, old_m, [])
                        except Exception as exc:  # noqa: BLE001 - a failed cancel must not block rotation
                            log.warning(
                                "MARKET_SWITCH_CANCEL_QUOTES_FAILED cid=%s market='%s': %s; "
                                "继续切换市场",
                                old_m.condition_id, old_m.question[:50], exc,
                            )

        token_ids = list(new_tokens)
        if self.tracker:
            carry_books = {
                t: self.tracker.books[t]
                for t in new_tokens & old_tokens
                if t in self.tracker.books
            }
            if self.broker:
                manual_tokens = {
                    token
                    for market in self.broker.held_markets()
                    if market.condition_id in manual_hold
                    for token in (market.yes_token, market.no_token)
                }
                for t in self.broker.position_tokens():
                    if t in manual_tokens:
                        continue
                    if t not in token_ids:
                        token_ids.append(t)

        if self.tracker is None:
            self.tracker = BookTracker(token_ids, carry=carry_books)
            if initial:
                if self.paper:
                    p = self.cfg.get("paper") or {}
                    self.broker = PaperBroker(
                        self.cfg["capital_usd"], self.tracker,
                        latency_secs=float(p.get("order_latency_ms", 300)) / 1000.0)
                    self.risk = RiskManager(self.cfg, self.cfg["capital_usd"])
                else:
                    self.broker = LiveBroker(
                        self.cfg, self.tracker, _build_live_notifier(self.cfg))
                    await asyncio.to_thread(self.broker.refresh_state)
                    self._last_pos_refresh = time.time()
                    self.risk = RiskManager(self.cfg, self.broker.equity())
                    from .userfeed import UserFeed
                    self.userfeed = UserFeed(self.broker)
                    self.userfeed.start()
            else:
                self.broker.tracker = self.tracker
                if self.paper:
                    self.tracker.on_trade(self.broker._on_trade)
            self.broker.metrics = self.metrics
            self.tracker.on_trade(self._on_market_trade)
            await self.tracker.start()
        else:
            # Reuse the running tracker: incrementally resubscribe rather than
            # tearing it down. Surviving books (and their resting reward quotes)
            # keep ticking, only new tokens prime, and the existing trade
            # listeners persist — no cross-market feed gap on a single swap.
            await self.tracker.resubscribe(token_ids, carry=carry_books)
            self.broker.metrics = self.metrics

        self._last_scan = time.time()
        if not self.paper:
            await self._ensure_held_market_books()
        self._compute_size_factors()

    async def _refresh_reward_min_sizes(self) -> None:
        """轻量刷新已选市场的奖励最小份数，不改变市场选择或盘口订阅。"""
        markets = list(self.markets)
        if not markets:
            return
        refreshed = await asyncio.gather(*(
            asyncio.to_thread(gamma.fetch_market, market.condition_id)
            for market in markets
        ), return_exceptions=True)
        for market, latest in zip(markets, refreshed):
            if isinstance(latest, Exception):
                log.warning("奖励门槛查询失败，保留 %.0f 股：%s (%s)",
                            market.min_size, market.question[:45], latest)
                continue
            if latest is None or abs(latest.min_size - market.min_size) < 1e-9:
                continue
            old_min_size = market.min_size
            market.min_size = latest.min_size
            log.info("奖励最小份数更新：%s %.0f → %.0f 股",
                     market.question[:45], old_min_size, market.min_size)

    def _compute_size_factors(self) -> None:
        if not self.tracker:
            return
        self._size_factors = strategy.compute_size_factors(
            self.markets,
            self.tracker.books,
            self.broker.open_quotes,
            self.cfg,
            self.markouts,
        )

    async def _on_market_trade(self, token_id: str, price: float,
                               side: str, size: float) -> None:
        market = self._token_market.get(token_id)
        if market is not None:
            self.guards.record_trade(market, token_id, side, size, time.time())

    @staticmethod
    def _log_inventory_recovery_quote(
            market: gamma.Market, *, unpaired: float, quote: strategy.Quote,
            yes_book: Book, no_book: Book, pricing: dict[str, float | str],
            pair_cap: float | None = None, hard_cap: float | None = None,
            cost_basis: float | None = None,
            proposed_price: float | None = None) -> None:
        """Log the quote inputs needed to reconstruct a complement bid."""
        held = "YES" if unpaired > 0 else "NO"
        quote_token = "YES" if quote.token_id == market.yes_token else "NO"
        fee = market.fee_bps / 10_000.0 * (
            quote.price * (1.0 - quote.price)) ** market.fee_exponent
        expected = (1.0 - cost_basis - quote.price - fee
                    if cost_basis is not None else None)
        proposed_price = quote.price if proposed_price is None else proposed_price
        log.info(
            "INVENTORY_RECOVERY_QUOTE market='%s' held=%s %.0f "
            "quote=BUY %s %.0f @ %.3f path=%s proposed=%.3f cost_basis=%s "
            "effective_cap=%s hard_cap=%s fee_per_share=%.6f expected_pair_pnl=%s "
            "yes_book=%.3fx%.0f/%.3fx%.0f no_book=%.3fx%.0f/%.3fx%.0f "
            "microprice=%.3f flow=%.3f drift=%+.4f fair=%.3f "
            "base_offset=%.4f adaptive=%+.4f offset=%.4f skew=%+.4f "
            "fade_yes=%.4f fade_no=%.4f normal=yes@%.3f,no@%.3f "
            "说明=已检测裸仓；互补买单按当前策略价生成，预期配对盈亏仅作审计",
            market.question[:80], held, abs(unpaired), quote_token, quote.size,
            quote.price, pricing.get("recovery_path", "normal"), proposed_price,
            "unknown" if cost_basis is None else f"{cost_basis:.3f}",
            "unknown" if pair_cap is None else f"{pair_cap:.3f}",
            "unknown" if hard_cap is None else f"{hard_cap:.3f}", fee,
            "unknown" if expected is None else f"{expected:+.6f}",
            yes_book.best_bid, yes_book.bids.get(yes_book.best_bid, 0.0),
            yes_book.best_ask, yes_book.asks.get(yes_book.best_ask, 0.0),
            no_book.best_bid, no_book.bids.get(no_book.best_bid, 0.0),
            no_book.best_ask, no_book.asks.get(no_book.best_ask, 0.0),
            pricing.get("yes_microprice", 0.0), pricing.get("flow_imbalance", 0.0), pricing.get("flow_drift", 0.0),
            pricing.get("fair", 0.0), pricing.get("base_offset", 0.0), pricing.get("adaptive_offset", 0.0),
            pricing.get("offset", 0.0), pricing.get("skew", 0.0), pricing.get("fade_yes", 0.0), pricing.get("fade_no", 0.0),
            pricing.get("yes_bid_quote", 0.0), pricing.get("no_bid_quote", 0.0),
        )

    def _log_inventory_recovery_skip(
            self, market: gamma.Market, *, unpaired: float, reason: str,
            basis: float | None = None, yes_book: Book | None = None,
            no_book: Book | None = None) -> None:
        """Rate-limit evidence for a held inventory market that cannot quote."""
        now = time.time()
        if now - self._recovery_skip_logged_at.get(market.condition_id, 0.0) < 60.0:
            return
        self._recovery_skip_logged_at[market.condition_id] = now

        def top(book: Book | None) -> str:
            if book is None or book.best_bid is None or book.best_ask is None:
                return "empty"
            return f"{book.best_bid:.3f}/{book.best_ask:.3f}"

        basis_text = "unknown" if basis is None else f"{basis:.3f}"
        reason_cn = {
            "near_resolution": "临近结算",
            "stale_book": "订单簿过期",
            "unquotable_book": "订单簿不可报价",
            "theme_inventory_cap": "主题库存上限",
            "unknown_cost_basis": "成本基准未知",
            "no_complement_quote": "无互补报价",
            "below_current_clob_min_order_size": "低于当前最小挂单量",
            "strategy_no_quote": "策略无报价",
            "book_unavailable": "恢复定价缺少有效盘口",
            "invalid_strategy_price": "恢复策略价无效",
        }
        log.warning(
            "INVENTORY_RECOVERY_SKIPPED market='%s' reason=%s 原因=%s "
            "unpaired=%.0f cost_basis=%s yes_book=%s no_book=%s "
            "说明=检测到裸仓，但当前条件不允许生成安全互补买单",
            market.question[:80], reason, reason_cn.get(reason, reason), unpaired,
            basis_text, top(yes_book), top(no_book),
        )

    def _inventory_recovery_quotes(self, m: gamma.Market,
                                   desired: list[strategy.Quote],
                                   unpaired: float,
                                   now: float | None = None) -> list[strategy.Quote]:
        """第一阶段补单：只保留受配对成本上限约束的互补买单。"""
        if abs(unpaired) < MIN_TAKER_SHARES:
            return desired
        complement = m.no_token if unpaired > 0 else m.yes_token
        min_order_size = self._clob_min_order_size(complement)
        if min_order_size is not None and abs(unpaired) < min_order_size:
            return []
        basis_fn = getattr(self.broker, "unpaired_cost_basis", None)
        basis = basis_fn(m) if basis_fn else None
        if basis is None:
            return []
        max_price = self._forced_hedge_max_price(m, basis)
        return [strategy.Quote(q.token_id, min(q.price, max_price), abs(unpaired))
                for q in desired
                if q.token_id == complement]

    def _selected_market_recovery_quotes(self, m: gamma.Market,
                                         normal: list[strategy.Quote],
                                         unpaired: float) -> list[strategy.Quote]:
        """Quote only the equal-size complement while selected inventory is unpaired."""
        if abs(unpaired) < MIN_TAKER_SHARES:
            return normal
        recovery = self._inventory_recovery_quotes(m, normal, unpaired)
        if not recovery:
            return []
        return recovery

    def _can_retain_recovery_quote(self, m: gamma.Market,
                                   current: list[strategy.Quote],
                                   unpaired: float) -> bool:
        """Keep one safe, equal-size complement bid in its existing queue position."""
        if abs(unpaired) < MIN_TAKER_SHARES or len(current) != 1:
            return False
        quote = current[0]
        complement = m.no_token if unpaired > 0 else m.yes_token
        if quote.token_id != complement or abs(quote.size - abs(unpaired)) > 1e-6:
            return False
        basis_fn = getattr(self.broker, "unpaired_cost_basis", None)
        basis = basis_fn(m) if basis_fn else None
        if basis is None:
            return False
        return quote.price <= self._forced_hedge_max_price(m, basis) + 1e-9

    def _clob_min_order_size(self, token_id: str) -> float | None:
        """Current CLOB quantity floor, populated from `/book` snapshots."""
        if self.tracker is None:
            return None
        book = self.tracker.books.get(token_id)
        return book.min_order_size if book is not None else None

    def _quote_cfg_for_inventory_recovery(self, unpaired: float) -> dict:
        """Allow an existing position's complement up to its unpaired shares."""
        effective_cap = abs(unpaired)
        if effective_cap <= 0:
            return self.cfg
        scale = max(self._scale, 1e-9)
        configured_cap = float(self.cfg["quoting"]["max_capital_per_market"])
        recovery_cap = max(configured_cap, effective_cap / scale)
        if recovery_cap == configured_cap:
            return self.cfg
        return {**self.cfg, "quoting": {
            **self.cfg["quoting"], "max_capital_per_market": recovery_cap,
        }}

    def _filter_quotes_for_side_guard(self, desired: list[strategy.Quote], *,
                                      unpaired: float, now: float,
                                      recovery_token: str | None = None) -> list[strategy.Quote]:
        """Keep a pair-capped recovery bid even if its normal quote side is paused."""
        return [q for q in desired
                if q.token_id == recovery_token or self.guards.allow_side(q.token_id, now)]

    def _apply_quote_risk_decision(
        self,
        desired: list[strategy.Quote],
        risk_decision,
        market: gamma.Market,
    ) -> list[strategy.Quote]:
        """Apply a QuoteRiskDecision to desired quotes in active mode.

        - ``pull``: removes the quote from the dangerous side entirely.
        - ``widen``: widens the dangerous side's bid by ``yes_widen``/``no_widen``
          price units (lowers the bid price).
        - ``allow``: leaves the quote unchanged.
        """
        from .risk import QuoteRiskDecision
        rd: QuoteRiskDecision = risk_decision
        out: list[strategy.Quote] = []
        yes_seen, no_seen = False, False
        for q in desired:
            if q.token_id == market.yes_token:
                yes_seen = True
                if rd.yes_action == "pull":
                    log.warning(
                        "P0 \u9006\u5411\u9009\u62e9\u9632\u62a4\uff1a\u64a4\u4e0b\u201c%s\u201d\u7684 YES \u4fa7\u62a5\u4ef7 score=%.2f (%s)",
                        market.question[:45], rd.score, rd.reason)
                    continue
                if rd.yes_action == "widen":
                    new_price = strategy._round_tick(
                        q.price - max(rd.yes_widen, market.tick / 2), market.tick)
                    log.info(
                        "P0 \u9006\u5411\u9009\u62e9\u9632\u62a4\uff1a\u6269\u5927\u201c%s\u201d\u7684 YES \u4fa7\u62a5\u4ef7 %.2fc "
                        "%.4f\u2192%.4f", market.question[:40],
                        rd.yes_widen * 100, q.price, new_price)
                    out.append(strategy.Quote(q.token_id, new_price, q.size))
                    continue
            elif q.token_id == market.no_token:
                no_seen = True
                if rd.no_action == "pull":
                    log.warning(
                        "P0 \u9006\u5411\u9009\u62e9\u9632\u62a4\uff1a\u64a4\u4e0b\u201c%s\u201d\u7684 NO \u4fa7\u62a5\u4ef7 score=%.2f (%s)",
                        market.question[:45], rd.score, rd.reason)
                    continue
                if rd.no_action == "widen":
                    new_price = strategy._round_tick(
                        q.price - max(rd.no_widen, market.tick / 2), market.tick)
                    log.info(
                        "P0 \u9006\u5411\u9009\u62e9\u9632\u62a4\uff1a\u6269\u5927\u201c%s\u201d\u7684 NO \u4fa7\u62a5\u4ef7 %.2fc "
                        "%.4f\u2192%.4f", market.question[:40],
                        rd.no_widen * 100, q.price, new_price)
                    out.append(strategy.Quote(q.token_id, new_price, q.size))
                    continue
            out.append(q)
        # Log when P0 wanted to act but the quote was already absent from
        # desired (e.g. removed by the traditional guard or recovery logic
        # earlier in the loop), so we have an audit trail.
        if rd.yes_action in ("pull", "widen") and not yes_seen:
            log.info(
                "P0 \u9006\u5411\u9009\u62e9\u9632\u62a4\uff1aYES=%s \u51b3\u7b56\u5df2\u8bb0\u5f55\u4f46\u62a5\u4ef7\u4e0d\u5728 desired \u4e2d "
                "(\u53ef\u80fd\u5df2\u88ab\u4f20\u7edf guard \u6216\u8865\u5355\u903b\u8f91\u62a2\u5148\u79fb\u9664) score=%.2f (%s) "
                "market=\"%s\"",
                rd.yes_action, rd.score, rd.reason,
                market.question[:40])
        if rd.no_action in ("pull", "widen") and not no_seen:
            log.info(
                "P0 \u9006\u5411\u9009\u62e9\u9632\u62a4\uff1aNO=%s \u51b3\u7b56\u5df2\u8bb0\u5f55\u4f46\u62a5\u4ef7\u4e0d\u5728 desired \u4e2d "
                "(\u53ef\u80fd\u5df2\u88ab\u4f20\u7edf guard \u6216\u8865\u5355\u903b\u8f91\u62a2\u5148\u79fb\u9664) score=%.2f (%s) "
                "market=\"%s\"",
                rd.no_action, rd.score, rd.reason,
                market.question[:40])
        return out

    def _cooldown_recovery_quotes(self, m: gamma.Market,
                                   desired: list[strategy.Quote],
                                   unpaired: float,
                                   now: float | None = None) -> list[strategy.Quote]:
        """Keep only risk-reducing complementary bids during market cooldown.

        Cooldown markets also escalate after the window, same as held-only."""
        if abs(unpaired) < MIN_TAKER_SHARES:
            return desired  # 空仓无需 recovery，保留策略原报价
        risk_cfg = self.cfg.get("risk") or {}
        escalate_secs = float(risk_cfg.get("recovery_escalate_after_minutes", 0)) * 60.0
        if now is not None and escalate_secs > 0 and self.broker is not None:
            last_fill = self.broker.last_fill_ts(m.condition_id)
            if last_fill is not None and now - last_fill >= escalate_secs:
                return self._escalated_recovery_quotes(m, desired, unpaired)
        return self._inventory_recovery_quotes(m, desired, unpaired, now)

    def _held_market_recovery_quotes(self, m: gamma.Market, desired: list[strategy.Quote],
                                     unpaired: float,
                                     now: float | None = None) -> list[strategy.Quote]:
        """Held-only markets may only quote the inventory-reducing complement.

        After the escalate window has passed, promote to normal fair-price on
        the complement side only — accepting a small known loss to resolve the
        stale inventory rather than waiting indefinitely on a pair-cap quote
        that can never fill.
        """
        if abs(unpaired) < MIN_TAKER_SHARES:
            return desired  # 空仓无需 recovery，保留策略原报价
        risk_cfg = self.cfg.get("risk") or {}
        escalate_secs = float(risk_cfg.get("recovery_escalate_after_minutes", 0)) * 60.0
        if now is not None and escalate_secs > 0 and self.broker is not None:
            last_fill = self.broker.last_fill_ts(m.condition_id)
            if last_fill is not None and now - last_fill >= escalate_secs:
                return self._escalated_recovery_quotes(m, desired, unpaired)
        return self._inventory_recovery_quotes(m, desired, unpaired, now)

    def _escalated_recovery_quotes(self, m: gamma.Market,
                                    desired: list[strategy.Quote],
                                    unpaired: float, *,
                                    yes_book: Book | None = None,
                                    exposure_usd: float = 0.0,
                                    max_inventory_usd: float | None = None,
                                    fade_yes: float = 0.0,
                                    fade_no: float = 0.0,
                                    flow_imbalance: float = 0.0,
                                    markout_avg: float | None = None) -> list[strategy.Quote]:
        """Phase 2: one complement bid derived from the current book center.

        The recovery calculation intentionally bypasses the normal market
        selection range.  It never reuses ``desired``: a tail-priced held
        market has no normal quote to filter, but still needs a reducing bid.
        """
        if abs(unpaired) < MIN_TAKER_SHARES:
            return desired  # 空仓无需 recovery，保留策略原报价
        complement = m.no_token if unpaired > 0 else m.yes_token
        min_order_size = self._clob_min_order_size(complement)
        if min_order_size is not None and abs(unpaired) < min_order_size:
            self._recovery_pricing[m.condition_id] = {
                "recovery_path": "market_center_recovery",
                "reason": "below_current_clob_min_order_size",
            }
            return []
        if yes_book is None and self.tracker is not None:
            yes_book = self.tracker.books.get(m.yes_token)
        if yes_book is None:
            self._recovery_pricing[m.condition_id] = {
                "recovery_path": "market_center_recovery", "reason": "book_unavailable",
            }
            return []
        max_inventory_usd = (max_inventory_usd if max_inventory_usd is not None
                             else float(self.cfg["risk"]["max_inventory_usd_per_market"]) * self._scale)
        quote, pricing = strategy.compute_recovery_quote(
            m, yes_book, exposure_usd, self.cfg, max_inventory_usd,
            complement, abs(unpaired), fade_yes=fade_yes, fade_no=fade_no,
            flow_imbalance=flow_imbalance, markout_avg=markout_avg,
        )
        self._recovery_pricing[m.condition_id] = pricing
        return [quote] if quote is not None else []

    def _forced_hedge_allowed(self, market: gamma.Market, *, urgent: bool,
                              exposure_usd: float, threshold_usd: float,
                              risk_since: float, now: float, wait_secs: float,
                              basis: float | None, ask: float) -> bool:
        """Permit a taker hedge only after escalation and within pair-cost cap."""
        if basis is None:
            return False
        escalated = (urgent or abs(exposure_usd) >= threshold_usd
                     or now - risk_since >= wait_secs)
        return escalated and ask <= self._forced_hedge_max_price(market, basis) + 1e-9

    @staticmethod
    def _forced_hedge_max_price(market: gamma.Market, basis: float) -> float:
        """Highest tick price that keeps a paired share break-even after fee."""
        tick = market.tick
        price = min(1.0 - tick, math.floor((1.0 - basis) / tick + 1e-9) * tick)
        fee_rate = market.fee_bps / 10_000.0
        while price > 0:
            fee = fee_rate * (price * (1.0 - price)) ** market.fee_exponent
            if basis + price + fee <= 1.0 + 1e-9:
                return round(price, 6)
            price = round(price - tick, 6)
        return 0.0

    async def _run_reward_exit_batch_tick(self, now: float) -> None:
        """Coordinate all active reward exit batches for the current tick.

        Dispatches to:
        - _process_reward_fills(): detect new normal reward fills, create batches
        - _advance_reward_exit_batches(): take → seal → SELL per batch
        - Lock/unlock CIDs as needed
        """
        if self._reward_exit_mode == "off" or self.broker is None or self.tracker is None:
            return

        await self._retry_reward_exit_quote_cancels(now)

        # ── Step -1: one-time stale batch cleanup on first tick ──
        if not self._stale_batches_cleaned and self.metrics is not None:
            self._stale_batches_cleaned = True
            risk_cfg = self.cfg.get("risk") or {}
            stale_secs: int = int(risk_cfg.get(
                "reward_exit_stale_after_secs",
                risk_cfg.get("reward_exit_terminal_after_secs", 900),
            ))
            # Only an incomplete take can be stale here. SELL_PENDING is a
            # valid long-lived passive exit state and must keep its CID locked
            # across a restart so generic inventory recovery cannot take over.
            open_batches = self.metrics.get_open_reward_exit_batches()
            for b in open_batches:
                if b["status"] != "TAKE_PENDING":
                    continue
                age = now - float(b.get("created_ts") or 0)
                if age < stale_secs:
                    continue
                bid = b["batch_id"]
                log.warning(
                    "REWARD_EXIT_BATCH_STALE_CLEANUP batch_id=%s cid=%s "
                    "status=%s age=%.0fs "
                    "说明=启动时发现前一次会话残留批次，自动关闭并解锁市场",
                    bid, b["cid"], b["status"], age,
                )
                self.metrics.close_reward_exit_batch(
                    batch_id=bid,
                    status="CLOSED",
                    closed_ts=now,
                    manual_reason=f"stale_startup_cleanup_age_{age:.0f}s",
                )

        # ── Step 0: Detect and credit take fills ──
        # Must run before _process_reward_fills so we don't mistake take fills
        # for new origin fills.
        await self._credit_take_fills(now)

        # ── Step 1: detect new normal reward fills ──
        await self._process_reward_fills(now)

        # Shadow observes and computes only.  It must not lock a CID or alter
        # the existing quote/recovery path.
        if self._reward_exit_mode == "shadow":
            return

        # ── Step 2: advance existing batches ──
        await self._advance_reward_exit_batches(now)

        # ── Step 3: sync CIDs ──
        # Any CID with an open (non-CLOSED) batch is reward_exit_locked.
        if self.metrics:
            open_batches = self.metrics.get_open_reward_exit_batches()
            active_cids = {b["cid"] for b in open_batches}
            # Lock new CIDs
            for cid in active_cids - self._reward_exit_locked:
                self._reward_exit_locked.add(cid)
                m_name = next((m.question for m in self.markets if m.condition_id == cid), cid)
                log.warning("MARKET_REWARD_EXIT_LOCKED cid=%s market='%s' "
                            "说明=该市场有活跃奖励退出批次，停止普通报价和旧 recovery",
                            cid, str(m_name)[:50])
            # Unlock CIDs where all batches are CLOSED
            for cid in self._reward_exit_locked - active_cids:
                self._reward_exit_locked.discard(cid)
                self._reward_exit_cancel_retries.pop(cid, None)
                self._awaiting_top_n_rescan.add(cid)
                m_name = next((m.question for m in self.markets if m.condition_id == cid), cid)
                log.warning("MARKET_REWARD_EXIT_UNLOCKED cid=%s market='%s' "
                            "说明=所有批次已关闭，市场可在下次扫描后恢复报价",
                            cid, str(m_name)[:50])

    # ── Take fill credit: detect FAK fills and update batch ──

    async def _credit_take_fills(self, now: float) -> None:
        """Check fills_log for taker fills and credit them to TAKE_PENDING batches.

        For each active take batch, find fills on the complement token that are
        taker fills (path == 'reward_exit_take' or 'forced_hedge') and accumulate
        take_filled_size, take_notional_usd, take_fee_usd.  When filled >= 2q,
        transition to SELL_PENDING via compute_sell_target.

        NOTE: batch_take fills are credited directly by _advance_take_pending()
        immediately after FAK execution.  This method reads the already-persisted
        fill facts (list_reward_exit_fills) to update batch progress and
        transition to SELL_PENDING — it does NOT write duplicate fills.
        """
        if self.broker is None or self.metrics is None or self.tracker is None:
            return

        open_batches = self.metrics.get_open_reward_exit_batches()
        take_batches = [
            b for b in open_batches if b["status"] in ("TAKE_PENDING", "TAKE_BLOCKED")
        ]
        if not take_batches:
            return

        for b in take_batches:
            bid = b["batch_id"]
            # Direct credit in _advance_take_pending() is the authority for
            # batch_take fills — do NOT scan fills_log and write duplicates.
            # Just read what was already persisted.

            persisted = self.metrics.list_reward_exit_fills(bid, intent="batch_take")
            total_filled = sum(float(f["size"]) for f in persisted)
            total_notional = sum(float(f["price"]) * float(f["size"])
                                 for f in persisted)
            total_fee = sum(float(f.get("fee_usd") or 0) for f in persisted)
            prev_filled = float(b.get("take_filled_size") or 0)
            if abs(total_filled - prev_filled) < 1e-9 and \
                    abs(total_notional - float(b.get("take_notional_usd") or 0)) < 1e-9:
                continue

            # Update persisted batch with accumulated totals
            self.metrics.update_reward_exit_batch(
                batch_id=bid,
                take_filled_size=total_filled,
                take_notional_usd=total_notional,
                take_fee_usd=total_fee,
            )
            pending_take = self.metrics.get_pending_batch_take(bid)
            clear_pending = getattr(self.broker, "clear_pending_batch_take", None)
            reported = float(pending_take["expiration"]) if pending_take is not None else -1.0
            complete_submission = (pending_take is not None and reported >= 0
                                   and total_filled >= float(pending_take["size"]) + reported - 1e-9)
            if complete_submission:
                self.metrics.update_reward_exit_order(
                    pending_take["order_id"], status="CLOSED")
            # A restart can leave the batch aggregate already updated while the
            # durable submission is still PENDING.  Release the in-memory
            # attribution context whenever the durable record is resolved;
            # requiring a *new* fill here would otherwise permanently block it.
            if callable(clear_pending) and (complete_submission or pending_take is None):
                clear_pending(b["cid"], bid)
                self._batch_pending_take_until.pop(bid, None)

            log.warning(
                "BATCH_TAKE_CREDITED batch_id=%s cid=%s "
                "prev_filled=%.0f new=%.0f total=%.0f target=%.0f "
                "说明=take成交已计入批次",
                bid, b["cid"], prev_filled, total_filled - prev_filled,
                total_filled, float(b["take_target_size"]),
            )

            # If take is fully filled, seal and compute SELL target
            target = float(b["take_target_size"])
            if total_filled >= target - 1e-9:
                await self._seal_and_set_sell_target(b, persisted, now)

    async def _seal_and_set_sell_target(
        self, batch: dict, take_fill_dicts: list[dict], now: float,
    ) -> None:
        """Seal a fully-filled take batch: compute paired loss, set SELL target."""
        if self.metrics is None or self.tracker is None or self.broker is None:
            return

        cid = batch["cid"]
        batch_id = batch["batch_id"]

        # Build TakeFill objects from the complete persisted batch history.
        take_fills = sorted(
            [reward_exit.TakeFill(
                fill_id=f["fill_id"],
                ts=f["ts"],
                price=f["price"],
                size=f["size"],
                notional=float(f["price"]) * float(f["size"]),
                fee_usd=float(f.get("fee_usd") or 0),
            ) for f in take_fill_dicts],
            key=lambda x: (x.ts, x.fill_id),
        )

        orig_size = float(batch["origin_size"])
        orig_notional = float(batch["origin_notional_usd"])
        orig_fee = float(batch["origin_fee_usd"])

        # FIFO split + paired loss
        split = reward_exit.split_take_fills_fifo(take_fills, orig_size)
        paired_loss = reward_exit.compute_paired_loss(
            origin_notional_usd=orig_notional,
            origin_fee_usd=orig_fee,
            paired_complement_notional=split.paired_notional,
            paired_complement_fee=split.paired_fee,
            origin_size=orig_size,
        )

        # Determine the market for fee computation
        m = next((mm for mm in self.markets if mm.condition_id == cid), None)
        if m is None:
            m = self._token_market.get(batch["complement_token_id"])
        if m is None:
            log.error(
                "BATCH_SEAL_FAILED batch_id=%s cid=%s 说明=无法定位市场",
                batch_id, cid,
            )
            return

        complement_token = batch["complement_token_id"]
        book = self.tracker.books.get(complement_token)
        best_bid = book.best_bid if book else None

        # Compute SELL target price
        target_result = reward_exit.compute_sell_target(
            market=m,
            exit_size=orig_size,
            exit_cost_notional=split.exit_notional,
            exit_cost_fee=split.exit_fee,
            paired_loss_usd=paired_loss,
            best_bid=best_bid,
        )

        if target_result.price is None:
            # Cannot compute a valid target — move to MANUAL_HOLD
            self.metrics.update_reward_exit_batch(
                batch_id=batch_id,
                paired_size=orig_size,
                paired_loss_usd=paired_loss,
                exit_initial_size=orig_size,
                status="MANUAL_HOLD",
                manual_reason=target_result.reason,
                updated_ts=now,
            )
            log.warning(
                "BATCH_MANUAL_HOLD batch_id=%s cid=%s reason=%s "
                "origin_notional=%.4f paired_notional=%.4f exit_notional=%.4f "
                "paired_loss=%.4f 说明=无法计算有效卖出目标价",
                batch_id, cid, target_result.reason,
                orig_notional, split.paired_notional, split.exit_notional,
                paired_loss,
            )
            return

        # Seal → SELL_PENDING
        self.metrics.update_reward_exit_batch(
            batch_id=batch_id,
            paired_size=orig_size,
            paired_loss_usd=paired_loss,
            exit_initial_size=orig_size,
            exit_target_price=target_result.price,
            status="SELL_PENDING",
            updated_ts=now,
        )

        log.warning(
            "BATCH_SEALED batch_id=%s cid=%s paired_loss=%.4f "
            "exit_cost_notional=%.4f exit_cost_fee=%.4f "
            "exit_target_price=%.4f exit_size=%.0f "
            "required_net=%.4f 说明=TAKE完成，批次已密封，进入SELL阶段",
            batch_id, cid, paired_loss,
            split.exit_notional, split.exit_fee,
            target_result.price, orig_size,
            target_result.required_net_usd,
        )

        # ── Timing stats: compute end-to-end take latency ──
        if self.metrics is not None:
            fresh = self.metrics.get_reward_exit_batch(batch_id)
            if fresh:
                ots = float(fresh.get("origin_fill_ts") or 0)
                bts = float(fresh.get("created_ts") or 0)
                sts = float(fresh.get("first_take_submit_ts") or 0)
                ets = float(fresh.get("first_take_executed_ts") or 0)
                log.warning(
                    "BATCH_TIMING batch_id=%s cid=%s "
                    "fill_to_batch=%.3fs batch_to_submit=%.3fs "
                    "submit_to_exec=%.3fs exec_to_seal=%.3fs "
                    "total_fill_to_seal=%.3fs "
                    "说明=take阶段端到端耗时统计",
                    batch_id, cid,
                    bts - ots if ots > 0 and bts > 0 else -1.0,
                    sts - bts if sts > 0 and bts > 0 else -1.0,
                    ets - sts if ets > 0 and sts > 0 else -1.0,
                    now - ets if ets > 0 else -1.0,
                    now - ots if ots > 0 else -1.0,
                )

        self._batch_last_take_attempt.pop(batch_id, None)

    # ── Fill detection and batch creation ──

    async def _process_reward_fills(self, now: float) -> None:
        """Identify new normal reward BUY fills and create RewardExitBatches.

        A fill qualifies when:
        - It is a BUY (not SELL)
        - It is not a taker fill (maker only)
        - It is not an exit fill
        - Its fill_id has not already been recorded as an origin_fill_id
        - Its fill was placed as a normal reward quote (intent == 'normal_reward';
          legacy paper entries may use path == 'normal')
        """
        if self.broker is None or self.metrics is None or self.tracker is None:
            return

        fills = list(self.broker.fills_log)
        # A process restart or a user-feed outage can leave the in-memory log
        # empty even though LiveBroker already persisted the normal maker fill.
        # Replay those durable origin facts before deciding that no batch exists.
        for persisted in self.metrics.list_unbatched_normal_reward_fills():
            fills.append({
                "fill_id": persisted["fill_id"],
                "order_id": persisted["order_id"],
                "intent": persisted["intent"],
                "cid": persisted["cid"],
                "token": persisted["token_id"],
                "side": persisted["side"],
                "price": persisted["price"],
                "size": persisted["size"],
                "fee_usd": persisted["fee_usd"],
                "ts": persisted["ts"],
            })
        known_fill_ids = self.metrics.reward_exit_batch_origin_fill_ids()

        for entry in fills:
            fill_id = str(entry.get("id") or entry.get("fill_id") or "")
            if not fill_id or fill_id in known_fill_ids:
                continue
            side = str(entry.get("side") or "").upper()
            if side != "YES" and side != "NO":
                continue
            if entry.get("taker") or entry.get("exit"):
                continue
            # Must be a normal reward quote fill.  The explicit intent is the
            # durable discriminator; retain the path check for legacy paper
            # fills created before intent was added.
            path = str(entry.get("path") or "")
            intent = str(entry.get("intent") or "")
            if intent and intent != "normal_reward":
                continue
            if not intent and path != "normal":
                continue

            token = str(entry.get("token") or "")
            cid = str(entry.get("cid") or "")
            size = float(entry.get("size") or 0)
            price = float(entry.get("price") or 0)
            order_id = str(entry.get("order_id") or "")
            origin_fill_ts = float(entry.get("ts") or now)

            if not token or not cid or size <= 0:
                continue

            # Determine complement token
            m = next((mm for mm in self.markets if mm.condition_id == cid), None)
            if m is None:
                m = self._token_market.get(token)
            if m is None:
                continue
            complement = m.no_token if token == m.yes_token else m.yes_token

            batch_id = f"reward-exit-{fill_id}"

            # ── Cancel all normal quotes on this market FIRST ──
            # A fill on one side means we now hold inventory on that token.
            # The other side's quote must be cancelled immediately to avoid
            # double-selling into the same market during the exit.
            # We cancel before the DB write so a crash during write won't
            # leave stale quotes on Polymarket.
            rotate_market = cid not in self._reward_exit_locked
            if rotate_market:
                self._reward_exit_locked.add(cid)
                log.warning(
                    "MARKET_REWARD_EXIT_LOCKED cid=%s market='%s' "
                    "说明=检测到首笔普通奖励成交，立即锁定市场并撤销普通报价",
                    cid, str(m.question)[:50],
                )
                try:
                    cancelled = await self._broker_call(
                        self.broker.cancel_quotes_for_market, m)
                    if cancelled is False:
                        self._schedule_reward_exit_cancel_retry(cid, m, now, 1)
                except Exception as exc:  # noqa: BLE001 - confirmed fills must still take
                    log.warning(
                        "REWARD_EXIT_CANCEL_QUOTES_FAILED cid=%s market='%s': %s; "
                        "仍为已确认奖励成交创建 double-take 批次",
                        cid, str(m.question)[:50], exc,
                    )
                    self._schedule_reward_exit_cancel_retry(cid, m, now, 1)

            # Persist the batch
            self.metrics.open_reward_exit_batch(
                batch_id=batch_id, cid=cid,
                market_name=m.question,
                origin_order_id=order_id, origin_fill_id=fill_id,
                origin_token_id=token, complement_token_id=complement,
                origin_size=size,
                origin_notional_usd=price * size,
                origin_fee_usd=float(entry.get("fee") or entry.get("fee_usd") or 0.0),
                take_target_size=2.0 * size,
                created_ts=now,
                origin_fill_ts=origin_fill_ts,
            )

            log.warning(
                "REWARD_EXIT_BATCH_OPENED batch_id=%s cid=%s market='%s' "
                "token=%s size=%.0f price=%.4f complement=%s "
                "说明=普通奖励成交已创建退出批次，准备 take %.0f 股互补 token",
                batch_id, cid, m.question[:50],
                "YES" if token == m.yes_token else "NO",
                size, price,
                "YES" if complement == m.yes_token else "NO",
                2.0 * size,
            )
            if rotate_market:
                await self._rescan(rotate=True, exclude_cids={cid})
            known_fill_ids.add(fill_id)

    def _schedule_reward_exit_cancel_retry(
            self, cid: str, market: gamma.Market, now: float, attempts: int,
    ) -> None:
        delay = min(2 ** (attempts - 1), 30.0)
        self._reward_exit_cancel_retries[cid] = (market, now + delay, attempts)
        log.warning(
            "REWARD_EXIT_CANCEL_QUOTES_RETRY_SCHEDULED cid=%s attempt=%d delay=%.0fs",
            cid, attempts, delay,
        )

    async def _retry_reward_exit_quote_cancels(self, now: float) -> None:
        """Retry failed normal-quote cancellations without delaying confirmed takes."""
        if self.broker is None:
            return
        for cid, (market, due_ts, attempts) in list(self._reward_exit_cancel_retries.items()):
            if now < due_ts:
                continue
            try:
                cancelled = await self._broker_call(self.broker.cancel_quotes_for_market, market)
            except Exception as exc:  # noqa: BLE001 - retry on the next backoff slot
                log.warning("REWARD_EXIT_CANCEL_QUOTES_RETRY_FAILED cid=%s: %s", cid, exc)
                cancelled = False
            if cancelled is False:
                self._schedule_reward_exit_cancel_retry(cid, market, now, attempts + 1)
            else:
                self._reward_exit_cancel_retries.pop(cid, None)
                log.info("REWARD_EXIT_CANCEL_QUOTES_RETRY_SUCCEEDED cid=%s attempts=%d",
                         cid, attempts)

    # ── Batch advancement ──

    async def _advance_reward_exit_batches(self, now: float) -> None:
        """Advance each open batch: take → seal → SELL.

        Only the first (oldest) TAKE_PENDING batch for each CID gets
        take orders submitted — market lock serialization.
        """
        if self.broker is None or self.metrics is None or self.tracker is None:
            return

        open_batches = self.metrics.get_open_reward_exit_batches()
        # Group by CID, process oldest TAKE_PENDING first
        by_cid: dict[str, list[dict]] = {}
        for b in open_batches:
            by_cid.setdefault(b["cid"], []).append(b)

        manual_hold = self._manual_hold_cids()
        risk_cfg = self.cfg.get("risk") or {}
        terminal_secs: int = int(risk_cfg.get("reward_exit_terminal_after_secs", 900))

        # ── Per-tick committed balance: prevent multiple batches in the
        #     same tick from collectively overspending.  We snapshot the
        #     live broker balance once, then deduct every actual FAK spend
        #     (safe_price × filled_now) so batch 2 sees a realistic
        #     post-batch-1 balance.  Do NOT re-read _collateral — the CLOB
        #     balance isn't settled instantly and would show stale data.
        committed_balance: float | None = None
        # Lazy-init on first use so PaperBroker (no _collateral) isn't
        # silently broken.
        if hasattr(self.broker, '_collateral'):
            try:
                bal = getattr(self.broker, '_collateral', None)
                if bal is not None and isinstance(bal, (int, float)) and bal == bal:
                    committed_balance = bal
            except Exception:
                pass

        # ── Terminal timeout: close stale TAKE_PENDING batches that have
        #     been stuck forever (e.g. balance insufficient, book missing,
        #     market delisted).  Without this the CID is permanently locked
        #     and will never quote again.
        if self.metrics:
            for b in open_batches:
                if b["status"] not in ("TAKE_PENDING",):
                    continue
                age = now - float(b["created_ts"])
                if age < terminal_secs:
                    continue
                bid = b["batch_id"]
                cid = b["cid"]
                log.warning(
                    "REWARD_EXIT_BATCH_TERMINAL_TIMEOUT batch_id=%s cid=%s "
                    "age=%.0fs status=%s take_filled=%.0f take_target=%.0f "
                    "说明=批次在TAKE_PENDING状态超时，自动关闭并解锁市场",
                    bid, cid, age, b["status"],
                    float(b.get("take_filled_size") or 0),
                    float(b.get("take_target_size") or 0),
                )
                self.metrics.close_reward_exit_batch(
                    batch_id=bid,
                    status="CLOSED",
                    closed_ts=now,
                    manual_reason=f"terminal_timeout_age_{age:.0f}s",
                )

        for cid, batches in by_cid.items():
            # Skip CIDs in manual hold — this covers both config-driven holds
            # and permanently banned CIDs (markout/recovery-loss ban).
            if cid in manual_hold:
                continue
            # Find the oldest pending or cost-blocked take — only one take at a time.
            take_batches = [
                b for b in batches if b["status"] in ("TAKE_PENDING", "TAKE_BLOCKED")
            ]
            if take_batches:
                take_batches.sort(key=lambda b: b["created_ts"])
                spent = await self._advance_take_pending(
                    cid, take_batches[0], now,
                    committed_balance=committed_balance,
                )
                # Deduct the actual FAK spend (not the budget) so the next
                # batch's check is accurate.  The live _collateral won't
                # reflect this spend for several seconds.
                if spent > 0 and committed_balance is not None:
                    committed_balance = max(0.0, committed_balance - spent)

            # SELL_PENDING batches
            sell_batches = [b for b in batches if b["status"] == "SELL_PENDING"]
            for b in sell_batches:
                await self._advance_sell_pending(cid, b, now)

    # ── Take stage: submit FAK BUY ──

    async def _advance_take_pending(self, cid: str, batch: dict, now: float,
                                     committed_balance: float | None = None) -> float:
        """Submit FAK BUY for remaining complement shares of a TAKE_PENDING batch.

        committed_balance, when passed, is the broker balance *after* earlier
        batches in this tick have already spent.  It overrides the live
        _collateral read for the budget check so multiple batches within one
        tick don't collectively overspend.

        Returns the actual USD spent (safe_price × filled_now) so the caller
        can deduct it from committed_balance for subsequent batches.
        """
        if self.broker is None or self.tracker is None:
            return 0.0

        complement_token = batch["complement_token_id"]
        orig_size = float(batch["origin_size"])
        target = 2.0 * orig_size
        filled = float(batch["take_filled_size"])
        remaining = reward_exit.remaining_take(target, filled)

        if remaining <= 0:
            return 0.0

        # Check if complement book is available
        book = self.tracker.books.get(complement_token)
        if book is None:
            return 0.0

        best_ask = book.best_ask
        if best_ask is None:
            return 0.0

        batch_id = batch["batch_id"]
        has_durable_pending = getattr(self.metrics, "get_pending_batch_take", None)
        pending_take = has_durable_pending(batch_id) if callable(has_durable_pending) else None
        if pending_take:
            reconcile_take = getattr(self.broker, "reconcile_pending_batch_take", None)
            if callable(reconcile_take):
                reconcile_take(pending_take)
            return 0.0
        has_pending = getattr(self.broker, "has_pending_batch_take", None)
        if callable(has_pending) and has_pending(cid, batch_id):
            return 0.0
        if now < self._batch_pending_take_until.get(batch_id, 0.0):
            return 0.0

        # Determine market before computing the cost cap.
        m = next((mm for mm in self.markets if mm.condition_id == cid), None)
        if m is None:
            m = self._token_market.get(complement_token)
        if m is None:
            return 0.0

        risk_cfg = self.cfg.get("risk") or {}
        max_pair_loss_cents = float(risk_cfg.get("reward_exit_max_pair_loss_cents", 8.0))
        origin_notional = float(batch.get("origin_notional_usd") or 0.0)
        origin_fee = float(batch.get("origin_fee_usd") or 0.0)
        origin_price = (origin_notional + origin_fee) / orig_size if orig_size > 0 else 0.0
        max_buy_price = reward_exit.max_take_price_for_pair(
            origin_price, m, max_pair_loss_cents)
        if best_ask > max_buy_price + 1e-9:
            if self.metrics is not None:
                self.metrics.update_reward_exit_batch(
                    batch_id=batch_id,
                    status="TAKE_BLOCKED",
                    manual_reason="take_cost_guard",
                    updated_ts=now,
                )
            log.warning(
                "BATCH_TAKE_BLOCKED batch_id=%s cid=%s origin_price=%.4f "
                "best_ask=%.4f max_ask=%.4f max_pair_loss_cents=%.2f "
                "说明=互补买入将超过每对成本上限，保持批次锁定等待盘口恢复",
                batch_id, cid, origin_price, best_ask, max_buy_price,
                max_pair_loss_cents,
            )
            return 0.0

        if batch.get("status") == "TAKE_BLOCKED" and self.metrics is not None:
            self.metrics.update_reward_exit_batch(
                batch_id=batch_id,
                status="TAKE_PENDING",
                manual_reason="",
                updated_ts=now,
            )

        if self._reward_exit_mode == "shadow":
            log.warning(
                "BATCH_TAKE_SUBMITTED batch_id=%s cid=%s complement=%s "
                "size=%.0f remaining=%.0f ask=%.4f mode=shadow "
                "说明=shadow模式，不实际提交take订单",
                batch_id, cid, complement_token[:12], target, remaining, best_ask,
            )
            return 0.0

        # ── Exponential backoff for FAK retries ──
        # First attempt after batch creation: no delay (attempts=0 → 0s).
        # After 1st failure: 1s, 2nd: 2s, 3rd: 4s, then clamp at 5s.
        # This puts fast pressure on fresh inventory while avoiding
        # wasteful spam when the book is persistently empty.
        last = self._batch_last_take_attempt.get(batch_id, 0.0)
        if last > 0:
            # How many previous failures for this batch?
            attempts: int = 0
            err_info = self._batch_take_errors.get(batch_id)
            if err_info:
                attempts = err_info[1]
            # Exponential: 1s, 2s, 4s capped at 5s
            delay: float = min(1.0 * (2 ** (attempts - 1)), 5.0) if attempts > 0 else 1.0
            if now - last < delay:
                return 0.0
        self._batch_last_take_attempt[batch_id] = now

        # Determine tick for max legal price
        tick = getattr(m, 'tick', 0.01) if m else 0.01
        max_buy_price = min(max_buy_price, 1.0 - tick)

        log.warning(
            "BATCH_TAKE_SUBMITTED batch_id=%s cid=%s complement=%s "
            "size=%.0f remaining=%.0f price=%.4f mode=active "
            "说明=提交FAK BUY互补token",
            batch_id, cid, complement_token[:12], target, remaining, max_buy_price,
        )

        # ── Record first take submit timestamp for timing stats ──
        first_submit = batch.get("first_take_submit_ts")
        if first_submit is None and self.metrics is not None:
            self.metrics.update_reward_exit_batch(
                batch_id=batch_id, first_take_submit_ts=now)

        # ── Balance check: skip if remaining * best_ask > available ──
        # Compute amount tightly to avoid FAK overfill.  The CLOB FAK
        # fills at the *maker* ask prices (which can be lower than
        # max_buy_price), so amount = max_buy_price × remaining can
        # produce up to (max_buy_price / best_ask) × more shares than
        # requested.  Use best_ask for the spend budget instead.
        safe_price = best_ask if best_ask and best_ask <= max_buy_price else max_buy_price
        budget_needed = round(safe_price * remaining, 2)
        balance_ok = True
        # ── Per-tick committed balance: use the value passed down from
        #     _advance_reward_exit_batches when available (already reflects
        #     prior batches' FAK fills in this tick).  Otherwise fall back
        #     to the live broker collateral.
        bal: float | None = committed_balance
        if bal is None and hasattr(self.broker, '_collateral'):
            try:
                bal = getattr(self.broker, '_collateral', None)
            except Exception:
                pass
        if bal is not None and isinstance(bal, (int, float)) and bal == bal:
            if bal < budget_needed * 0.99:
                balance_ok = False
                log.warning(
                    "BATCH_TAKE_BALANCE_INSUFFICIENT batch_id=%s cid=%s "
                    "needed=%.2f balance=%.2f "
                    "说明=余额不足，跳过本次take（等待入金或市场变化）",
                    batch_id, cid, budget_needed, bal,
                )

        # Use broker.taker_buy() for true FAK execution
        filled_now = 0.0
        if balance_ok and hasattr(self.broker, 'taker_buy') and m is not None:
            async with self._market_lock(cid):
                filled_now = await self._broker_call(
                    self.broker.taker_buy,
                    m, complement_token, remaining, max_buy_price,
                    {"path": "reward_exit_take", "intent": "batch_take",
                     "batch_id": batch_id, "best_ask": best_ask},
                )

        if filled_now > 0 and self.metrics is not None:
            # The order response confirms quantity but not the exchange fill
            # price or fee.  Wait for the durable user-feed fact before
            # advancing accounting or sealing the batch.
            self._batch_pending_take_until[batch_id] = now + 30.0
            log.warning(
                "BATCH_TAKE_AWAITING_FILL batch_id=%s cid=%s reported_size=%.2f "
                "说明=FAK回执仅确认数量，等待成交流写入真实价格和手续费",
                batch_id, cid, filled_now,
            )
        else:
            # ── FAK returned 0: classify the error for adaptive backoff ──
            # Permanent errors (balance, invalid args) get longer cooldown
            # after repeated failures to avoid wasted API calls.
            err_key = "unknown"
            if not balance_ok:
                err_key = "balance_insufficient"
            elif book is None or best_ask is None:
                err_key = "no_book"
            elif m is None:
                err_key = "no_market"
            prev = self._batch_take_errors.get(batch_id)
            if prev and prev[0] == err_key:
                new_count = prev[1] + 1
            else:
                new_count = 1
            self._batch_take_errors[batch_id] = (err_key, new_count)
            if new_count >= 3 and new_count % 10 == 0:
                log.warning(
                    "BATCH_TAKE_STUCK batch_id=%s cid=%s error=%s count=%d "
                    "说明=take持续失败，已重试%d次，原因=%s",
                    batch_id, cid, err_key, new_count, new_count, err_key,
                )

        # Return actual spend for per-tick committed balance tracking
        return safe_price * filled_now

    # ── SELL stage: per-batch GTD maker SELL ──

    # Track per-batch exit orders: batch_id → RestingOrder
    _batch_exit_orders: dict[str, object] = {}
    # Guard: close stale batches from prior sessions exactly once per run
    _stale_batches_cleaned: bool = False

    async def _advance_sell_pending(self, cid: str, batch: dict, now: float) -> None:
        """Manage maker SELL for a SELL_PENDING batch.

        Uses per-batch exit order tracking (_batch_exit_orders) to avoid the
        singleton _exit_orders[cid] collision when multiple batches share a CID.
        """
        if self.broker is None or self.tracker is None:
            return

        batch_id = batch["batch_id"]
        complement_token = batch["complement_token_id"]
        target_price = float(batch["exit_target_price"])
        exit_size = float(batch["exit_initial_size"])
        filled = float(batch.get("exit_filled_size") or 0.0)

        # Rebuild exit progress from the durable fill facts.  Scanning the
        # broker log and adding to the batch on every tick double-counts the
        # same fill after a restart.
        persisted_fills: list[dict] = []
        if self.metrics is not None:
            for entry in getattr(self.broker, "fills_log", []):
                if (str(entry.get("batch_id") or "") == batch_id
                        and str(entry.get("intent") or "") == "batch_exit"):
                    fill_id = str(entry.get("fill_id") or entry.get("id") or "")
                    if not fill_id:
                        continue
                    self.metrics.record_reward_exit_fill(
                        fill_id=fill_id, batch_id=batch_id,
                        order_id=str(entry.get("order_id") or ""),
                        intent="batch_exit", cid=cid,
                        token_id=str(entry.get("token") or complement_token),
                        side=str(entry.get("side") or "SELL"),
                        price=float(entry.get("price") or 0),
                        size=float(entry.get("size") or 0),
                        fee_usd=float(entry.get("fee") or entry.get("fee_usd") or 0),
                        ts=float(entry.get("ts") or now),
                    )
            persisted_fills = self.metrics.list_reward_exit_fills(
                batch_id, intent="batch_exit")
            filled = sum(float(f.get("size") or 0) for f in persisted_fills)
            notional = sum(float(f.get("price") or 0) * float(f.get("size") or 0)
                           for f in persisted_fills)
            fees = sum(float(f.get("fee_usd") or 0) for f in persisted_fills)
            if (abs(filled - float(batch.get("exit_filled_size") or 0)) > 1e-9
                    or abs(notional - float(batch.get("exit_notional_usd") or 0)) > 1e-9):
                self.metrics.update_reward_exit_batch(
                    batch_id=batch_id, exit_filled_size=filled,
                    exit_notional_usd=notional, exit_fee_usd=fees,
                )
        rem = max(0.0, exit_size - filled)

        if rem <= 0:
            # All exit shares sold — close batch
            if self.metrics:
                self.metrics.close_reward_exit_batch(batch_id=batch_id, closed_ts=now)
                # ── Timing: full lifecycle fill → close ──
                fresh = self.metrics.get_reward_exit_batch(batch_id)
                if fresh:
                    ots = float(fresh.get("origin_fill_ts") or 0)
                    ets = float(fresh.get("first_take_executed_ts") or 0)
                    cts = float(fresh.get("closed_ts") or 0)
                    log.warning(
                        "BATCH_TIMING batch_id=%s cid=%s "
                        "total_fill_to_close=%.3fs seal_to_close=%.3fs "
                        "stage=COMPLETE 说明=退出全生命周期耗时统计",
                        batch_id, cid,
                        cts - ots if ots > 0 else -1.0,
                        cts - ets if ets > 0 else -1.0,
                    )
            log.warning(
                "REWARD_EXIT_BATCH_CLOSED batch_id=%s cid=%s "
                "exit_filled=%.0f exit_size=%.0f 说明=所有退出份额已售出",
                batch_id, cid, filled, exit_size,
            )
            self._batch_last_take_attempt.pop(batch_id, None)
            self._batch_exit_orders.pop(batch_id, None)
            self._batch_exit_cancel_pending.pop(batch_id, None)
            self._batch_sell_failures.pop(batch_id, None)
            return

        if target_price <= 0:
            # Target price not yet computed
            return

        # Determine market — fall back to token_market if not in self.markets
        m = next((mm for mm in self.markets if mm.condition_id == cid), None)
        if m is None:
            m = self._token_market.get(complement_token)
        if m is None:
            return

        book = self.tracker.books.get(complement_token)
        if book is None:
            return

        if persisted_fills:
            latest = persisted_fills[-1]
            log.info(
                "BATCH_SELL_PROGRESS batch_id=%s cid=%s latest_fill=%.0f@%.4f "
                "total_filled=%.0f remaining=%.0f",
                batch_id, cid, float(latest.get("size") or 0),
                float(latest.get("price") or 0), filled, rem,
            )
        if rem <= 0:
            if self.metrics:
                self.metrics.close_reward_exit_batch(batch_id=batch_id, closed_ts=now)
            log.warning(
                "REWARD_EXIT_BATCH_CLOSED batch_id=%s cid=%s "
                "exit_filled=%.0f exit_size=%.0f 说明=所有退出份额已售出",
                batch_id, cid, filled, exit_size,
            )
            self._batch_last_take_attempt.pop(batch_id, None)
            self._batch_exit_orders.pop(batch_id, None)
            self._batch_exit_cancel_pending.pop(batch_id, None)
            self._batch_sell_failures.pop(batch_id, None)
            return

        best_bid = book.best_bid
        tick = getattr(m, 'tick', 0.01)

        # Apply bid floor: the actual SELL price must be ≥ best_bid + tick
        if best_bid is not None:
            floor = min(1.0 - tick, best_bid + tick)
            if target_price < floor - 1e-9:
                log.warning(
                    "BATCH_SELL_PRICING batch_id=%s cid=%s "
                    "target=%.4f best_bid=%.4f floor=%.4f "
                    "说明=目标价低于best_bid+tick，使用floor价",
                    batch_id, cid, target_price, best_bid, floor,
                )
                target_price = floor

        # Rehydrate order identity from the broker/exchange view before
        # creating anything.  A restart must not duplicate an existing GTD.
        pending_cancel_id = self._batch_exit_cancel_pending.get(batch_id)
        if pending_cancel_id:
            if not hasattr(self.broker, "reconcile_orders"):
                return
            reconciled = await self._broker_call(self.broker.reconcile_orders)
            if not reconciled:
                log.warning(
                    "BATCH_SELL_CANCEL_PENDING batch_id=%s order_id=%s "
                    "说明=旧卖单撤销后对账失败，暂不重挂",
                    batch_id, pending_cancel_id,
                )
                return
            active_ids = {
                ro.order_id
                for orders in getattr(self.broker, "_open_orders", {}).values()
                for ro in orders
            }
            active_ids.update(
                ro.order_id for ro in getattr(self.broker, "_exit_orders", {}).values())
            active_ids.update(
                ro.order_id
                for ro in getattr(self.broker, "_reward_exit_orders", {}).values())
            if pending_cancel_id in active_ids:
                log.warning(
                    "BATCH_SELL_CANCEL_PENDING batch_id=%s order_id=%s "
                    "说明=旧卖单仍在交易所活动订单中，暂不重挂",
                    batch_id, pending_cancel_id,
                )
                return
            self._batch_exit_cancel_pending.pop(batch_id, None)
            if self.metrics:
                self.metrics.update_reward_exit_order(
                    pending_cancel_id, status="CANCELLED")

        cur = self._batch_exit_orders.get(batch_id)
        if cur is None:
            cur = getattr(self.broker, "_reward_exit_orders", {}).get(batch_id)
        if cur is None and self.metrics is not None:
            persisted_order = self.metrics.get_reward_exit_order(batch_id)
            if persisted_order and str(persisted_order.get("status")) == "OPEN":
                order_id = str(persisted_order.get("order_id") or "")
                for order in getattr(self.broker, "_reward_exit_orders", {}).values():
                    if getattr(order, "order_id", "") == order_id:
                        cur = order
                        break
                if cur is None:
                    for order in getattr(self.broker, "_exit_orders", {}).values():
                        if getattr(order, "order_id", "") == order_id:
                            cur = order
                            break
                if cur is None:
                    # Reconcile before deciding whether the persisted OPEN
                    # order is still protecting these shares.  A successful
                    # reconcile that still cannot find it is authoritative:
                    # mark the old identity terminal and recreate the exit.
                    reconciled = False
                    if hasattr(self.broker, "reconcile_orders"):
                        with contextlib.suppress(Exception):
                            reconciled = bool(await self._broker_call(
                                self.broker.reconcile_orders))
                    if not reconciled:
                        log.warning(
                            "BATCH_SELL_REHYDRATE_PENDING batch_id=%s order_id=%s "
                            "说明=持久化卖单未在当前订单视图出现且对账未确认，暂不重复挂单",
                            batch_id, order_id,
                        )
                        return
                    for order in getattr(self.broker, "_reward_exit_orders", {}).values():
                        if getattr(order, "order_id", "") == order_id:
                            cur = order
                            break
                    if cur is None:
                        for order in getattr(self.broker, "_exit_orders", {}).values():
                            if getattr(order, "order_id", "") == order_id:
                                cur = order
                                break
                    if cur is None:
                        self.metrics.update_reward_exit_order(
                            order_id, status="CANCELLED")
                        log.warning(
                            "BATCH_SELL_REHYDRATE_STALE batch_id=%s order_id=%s "
                            "说明=对账确认持久化卖单已不在交易所，标记失效并恢复退出挂单",
                            batch_id, order_id,
                        )

        from .brokers import RestingOrder
        GTD_REFRESH_MARGIN_SECS = 30.0  # same as broker's constant
        if cur is not None:
            cur_quote = getattr(cur, "quote", None)
            cur_expiration = float(getattr(cur, "expiration", now) or now)
            if (cur_quote is not None
                    and cur_quote.token_id == complement_token
                    and abs(cur_quote.price - target_price) < 1e-9
                    and abs(cur_quote.size - rem) < 1e-9
                    and (not isinstance(cur, RestingOrder)
                         or cur_expiration - now >= GTD_REFRESH_MARGIN_SECS)):
                return  # unchanged resting order

        if self._reward_exit_mode == "shadow":
            log.warning(
                "BATCH_SELL_PLACED batch_id=%s cid=%s "
                "size=%.0f price=%.4f mode=shadow "
                "说明=shadow模式，不实际挂单",
                batch_id, cid, rem, target_price,
            )
            return

        failure = self._batch_sell_failures.get(batch_id)
        if failure is not None:
            last_failure, failures = failure
            delay = min(2.0 ** failures, 30.0)
            if now - last_failure < delay:
                log.warning(
                    "BATCH_SELL_RETRY_BACKOFF batch_id=%s cid=%s "
                    "failures=%d retry_in=%.1fs "
                    "说明=上次退出卖单失败，退避后再尝试",
                    batch_id, cid, failures, delay - (now - last_failure),
                )
                return

        # Cancel previous per-batch exit order if any
        if cur is not None:
            order_id = str(getattr(cur, "order_id", ""))
            if hasattr(self.broker, "cancel_reward_exit"):
                cancelled = await self._broker_call(
                    self.broker.cancel_reward_exit, batch_id)
            elif hasattr(self.broker, '_batch_cancel'):
                cancelled = await self._broker_call(
                    self.broker._batch_cancel, [order_id])
            else:
                cancelled = False
            if not cancelled:
                log.warning(
                    "BATCH_SELL_CANCEL_FAILED batch_id=%s order_id=%s "
                    "说明=旧卖单撤销失败，保留现状避免重复挂单",
                    batch_id, order_id,
                )
                return
            self._batch_exit_cancel_pending[batch_id] = order_id
            return

        # Place new GTD SELL via the broker's batch-specific API.
        sell_quote = strategy.Quote(complement_token, target_price, rem)
        placed: RestingOrder | None = None
        audit_context = {
            "path": "reward_exit_exit", "intent": "batch_exit",
            "batch_id": batch_id,
        }
        if hasattr(self.broker, "place_reward_exit"):
            async with self._market_lock(cid):
                placed = await self._broker_call(
                    self.broker.place_reward_exit,
                    m, batch_id, sell_quote, audit_context)
        elif hasattr(self.broker, '_place_sell'):
            async with self._market_lock(cid):
                placed = await self._broker_call(
                    self.broker._place_sell, sell_quote, audit_context)

        if placed:
            self._batch_exit_orders[batch_id] = placed
            self._batch_sell_failures.pop(batch_id, None)
            if self.metrics:
                self.metrics.record_reward_exit_order(
                    order_id=placed.order_id, batch_id=batch_id,
                    intent="batch_exit", cid=cid,
                    token_id=complement_token, side="SELL",
                    price=target_price, size=rem,
                    expiration=float(getattr(placed, "expiration", 0) or 0),
                    status="OPEN",
                )
            log.warning(
                "BATCH_SELL_PLACED batch_id=%s cid=%s "
                "size=%.0f price=%.4f mode=active order_id=%s "
                "说明=批次退出卖单已挂出",
                batch_id, cid, rem, target_price, placed.order_id,
            )
        else:
            _, failures = self._batch_sell_failures.get(batch_id, (0.0, 0))
            self._batch_sell_failures[batch_id] = (now, failures + 1)
            if hasattr(self.broker, "reconcile_orders"):
                with contextlib.suppress(Exception):
                    await self._broker_call(self.broker.reconcile_orders)
            log.warning(
                "BATCH_SELL_FAILED batch_id=%s cid=%s "
                "size=%.0f price=%.4f 说明=退出卖单提交失败",
                batch_id, cid, rem, target_price,
            )


    async def _quote_all(self) -> None:
        r = self.cfg["risk"]
        max_inv = r["max_inventory_usd_per_market"]
        derisk_h = r["derisk_hours_before_end"]
        exit_h = r["exit_hours_before_end"]
        widen_max = r["derisk_widen_cents"] / 100.0
        max_stale = self.cfg["guards"]["max_book_staleness_secs"]
        now = time.time()
        self.guards.check_fills(self.broker.fills_log, now)
        self.markouts.ingest(self.broker.fills_log)
        for mo in self.markouts.resolve(self._token_mid, now):
            self.metrics.record_markout(mo)
        for cid, avg_cents, n in self.markouts.toxic_markets():
            m = next((mm for mm in self.markets if mm.condition_id == cid), None)
            # P1-4: never trip a manual_hold market — the operator is
            # managing it by hand.
            manual_hold = self._manual_hold_cids()
            if cid in manual_hold:
                log.info("markout toxic '%s' (%.1fc, n=%d) — 人工持有中，跳过 trip",
                         (m.question if m else cid)[:50], avg_cents, n)
                self.markouts.reset_market(cid)
                continue
            self.guards.trip_market(
                cid, now, f"avg markout {avg_cents:+.1f}c over {n} fills",
                m.question if m else cid)
            # P1.3: markout-ban on trip — 一次 trip 直接 ban，持久化到磁盘，重启不丢失。
            if self.cfg["guards"].get("markout_ban_on_trip", False):
                self._banned_cids.add(cid)
                self._persist_banned_cids()
                log.warning("markout-ban — 已将 %s 加入禁止名单（持久化，重启后仍然有效）",
                            (m.question if m else cid)[:50])
            self.markouts.reset_market(cid)
        managed = {m.condition_id: m for m in self.markets}
        for m in self.broker.held_markets():
            managed.setdefault(m.condition_id, m)
        all_markets = list(managed.values())
        net_exp = self.broker.net_yes_exposure_usd
        # Decide all markets first, then dispatch order ops concurrently so
        # markets late in the iteration aren't quoted on stale books while
        # earlier ones complete their REST round trips.
        updates: list[tuple[gamma.Market, list[strategy.Quote], dict[str, dict] | None]] = []
        empty_selected_flat_cids: set[str] = set()
        self._quote_block_reasons.clear()

        selected_cids = {m.condition_id for m in self.markets}
        manual_hold = self._manual_hold_cids()
        for m in all_markets:
            if m.condition_id in manual_hold:
                continue
            # P1.x: skip normal quoting for reward_exit_locked CIDs
            if m.condition_id in self._reward_exit_locked:
                self._quote_block_reasons[m.condition_id] = "奖励退出批次进行中，暂停普通报价"
                self.metrics.sample_uptime(m.condition_id, False)
                # Still allow exit orders for SELL_PENDING batches to stay
                if self.broker.open_quotes(m):
                    log.info("奖励退出锁定市场 '%s' — 撤销普通报价", m.question[:45])
                    updates.append((m, [], None))
                continue
            if m.condition_id in self._awaiting_top_n_rescan:
                self._quote_block_reasons[m.condition_id] = "奖励退出完成，等待重新进入TopN"
                self.metrics.sample_uptime(m.condition_id, False)
                if self.broker.open_quotes(m):
                    updates.append((m, [], None))
                continue
            unpaired = self.broker.unpaired_shares(m)
            needs_recovery = abs(unpaired) >= MIN_TAKER_SHARES
            h = hours_to_end(m, now)
            if h is not None and h <= exit_h:
                self._quote_block_reasons[m.condition_id] = "临近结算，已停止新增报价"
                if needs_recovery:
                    self._log_inventory_recovery_skip(
                        m, unpaired=unpaired, reason="near_resolution")
                    if self.metrics:
                        self.metrics.record_recovery_event(
                            m.condition_id, "skip", unpaired, reason="near_resolution")
                if self.broker.open_quotes(m):
                    log.warning("'%s' %.1f小时后结算 — 退出市场", m.question[:45], h)
                    updates.append((m, [], None))
                continue
            yes_book = self.tracker.books[m.yes_token]
            no_book = self.tracker.books[m.no_token]
            if yes_book.mid is not None:
                self.guards.record_mid(m.condition_id, yes_book.mid, now, m.question)
            feed_age = self.tracker.feed_age(now)
            book_age = now - min(yes_book.updated_ts, no_book.updated_ts)
            if strategy.book_feed_stale(feed_age, book_age, max_stale):
                self._quote_block_reasons[m.condition_id] = (
                    f"行情或订单簿过期（行情 {feed_age:.0f} 秒，订单簿 {book_age:.0f} 秒）")
                self.metrics.sample_uptime(m.condition_id, False)
                if needs_recovery:
                    self._log_inventory_recovery_skip(
                        m, unpaired=unpaired, reason="stale_book",
                        yes_book=yes_book, no_book=no_book)
                    if self.metrics:
                        self.metrics.record_recovery_event(
                            m.condition_id, "skip", unpaired, reason="stale_book")
                if self.broker.open_quotes(m):
                    log.warning("数据源/订单簿过期 (数据源%.0fs, 订单簿%.0fs) — "
                                "撤下 '%s' 报价",
                                feed_age, book_age, m.question[:45])
                    updates.append((m, [], None))
                continue
            band = m.max_spread_cents / 100.0
            max_spread_mult = float(
                self.cfg["quoting"].get("max_book_spread_mult_of_band", 3.0))
            if (not strategy.book_is_quotable(yes_book, band, max_spread_mult)
                    or not strategy.book_is_quotable(no_book, band, max_spread_mult)):
                self._quote_block_reasons[m.condition_id] = "YES/NO 订单簿不可报价"
                self.metrics.sample_uptime(m.condition_id, False)
                if needs_recovery:
                    self._log_inventory_recovery_skip(
                        m, unpaired=unpaired, reason="unquotable_book",
                        yes_book=yes_book, no_book=no_book)
                    if self.metrics:
                        self.metrics.record_recovery_event(
                            m.condition_id, "skip", unpaired, reason="unquotable_book")
                if self.broker.open_quotes(m):
                    log.warning("订单簿双向不可报价 — 撤下 '%s'",
                                m.question[:45])
                    updates.append((m, [], None))
                continue
            cooled_down = not self.guards.allow(m.condition_id, now)
            if not self.risk.theme_quoting_ok(m, all_markets, net_exp, self._scale):
                self._quote_block_reasons[m.condition_id] = "主题仓位达到上限，暂停新增报价"
                self.metrics.sample_uptime(m.condition_id, False)
                if needs_recovery:
                    self._log_inventory_recovery_skip(
                        m, unpaired=unpaired, reason="theme_inventory_cap",
                        yes_book=yes_book, no_book=no_book)
                    if self.metrics:
                        self.metrics.record_recovery_event(
                            m.condition_id, "skip", unpaired, reason="theme_inventory_cap")
                if self.broker.open_quotes(m):
                    log.warning("主题库存上限 — '%s' 不报价",
                                m.question[:45])
                    updates.append((m, [], None))
                continue
            derisk_frac = 1.0
            if h is not None and h <= derisk_h:
                derisk_frac = max(0.25, (h - exit_h) / max(derisk_h - exit_h, 1e-9))
            eff_max_inv = max_inv * derisk_frac * self._scale
            widen = (1.0 - derisk_frac) * widen_max
            exposure = net_exp(m)
            if (not self.risk.market_inventory_ok(exposure, eff_max_inv)
                    or self.risk.theme_at_cap(m, all_markets, net_exp, self._scale)):
                exposure = eff_max_inv if exposure > 0 else -eff_max_inv
            fade_yes, fade_no = self._fades(m, now)
            flow_yes, flow_no = self.guards.check_flow(m, now)
            flow_imb = self.guards.flow_imbalance(m, now)
            markout_avg = self.markouts.market_avg(m.condition_id)
            size_factor = self._size_factors.get(m.condition_id, 1.0)
            pricing: dict[str, float] = {}
            selected = m.condition_id in selected_cids
            quote_cfg = (self.cfg if selected else
                         self._quote_cfg_for_inventory_recovery(unpaired))
            desired = strategy.compute_quotes(
                m, yes_book, exposure, quote_cfg, eff_max_inv,
                fade_yes=fade_yes + widen + flow_yes,
                fade_no=fade_no + widen + flow_no,
                scale=self._scale,
                flow_imbalance=flow_imb,
                markout_avg=markout_avg,
                size_factor=size_factor,
                pricing=pricing,
                min_quote_size=(abs(unpaired)
                                if needs_recovery and not selected else None),
            )
            normal_desired = desired
            recovery_path = "normal"
            escalate_secs = float(self.cfg["risk"].get("recovery_escalate_after_minutes", 0)) * 60.0
            if m.condition_id not in selected_cids:
                desired = self._held_market_recovery_quotes(m, desired, unpaired, now)
                recovery_path = "inventory_recovery"
                # Detect escalation by checking the same condition as
                # _held_market_recovery_quotes — not by price comparison,
                # because the soft window can push a pair-capped quote above cap.
                if (escalate_secs > 0 and self.broker is not None
                        and abs(unpaired) >= MIN_TAKER_SHARES and desired):
                    last_fill = self.broker.last_fill_ts(m.condition_id)
                    if last_fill is not None and now - last_fill >= escalate_secs:
                        desired = self._escalated_recovery_quotes(
                            m, normal_desired, unpaired, yes_book=yes_book,
                            exposure_usd=exposure, max_inventory_usd=eff_max_inv,
                            fade_yes=fade_yes + widen + flow_yes,
                            fade_no=fade_no + widen + flow_no,
                            flow_imbalance=flow_imb, markout_avg=markout_avg)
                        pricing = self._recovery_pricing.get(m.condition_id, pricing)
                        recovery_path = "market_center_recovery"
            elif cooled_down:
                desired = self._cooldown_recovery_quotes(m, desired, unpaired, now)
                recovery_path = "cooldown_recovery"
                if (escalate_secs > 0 and self.broker is not None
                        and abs(unpaired) >= MIN_TAKER_SHARES and desired):
                    last_fill = self.broker.last_fill_ts(m.condition_id)
                    if last_fill is not None and now - last_fill >= escalate_secs:
                        desired = self._escalated_recovery_quotes(
                            m, normal_desired, unpaired, yes_book=yes_book,
                            exposure_usd=exposure, max_inventory_usd=eff_max_inv,
                            fade_yes=fade_yes + widen + flow_yes,
                            fade_no=fade_no + widen + flow_no,
                            flow_imbalance=flow_imb, markout_avg=markout_avg)
                        pricing = self._recovery_pricing.get(m.condition_id, pricing)
                        recovery_path = "market_center_recovery"
            else:
                desired = self._selected_market_recovery_quotes(m, desired, unpaired)
                if abs(unpaired) >= MIN_TAKER_SHARES:
                    recovery_path = "inventory_recovery"
                # Escalate to Phase 2 after the window, same as held-only / cooldown.
                if (escalate_secs > 0 and self.broker is not None
                        and abs(unpaired) >= MIN_TAKER_SHARES):
                    last_fill = self.broker.last_fill_ts(m.condition_id)
                    if last_fill is not None and now - last_fill >= escalate_secs:
                        desired = self._escalated_recovery_quotes(
                            m, normal_desired, unpaired, yes_book=yes_book,
                            exposure_usd=exposure, max_inventory_usd=eff_max_inv,
                            fade_yes=fade_yes + widen + flow_yes,
                            fade_no=fade_no + widen + flow_no,
                            flow_imbalance=flow_imb, markout_avg=markout_avg)
                        pricing = self._recovery_pricing.get(m.condition_id, pricing)
                        recovery_path = "market_center_recovery"
                desired = self._filter_quotes_for_side_guard(
                    desired, unpaired=unpaired, now=now,
                    recovery_token=(m.no_token if unpaired > 0 else m.yes_token)
                    if needs_recovery else None)
            if not desired:
                self._quote_block_reasons.setdefault(
                    m.condition_id, "策略未生成可提交报价（价格、规模或仓位约束未满足）")
                if m.condition_id in selected_cids and not needs_recovery:
                    empty_selected_flat_cids.add(m.condition_id)
            if needs_recovery and not desired:
                basis_fn = getattr(self.broker, "unpaired_cost_basis", None)
                basis = basis_fn(m) if basis_fn else None
                reason = "unknown_cost_basis" if basis is None else "no_complement_quote"
                complement = m.no_token if unpaired > 0 else m.yes_token
                min_order_size = self._clob_min_order_size(complement)
                if (min_order_size is not None
                        and abs(unpaired) < min_order_size):
                    reason = "below_current_clob_min_order_size"
                elif not normal_desired:
                    reason = (str(pricing.get("reason"))
                              if recovery_path == "market_center_recovery"
                              and pricing.get("reason") else "strategy_no_quote")
                self._log_inventory_recovery_skip(
                    m, unpaired=unpaired, reason=reason, basis=basis,
                    yes_book=yes_book, no_book=no_book)
                if self.metrics:
                    self.metrics.record_recovery_event(
                        m.condition_id, "skip", unpaired,
                        reason=reason, recovery_path=recovery_path)
            # ── P0: quote risk decision — only for normal two-sided quotes ──
            is_recovery = (needs_recovery
                           and recovery_path
                           and recovery_path != "normal")
            if not is_recovery and desired:
                markout_avg = self.markouts.market_avg(m.condition_id)
                highest_horizon = max(self.markouts.horizons) if self.markouts.horizons else 300.0
                markout_n = len([
                    s for s in self.markouts._samples.get(m.condition_id, [])
                    if s[1] == highest_horizon
                ]) if self.markouts._samples.get(m.condition_id) else 0
                # Per-side markout for directional danger targeting
                yes_avg, no_avg, yes_n, no_n = self.markouts.market_avg_by_side(
                    m.condition_id, m.yes_token, m.no_token)
                risk_decision = self.guards.quote_risk_decision(
                    m, now, markout_avg=markout_avg, markout_samples=markout_n,
                    markout_yes_avg=yes_avg, markout_no_avg=no_avg,
                    markout_yes_samples=yes_n, markout_no_samples=no_n,
                    own_fills=self.broker.fills_log)
                # Record the decision for audit regardless of mode
                if self.metrics is not None:
                    self.metrics.record_quote_risk_decision(
                        m.condition_id,
                        self.guards.quote_risk_mode,
                        risk_decision,
                        ts=now,
                        market=m.question,
                    )
                # Apply the decision in active mode
                if self.guards.quote_risk_mode == "active":
                    desired = self._apply_quote_risk_decision(
                        desired, risk_decision, m)
            current = self.broker.open_quotes(m)
            if self._can_retain_recovery_quote(m, current, unpaired):
                final = current
            else:
                final = strategy.reconcile_quotes(
                    current, desired, self.cfg["quoting"]["requote_move_cents"])
            # ── log phase transitions (debounced) ──
            if needs_recovery and recovery_path != "normal":
                prev = self._recovery_phase_logged.get(m.condition_id)
                if prev != recovery_path:
                    phase_label = {
                        "inventory_recovery": "Phase 1 (软窗口补单)",
                        "cooldown_recovery": "Phase 1 (冷却期补单)",
                        "market_center_recovery": "Phase 2 (升级: 盘口中枢补单)",
                    }
                    log.warning("补单阶段 '%s': %s  敞口=%.0f",
                                phase_label.get(recovery_path, recovery_path),
                                m.question[:60], unpaired)
                    self._recovery_phase_logged[m.condition_id] = recovery_path
            elif recovery_path == "normal":
                self._recovery_phase_logged.pop(m.condition_id, None)
            # Phase 2 (escalated recovery) only quotes one side — judge "in-band"
            # by whether the complement is present (not whether both sides are).
            is_escalated = recovery_path == "market_center_recovery"
            if is_escalated:
                complement = m.no_token if unpaired > 0 else m.yes_token
                in_band = any(q.token_id == complement for q in final)
            else:
                in_band = (len(final) == 2
                           and any(q.token_id == m.yes_token for q in final)
                           and any(q.token_id == m.no_token for q in final))
            self.metrics.sample_uptime(m.condition_id, in_band)
            # Repost when the quote actually changed OR when a resting order is
            # near GTD expiry — otherwise a stable quote (unchanged key-set) is
            # never re-sent through set_quotes, so it silently expires on the
            # book and leaves a gap until the next reconcile notices it's gone.
            changed = {q.key() for q in final} != {q.key() for q in current}
            if changed or (final and self.broker.due_for_refresh(m)):
                basis_fn = getattr(self.broker, "unpaired_cost_basis", None)
                basis = basis_fn(m) if abs(unpaired) >= MIN_TAKER_SHARES and basis_fn else None
                cap = self._forced_hedge_max_price(m, basis) if basis is not None else None
                if abs(unpaired) >= MIN_TAKER_SHARES:
                    complement = m.no_token if unpaired > 0 else m.yes_token
                    recovery_quote = next((q for q in final if q.token_id == complement), None)
                    if recovery_quote is not None:
                        proposed_price = pricing.get(
                            "yes_bid_quote" if complement == m.yes_token else "no_bid_quote",
                            recovery_quote.price)
                        fee = m.fee_bps / 10_000.0 * (
                            recovery_quote.price * (1.0 - recovery_quote.price)) ** m.fee_exponent
                        expected = (1.0 - basis - recovery_quote.price - fee
                                    if basis is not None else None)
                        self._log_inventory_recovery_quote(
                            m, unpaired=unpaired, quote=recovery_quote,
                            yes_book=yes_book, no_book=no_book, pricing=pricing,
                            pair_cap=cap, hard_cap=cap, cost_basis=basis,
                            proposed_price=proposed_price)
                        if self.metrics:
                            self.metrics.record_recovery_event(
                                m.condition_id, "quote_placed", unpaired,
                                recovery_path=recovery_path,
                                quote_price=recovery_quote.price,
                                pair_cap=cap, proposed_price=proposed_price,
                                cost_basis=basis, fee_per_share=fee,
                                expected_pair_pnl=expected, hard_cap=cap)
                audit_context = {}
                for q in final:
                    fee = m.fee_bps / 10_000.0 * (q.price * (1.0 - q.price)) ** m.fee_exponent
                    audit_context[q.token_id] = {
                        "path": recovery_path,
                        "unpaired_cost": basis,
                        "pair_cap": cap,
                        "strike_pair_cap": cap,
                        "expected_pair_pnl": 1.0 - basis - q.price - fee if basis is not None else None,
                    }
                    if (abs(unpaired) >= MIN_TAKER_SHARES
                            and q.token_id == (m.no_token if unpaired > 0 else m.yes_token)):
                        audit_context[q.token_id]["recovery_order"] = True
                updates.append((m, final, audit_context))

        if updates:
            await asyncio.gather(
                *(self._set_quotes_locked(m, q, audit) for m, q, audit in updates))
        if (empty_selected_flat_cids
                and now - self._last_rotate >= ROTATE_MIN_INTERVAL_SECS):
            log.info("%d 个空仓市场未生成可提交报价，重新扫描以补足报价槽位",
                     len(empty_selected_flat_cids))
            await self._rescan(rotate=True, exclude_cids=empty_selected_flat_cids)

    async def _manage_inventory(self, now: float) -> None:
        manual_hold = self._manual_hold_cids()
        quoted = {m.condition_id for m in self.markets if m.condition_id not in manual_hold}
        managed = {m.condition_id: m for m in self.markets if m.condition_id not in manual_hold}
        for m in self.broker.held_markets():
            if m.condition_id not in manual_hold:
                managed.setdefault(m.condition_id, m)
        if not managed:
            return
        # Exits/hedges on different markets are independent — run them
        # concurrently so one slow hedge doesn't delay the others.
        await asyncio.gather(*(
            self._manage_market_inventory(cid, m, managed, quoted, now)
            for cid, m in managed.items()))

    async def _manage_market_inventory(self, cid: str, m: gamma.Market,
                                       managed: dict[str, gamma.Market],
                                       quoted: set[str], now: float) -> None:
        # P1.x: skip old recovery for reward_exit_locked CIDs
        if cid in self._reward_exit_locked or cid in self._awaiting_top_n_rescan:
            return
        r = self.cfg["risk"]
        threshold = r["flatten_threshold_usd"] * self._scale
        wait = r["flatten_after_secs"]
        max_spread = r["flatten_max_spread_cents"] / 100.0
        exit_h = r["exit_hours_before_end"]
        passive = bool(r.get("passive_exit", True))

        # A confirmed FAK hedge is locally overlaid until the Data API catches
        # up.  Do not submit another hedge against a stale REST snapshot.
        if getattr(self.broker, "has_pending_hedge", lambda _cid: False)(cid):
            return

        unpaired = self.broker.unpaired_shares(m)
        if abs(unpaired) < MIN_TAKER_SHARES:
            self._over_since.pop(cid, None)
            if hasattr(self.broker, "unpaired_since"):
                self.broker.unpaired_since.pop(cid, None)
            # Close any open recovery episode — position is flat.
            # Preserve the accumulated outcome data (chosen_path,
            # expected_loss_usd, actual_loss_usd) that partial fills
            # may have written; close_recovery_episode() overwrites
            # those columns with the passed values, so fetch the
            # existing row first.
            if self.metrics and r.get("recovery_episode_mode", "shadow") != "off":
                existing = self.metrics.get_open_episode(cid)
                close_actual = existing.get("actual_loss_usd") if existing else None
                # P1: a sell_original GTD order carries a reserved
                # expected loss.  When the position goes flat the order
                # (or a portion of it) has presumably executed.  Compute
                # actual loss from confirmed exit fills in fills_log
                # rather than blindly folding the full reservation, so
                # the closed episode reflects the true realised cost.
                # Caveat: exit fills older than the episode's started_ts
                # are intentionally excluded — those belong to a prior
                # episode.
                if existing and existing.get("sell_reserved_loss_usd"):
                    ep_start = (float(existing["started_ts"])
                                if existing.get("started_ts") is not None
                                else 0.0)
                    # Broker cost basis is unavailable when the position
                    # is already flat (unpaired_cost_basis returns None).
                    # Recover it from the persisted recovery_events that
                    # were recorded during the episode — every tick's
                    # record_recovery_event() includes a cost_basis.
                    flat_basis = (self.metrics.recovery_event_cost_basis(
                        cid, ep_start) if self.metrics else None)
                    realized = _compute_sell_realized_loss(
                        self.broker, m, cid, flat_basis, ep_start)
                    prev_actual = (
                        float(existing["actual_loss_usd"])
                        if existing.get("actual_loss_usd") is not None
                        else 0.0)
                    close_actual = prev_actual + realized
                self.metrics.close_recovery_episode(
                    cid=cid, closed_ts=now,
                    chosen_path=existing.get("chosen_path") if existing else None,
                    expected_loss_usd=existing.get("expected_loss_usd") if existing else None,
                    actual_loss_usd=close_actual,
                    reason="flat")
            await self._broker_call(self.broker.set_exit, m, None)
            return
        exposure = self.broker.net_yes_exposure_usd(m)
        h = hours_to_end(m, now)
        urgent = h is not None and h <= exit_h
        if not urgent:
            theme_markets = list(managed.values())
            if self.risk.theme_at_cap(m, theme_markets,
                                      self.broker.net_yes_exposure_usd,
                                      self._scale):
                urgent = True
        start = self._over_since.setdefault(cid, now)
        # If the broker recorded the fill that caused the unpaired position,
        # use that timestamp instead of the first detection time. This gives
        # the soft-recovery and escalate windows a more accurate start.
        last_fill_ts = getattr(self.broker, "last_fill_ts", lambda _cid: None)(cid)
        if last_fill_ts is not None and last_fill_ts <= now:
            start = min(start, last_fill_ts)
            self._over_since[cid] = start
        # Persist unpaired_since to survive restarts.
        if hasattr(self.broker, "unpaired_since"):
            self.broker.unpaired_since[cid] = self._over_since[cid]
            persist_fn = getattr(self.broker, "_persist_unpaired_since", None)
            if persist_fn is not None:
                persist_fn()
        # The passive maker exit (below) runs before the episode controller.
        # In active mode the episode controller owns inventory management —
        # skip the legacy exit so it doesn't bypass the episode loss budget
        # or place orders the controller didn't decide.
        # Shadow and off keep the old exit unchanged (P1: shadow broker
        # calls must match pre-episode behaviour exactly).
        episode_mode_tmp = r.get("recovery_episode_mode", "shadow")
        if episode_mode_tmp != "active":
            if not urgent and abs(exposure) >= threshold and passive and cid in quoted:
                await self._update_exit_sell(m, unpaired)
        if now - self._last_flatten.get(cid, 0.0) < FLATTEN_RETRY_SECONDS:
            return
        self._last_flatten[cid] = now

        # ── P1 recovery episode controller ──
        episode_mode = r.get("recovery_episode_mode", "shadow")
        _VALID_MODES = frozenset({"off", "shadow", "active"})
        if episode_mode not in _VALID_MODES:
            log.warning(
                "RECOVERY_EPISODE_BAD_MODE mode=%r 说明=config 中 recovery_"
                "episode_mode 值无效，已回退为 shadow。合法值：off/shadow/active",
                episode_mode,
            )
            episode_mode = "shadow"
        if episode_mode == "off":  # P0-2: only exact "off" skips new logic
            pass
        else:
            held_yes = unpaired > 0
            complement_token = m.no_token if held_yes else m.yes_token
            original_token = m.yes_token if held_yes else m.no_token
            complement_book = self.tracker.books.get(complement_token)
            original_book = self.tracker.books.get(original_token)
            complement_ask = complement_book.best_ask if complement_book else None
            original_bid = original_book.best_bid if original_book else None
            basis_fn = getattr(self.broker, "unpaired_cost_basis", None)
            basis = basis_fn(m) if basis_fn else None
            elapsed = now - start
            max_loss = float(r.get("recovery_max_loss_usd_per_market", 3.0))
            escalate_secs = float(r.get("recovery_escalate_after_secs", 180))
            terminal_secs = float(r.get("recovery_terminal_after_secs", 900))

            # P1: deduct accumulated actual_loss from partial fills and
            # reserved sell_original loss so the total loss across all
            # actions within a single episode stays within the per-market
            # cap.
            accumulated_loss: float = 0.0
            if self.metrics:
                existing = self.metrics.get_open_episode(cid)
                if existing:
                    if existing.get("actual_loss_usd") is not None:
                        accumulated_loss += float(existing["actual_loss_usd"])
                    if existing.get("sell_reserved_loss_usd") is not None:
                        accumulated_loss += float(existing["sell_reserved_loss_usd"])
            remaining_budget = max_loss - accumulated_loss

            # P1-5: near-resolution override.  When a market is inside the
            # exit window, being locked into resolution (a binary outcome)
            # is worse than a moderate exit loss.  Skip the per-episode
            # loss-budget gate so the cheapest available path is always
            # taken, letting forced-hedge / sell-original proceed.
            near_end = (h is not None and h <= exit_h) or urgent

            if near_end and remaining_budget <= 0.0:
                log.warning(
                    "RECOVERY_NEAR_END_OVERRIDE market='%s' cid=%s "
                    "h_to_end=%.1f exit_h=%.1f urgent=%s "
                    "max_loss=%.4f accumulated=%.4f remaining=%.4f "
                    "说明=市场临近结算，跳过 per-episode 损失预算限制，"
                    "执行最便宜的恢复路径",
                    m.question[:45], cid,
                    h if h is not None else float("inf"), exit_h, urgent,
                    max_loss, accumulated_loss, remaining_budget,
                )

            if elapsed >= terminal_secs:
                stage = "terminal"
            elif elapsed >= escalate_secs:
                stage = "escalated"
            else:
                stage = "passive"

            if remaining_budget <= 0.0 and not near_end:
                log.warning(
                    "RECOVERY_BUDGET_EXHAUSTED market='%s' cid=%s "
                    "max_loss=%.4f accumulated=%.4f remaining=%.4f "
                    "说明=本 episode 损失预算已耗尽，跳过恢复决策",
                    m.question[:45], cid, max_loss, accumulated_loss,
                    remaining_budget,
                )
                if self.metrics:
                    self.metrics.update_recovery_episode(
                        cid=cid,
                        peak_abs_exposure_usd=abs(exposure), stage=stage,
                        # P2: budget_exhausted is a state, not an executed
                        # action — don't overwrite a previously recorded
                        # executed path (buy_complement or sell_original).
                    )
                    self.metrics.record_recovery_event(
                        cid, "budget_exhausted", unpaired,
                        recovery_path="budget_exhausted", cost_basis=basis,
                        complement_ask=complement_ask,
                        original_bid=original_bid,
                        mkt_fee_bps=m.fee_bps,
                        mkt_fee_exponent=m.fee_exponent,
                    )
                # Fall through to old forced-hedge path — do NOT return.
                # Skip the rest of the episode controller so
                # choose_recovery_action is not called with a stale
                # budget.
                #
                # However, the old forced-hedge path can still submit a
                # taker FAK, which would bypass the episode loss cap.
                # For active mode, cancel the exit and return — do NOT
                # re-issue with an unvalidated price that hasn't been
                # through the episode budget (P1).
                # For shadow/off, fall through to the old forced-hedge
                # path unchanged.
                if episode_mode == "active":
                    if self.metrics:
                        self.metrics.record_recovery_event(
                            cid, "budget_exhausted_no_hedge", unpaired,
                            recovery_path="budget_exhausted",
                            cost_basis=basis,
                            complement_ask=complement_ask,
                            original_bid=original_bid,
                            mkt_fee_bps=m.fee_bps,
                            mkt_fee_exponent=m.fee_exponent,
                        )
                    # Cancel exit; do NOT re-issue at an unvalidated price.
                    await self._broker_call(self.broker.set_exit, m, None)
                    return
                # shadow/off: fall through to old forced-hedge path
            else:
                quote = choose_recovery_action(
                    market=m, unpaired=unpaired, basis=basis,
                    complement_ask=complement_ask, original_bid=original_bid,
                    elapsed_secs=elapsed, max_loss_usd=remaining_budget,
                    force_execute=near_end,
                )

                if self.metrics:
                    self.metrics.open_recovery_episode(
                        cid=cid, started_ts=start, initial_unpaired=unpaired,
                        peak_abs_exposure_usd=abs(exposure), stage=stage,
                    )
                    self.metrics.update_recovery_episode(
                        cid=cid, peak_abs_exposure_usd=abs(exposure), stage=stage,
                        # P2: do NOT write chosen_path here — this is a poll
                        # decision (not an executed action).  chosen_path is
                        # only written in the sell_original / buy_complement
                        # execution blocks below, so the episode's final
                        # recorded path reflects what was actually done.
                        expected_loss_usd=quote.expected_loss_usd,
                    )

                log.info(
                    "RECOVERY_EPISODE_DECISION market='%s' mode=%s path=%s stage=%s "
                    "unpaired=%.0f elapsed=%.0fs expected_loss_usd=%s reason=%s "
                    "near_end=%s "
                    "说明=P1恢复策略决策",
                    m.question[:45], episode_mode, quote.path, stage,
                    unpaired, elapsed,
                    "unknown" if quote.expected_loss_usd is None
                    else f"{quote.expected_loss_usd:.4f}",
                    quote.reason or "n/a",
                    "yes" if near_end else "no",
                )

                # P1-2: in passive stage, the episode controller only
                # records — don't execute until escalated or terminal.
                # P1-4: on manual_hold, cancel exit quotes and return to
                # prevent the quote loop from issuing new recovery-maker
                # orders; the old forced-hedge path below controls the
                # complement-side exit instead.
                if quote.path == "manual_hold":
                    if self.metrics:
                        self.metrics.record_recovery_event(
                            cid, "manual_hold", unpaired, reason=quote.reason,
                            recovery_path="manual_hold", cost_basis=basis,
                            complement_ask=complement_ask,
                            original_bid=original_bid,
                            mkt_fee_bps=m.fee_bps,
                            mkt_fee_exponent=m.fee_exponent,
                        )
                    # P1-1: In active mode only, cancel the exit quote.
                    # In shadow/off modes, do NOT touch broker state — the
                    # old forced-hedge path below handles exits unchanged.
                    if episode_mode == "active":
                        await self._broker_call(self.broker.set_exit, m, None)
                        # Return — do NOT fall through to old forced-hedge
                        # path.  manual_hold means all automatic paths are
                        # exhausted; letting the old path run would re-issue
                        # exit quotes via _update_exit_sell(), undoing the
                        # cancellation and re-entering a rejected path.
                        return
                    # shadow/off: fall through to old forced-hedge path

                elif quote.path == "wait":
                    if self.metrics:
                        self.metrics.record_recovery_event(
                            cid, "wait", unpaired, reason=quote.reason,
                            recovery_path="wait", cost_basis=basis,
                            complement_ask=complement_ask,
                            original_bid=original_bid,
                            mkt_fee_bps=m.fee_bps,
                            mkt_fee_exponent=m.fee_exponent,
                        )
                    # P1-1: fall through to old forced-hedge path

                elif episode_mode == "shadow":
                    # P1-1: shadow records the decision but does not return
                    # — the old forced-hedge path below still runs, so
                    # broker behaviour is unchanged from "off".
                    if self.metrics:
                        self.metrics.record_recovery_event(
                            cid, quote.path, unpaired,
                            recovery_path=quote.path,
                            proposed_price=quote.price,
                            cost_basis=basis,
                            expected_pair_pnl=(
                                -(quote.expected_loss_usd or 0.0) / quote.size
                                if quote.size > 0 else None),
                            complement_ask=complement_ask,
                            original_bid=original_bid,
                            mkt_fee_bps=m.fee_bps,
                            mkt_fee_exponent=m.fee_exponent,
                        )
                    # P1-1: fall through to old forced-hedge path

                elif episode_mode == "active":
                    # P0-2 guard: only exact "active" reaches this branch.

                    # P1-2: in passive stage, only record — wait for
                    # escalation before taking action.
                    if stage == "passive":
                        if self.metrics:
                            self.metrics.record_recovery_event(
                                cid, f"recovery_waiting_{stage}", unpaired,
                                recovery_path=quote.path, proposed_price=quote.price,
                                cost_basis=basis,
                            )
                        # P1-2: return — do NOT fall through to old forced-hedge
                        # path. In passive stage the episode controller is
                        # responsible for managing inventory; allowing the old
                        # path to run would bypass the episode's escalate
                        # waiting period (e.g. old flatten_after_secs=90s may
                        # FAK before episode escalates at 180s).
                        return

                    else:
                        # ── escalated or terminal: execute the chosen path ──
                        if quote.price is None or quote.token_id is None:
                            log.error(
                                "RECOVERY_EPISODE_BAD_QUOTE market='%s' path=%s "
                                "price=%s token=%s 说明=有效路径但报价不完整",
                                m.question[:45], quote.path, quote.price,
                                quote.token_id,
                            )
                            # fall through to old forced-hedge path
                        else:
                            price: float = quote.price
                            token_id: str = quote.token_id
                            size: float = quote.size

                            if quote.path == "sell_original":
                                # P0-1: place a reduce-only limit SELL (GTD),
                                # not taker_buy.  The matching engine fills
                                # against the bid.  Do NOT record as "filled"
                                # here — the order is resting, not executed.
                                #
                                # P1-1: only cancel+replace when the quote has
                                # changed materially (same diff check as
                                # _update_exit_sell).  This preserves queue
                                # position on a resting exchange order book.
                                cur = self.broker.exit_quote(m)
                                move = self.cfg["quoting"]["requote_move_cents"]
                                if (cur is not None and cur.token_id == token_id
                                        and abs(cur.price - price) * 100 < move
                                        and abs(cur.size - size) <= 0.1 * size):
                                    # unchanged — keep the existing resting order
                                    placed_ok = True
                                else:
                                    sell_quote = strategy.Quote(token_id, price, size)
                                    placed_ok = bool(await self._broker_call(
                                        self.broker.set_exit, m, sell_quote))
                                if placed_ok:
                                    log.warning(
                                    "RECOVERY_SELL_ORIGINAL_PLACED market='%s' "
                                    "token=%s size=%.0f price=%.3f "
                                    "expected_loss_usd=%s stage=%s "
                                    "说明=已挂 reduce-only GTD 卖单，不记录"
                                    "为已成交；由 userfeed 后续确认",
                                    m.question[:45],
                                    "YES" if token_id == m.yes_token else "NO",
                                    size, price,
                                    "unknown"
                                    if quote.expected_loss_usd is None
                                    else f"{quote.expected_loss_usd:.4f}",
                                    stage,
                                )
                                else:
                                    log.warning(
                                        "RECOVERY_SELL_ORIGINAL_FAILED market='%s' "
                                        "token=%s size=%.0f price=%.3f "
                                        "expected_loss_usd=%s stage=%s "
                                        "说明=退出卖单提交失败，余额不足或竞态（仓位已平）",
                                        m.question[:45],
                                        "YES" if token_id == m.yes_token else "NO",
                                        size, price,
                                        "unknown"
                                        if quote.expected_loss_usd is None
                                        else f"{quote.expected_loss_usd:.4f}",
                                        stage,
                                    )
                                if self.metrics and placed_ok:
                                    self.metrics.record_recovery_event(
                                        cid,
                                        f"recovery_{quote.path}_placed",
                                        unpaired,
                                        recovery_path=quote.path,
                                        proposed_price=price,
                                        cost_basis=basis,
                                        expected_pair_pnl=(
                                            -(quote.expected_loss_usd or 0.0)
                                            / size if size > 0 else None),
                                        complement_ask=complement_ask,
                                        original_bid=original_bid,
                                        mkt_fee_bps=m.fee_bps,
                                        mkt_fee_exponent=m.fee_exponent,
                                    )
                                    # P1: reserve the expected loss against
                                    # the episode budget exactly once per
                                    # episode.  sell_reserved_loss_usd
                                    # guards against re-reserving the same
                                    # GTD order on every tick.
                                    # P1-2: when the quoted price worsens
                                    # (expected_loss_usd increases), update
                                    # the reservation so the budget
                                    # correctly reflects the new risk.
                                    sell_expected = (
                                        quote.expected_loss_usd
                                        if quote.expected_loss_usd is not None
                                        else 0.0)
                                    self.metrics.set_sell_reserved_loss(
                                        cid, sell_expected)
                                    self.metrics.update_recovery_episode(
                                        cid=cid,
                                        peak_abs_exposure_usd=abs(exposure),
                                        stage=stage,
                                        chosen_path=quote.path,
                                        expected_loss_usd=quote.expected_loss_usd,
                                    )
                                # P0 new: return — do NOT fall through to
                                # old forced-hedge path.  A resting SELL
                                # exit is our action; the old path would
                                # issue a duplicate complement BUY.
                                return

                            # buy_complement: FAK taker buy.
                            # Cancel any stale exit quote first — if a prior
                            # tick chose sell_original and a GTD SELL is
                            # still resting, that order could fill *after* the
                            # FAK flattens the position, creating a fresh
                            # naked position in the opposite direction.
                            #
                            # LiveBroker.set_exit(None) returns False on cancel
                            # failure (reconcile_orders + early return at
                            # L1106).  Verify the exit was actually cleared
                            # before sending the FAK; otherwise skip this tick
                            # and retry cancellation next poll.
                            await self._broker_call(
                                self.broker.set_exit, m, None)
                            exit_still_resting = getattr(
                                self.broker, "exit_quote", lambda _m: None)(m)
                            if exit_still_resting is not None:
                                log.warning(
                                    "RECOVERY_FAK_ABORTED market='%s' cid=%s "
                                    "原因=撤旧卖单失败，跳过本 tick 避免逆向裸仓",
                                    m.question[:45], cid)
                                return
                            # The exit order may have partially filled between
                            # the cancel request and its execution on the
                            # exchange.  Re-read unpaired shares after a
                            # fresh Data API position refresh — the local
                            # cache from the last poll is stale until the
                            # userfeed or refresh_state() catches up.
                            # If the refresh fails, skip this tick — stale
                            # position data would cause an over-sized FAK
                            # and potential reverse inventory.
                            if not self.paper:
                                refreshed = await asyncio.to_thread(
                                    self.broker.refresh_state)
                                if not refreshed:
                                    log.warning(
                                        "RECOVERY_FAK_ABORTED market='%s' cid=%s "
                                        "原因=仓位刷新失败，跳过本 tick 避免使用过期数据",
                                        m.question[:45], cid)
                                    return
                            unpaired = self.broker.unpaired_shares(m)
                            if abs(unpaired) < MIN_TAKER_SHARES:
                                # The exit fill fully flattened the position;
                                # the episode controller will close the
                                # episode on the next poll.
                                log.warning(
                                    "RECOVERY_FAK_SKIPPED_FLAT market='%s' cid=%s "
                                    "unpaired=%.0f 说明=撤单期间成交已配平仓位，跳过 FAK",
                                    m.question[:45], cid, unpaired)
                                return
                            size = abs(unpaired)
                            if self.paper:
                                episode_filled = self.broker.taker_buy(
                                    m, token_id, size, price)
                            else:
                                audit_context = {
                                    "path": f"recovery_{quote.path}",
                                    "unpaired_cost": basis,
                                    "expected_loss": quote.expected_loss_usd,
                                }
                                episode_filled = await asyncio.to_thread(
                                    self.broker.taker_buy, m, token_id,
                                    size, price, audit_context)

                            if episode_filled > 0:
                                fee_rate = (m.fee_bps / 10_000.0
                                            * (price * (1.0 - price))
                                            ** m.fee_exponent)
                                if quote.path == "buy_complement":
                                    actual_loss = (
                                        episode_filled
                                        * max(0.0, (basis or 0.0) + price
                                              + fee_rate - 1.0))
                                else:
                                    actual_loss = (
                                        episode_filled
                                        * max(0.0, (basis or 0.0) - price
                                              + fee_rate))
                                label = ("YES" if token_id == m.yes_token
                                         else "NO")
                                log.warning(
                                    "RECOVERY_EXECUTED market='%s' path=%s "
                                    "token=%s size=%.0f price=%.3f "
                                    "expected_loss_usd=%s filled=%.6f "
                                    "actual_loss_usd=%.4f stage=%s "
                                    "说明=恢复策略执行（episode 仅在被检"
                                    "测为平仓时关闭以免 partial fill 重开）",
                                    m.question[:45], quote.path, label,
                                    size, price,
                                    "unknown"
                                    if quote.expected_loss_usd is None
                                    else f"{quote.expected_loss_usd:.4f}",
                                    episode_filled, actual_loss, stage,
                                )
                                if self.metrics:
                                    self.metrics.record_recovery_event(
                                        cid,
                                        f"recovery_{quote.path}_filled",
                                        unpaired,
                                        recovery_path=quote.path,
                                        quote_price=price,
                                        proposed_price=price,
                                        cost_basis=basis,
                                        expected_pair_pnl=(
                                            -(quote.expected_loss_usd or 0.0)
                                            / size if size > 0 else None),
                                        complement_ask=complement_ask,
                                        original_bid=original_bid,
                                        mkt_fee_bps=m.fee_bps,
                                        mkt_fee_exponent=m.fee_exponent,
                                    )
                                    # P2: accumulate actual_loss across
                                    # partial fills via the locked method
                                    # so concurrent writes (e.g. userfeed
                                    # callbacks) don't race.
                                    self.metrics.add_actual_loss(
                                        cid, actual_loss)
                                    self.metrics.update_recovery_episode(
                                        cid=cid,
                                        peak_abs_exposure_usd=abs(exposure),
                                        stage=stage,
                                        chosen_path=quote.path,
                                        expected_loss_usd=quote.expected_loss_usd,
                                    )
                                    # P1.3.2: cumulative loss ban — when
                                    # the total realised recovery loss across
                                    # all episodes for this market exceeds
                                    # the configured cap, ban it permanently
                                    # (persisted to banned_markets.json).
                                    ban_thresh = float(
                                        r.get("recovery_loss_ban_threshold_usd",
                                              1e9))
                                    cum_loss = (
                                        self.metrics.recovery_cumulative_loss(
                                            cid))
                                    if cum_loss > ban_thresh + 1e-9:
                                        self._banned_cids.add(cid)
                                        self._persist_banned_cids()
                                        log.warning(
                                            "RECOVERY_LOSS_BAN market='%s' "
                                            "cid=%s cumulative_loss=%.4f>%.4f "
                                            "说明=该市场累计恢复损失超过"
                                            "阈值，已永久禁入（重启后仍有效）",
                                            m.question[:45], cid,
                                            cum_loss, ban_thresh,
                                        )
                            # P0 new: return — do NOT fall through to old
                            # forced-hedge path.  The FAK already executed;
                            # the old path would re-read stale unpaired and
                            # submit a duplicate complement BUY.
                            return
                    # end active escalated/terminal block
                    # active passive stage returned above (P1-2);
                    # active manual_hold with mode=active returned above (P1-1).
                    # Remaining paths that fall through to old forced-hedge:
                    #   shadow    — all paths (by design: observe-only)
                    #   wait      — all modes (below min shares, safe)
                    #   price=None in active escalated/terminal (fallback)

        # ── existing forced-hedge path (always reached) ──
        excess_yes = unpaired > 0
        token = m.no_token if excess_yes else m.yes_token
        book = self.tracker.books.get(token)
        bid = book.best_bid if book else None
        ask = book.best_ask if book else None
        if ask is None or bid is None or ask - bid > max_spread:
            if passive:
                await self._update_exit_sell(m, unpaired)
            log.warning(
                "FORCED_HEDGE_DEFERRED market='%s' reason=book_unavailable_or_wide "
                "unpaired=%.0f bid=%s ask=%s max_spread=%.3f "
                "说明=强平条件尚未评估；互补订单簿无有效深度或价差过大",
                m.question[:45], unpaired,
                "unknown" if bid is None else f"{bid:.3f}",
                "unknown" if ask is None else f"{ask:.3f}", max_spread)
            if self.metrics:
                self.metrics.record_recovery_event(
                    cid, "forced_hedge_deferred", unpaired,
                    reason="book_unavailable_or_wide", recovery_path="forced_hedge",
                    proposed_price=ask)
            return
        basis_fn = getattr(self.broker, "unpaired_cost_basis", None)
        basis = basis_fn(m) if basis_fn else None
        if not self._forced_hedge_allowed(
                m, urgent=urgent, exposure_usd=exposure, threshold_usd=threshold,
                risk_since=start, now=now, wait_secs=wait, basis=basis, ask=ask):
            escalated = (urgent or abs(exposure) >= threshold
                         or now - start >= wait)
            cap = self._forced_hedge_max_price(m, basis) if basis is not None else None
            if not escalated:
                waited = now - start
                remaining = max(0.0, wait - waited)
                detail = f"等待 {waited:.0f}/{wait:.0f}秒"
            elif cap is not None and ask > cap + 1e-9:
                over = (ask - cap) * 100.0
                detail = f"卖价={ask:.3f} 超保本价{over:.1f}分 pair_cap={cap:.3f}"
            elif basis is None:
                detail = "成本基准未知"
            else:
                detail = "未知"
            fee = (m.fee_bps / 10_000.0 * (ask * (1.0 - ask)) ** m.fee_exponent)
            expected = (1.0 - basis - ask - fee if basis is not None else None)
            reason = ("not_escalated" if not escalated else
                      "over_hard_cap" if cap is not None and ask > cap + 1e-9 else
                      "unknown_cost_basis" if basis is None else "rejected")
            # log.warning(
            #     "FORCED_HEDGE_DEFERRED market='%s' reason=%s unpaired=%.0f "
            #     "urgent=%s exposure=%.4f threshold=%.4f waited=%.0fs/%.0fs "
            #     "bid=%.3f ask=%.3f cost_basis=%s hard_cap=%s fee_per_share=%.6f "
            #     "expected_pair_pnl=%s detail=%s "
            #     "说明=未提交吃单；风险条件或单对经济约束未满足",
            #     m.question[:45], reason, unpaired, urgent, exposure, threshold,
            #     now - start, wait, bid, ask,
            #     "unknown" if basis is None else f"{basis:.3f}",
            #     "unknown" if cap is None else f"{cap:.3f}", fee,
            #     "unknown" if expected is None else f"{expected:+.6f}", detail)
            if self.metrics:
                self.metrics.record_recovery_event(
                    cid, "forced_hedge_deferred", unpaired, reason=reason,
                    recovery_path="forced_hedge", quote_price=ask,
                    proposed_price=ask, cost_basis=basis, fee_per_share=fee,
                    expected_pair_pnl=expected, hard_cap=cap)
            return
        await self._broker_call(self.broker.set_exit, m, None)
        price = self._forced_hedge_max_price(m, basis)
        fee = m.fee_bps / 10_000.0 * (price * (1.0 - price)) ** m.fee_exponent
        expected = 1.0 - basis - price - fee
        log.warning(
            "FORCED_HEDGE_SUBMITTED market='%s' unpaired=%.0f token=%s size=%.0f "
            "limit=%.3f cost_basis=%.3f hard_cap=%.3f fee_per_share=%.6f "
            "expected_pair_pnl=%+.6f 说明=风险触发且成本加手续费不超过$1，提交互补吃单",
            m.question[:45], unpaired, "NO" if excess_yes else "YES", abs(unpaired),
            price, basis, price, fee, expected)
        if self.paper:
            filled = self.broker.taker_buy(m, token, abs(unpaired), price)
        else:
            audit_context = {
                "path": "forced_hedge", "unpaired_cost": basis,
                "pair_cap": price,
                "expected_pair_pnl": 1.0 - basis - price - fee,
            }
            filled = await asyncio.to_thread(
                self.broker.taker_buy, m, token, abs(unpaired), price, audit_context)
        if filled > 0:
            self._over_since.pop(cid, None)
            if hasattr(self.broker, "unpaired_since"):
                self.broker.unpaired_since.pop(cid, None)
            if self.metrics:
                self.metrics.record_recovery_event(
                    cid, "forced_hedge_filled", unpaired,
                    recovery_path="forced_hedge", quote_price=price,
                    proposed_price=price, cost_basis=basis, fee_per_share=fee,
                    expected_pair_pnl=expected, hard_cap=price)
            log.warning(
                "FORCED_HEDGE_FILLED market='%s' filled=%.6f token=%s limit=%.3f "
                "exposure=%.4f expected_pair_pnl=%+.6f "
                "说明=本地已收到成交结果；仍需后续仓位快照确认最终配平",
                m.question[:45], filled, "NO" if excess_yes else "YES", price,
                exposure, expected)

    async def _update_exit_sell(self, m: gamma.Market, unpaired: float) -> None:
        token = m.yes_token if unpaired > 0 else m.no_token
        book = self.tracker.books.get(token)
        mid = book.mid if book else None
        ask = book.best_ask if book else None
        size = float(int(abs(unpaired)))
        if mid is None or ask is None or size < MIN_TAKER_SHARES:
            await self._broker_call(self.broker.set_exit, m, None)
            return
        price = strategy._round_tick(max(ask, mid + m.tick), m.tick)
        price = min(price, strategy._round_tick(1.0 - m.tick, m.tick))
        cur = self.broker.exit_quote(m)
        move = self.cfg["quoting"]["requote_move_cents"]
        if (cur is not None and cur.token_id == token
                and abs(cur.price - price) * 100 < move
                and abs(cur.size - size) <= 0.1 * size):
            return
        await self._broker_call(self.broker.set_exit, m, strategy.Quote(token, price, size))

    def _token_mid(self, token_id: str) -> float | None:
        book = self.tracker.books.get(token_id)
        return book.mid if book else None

    def _fades(self, market: gamma.Market, now: float) -> tuple[float, float]:
        g = self.cfg["guards"]
        window = g["fade_window_minutes"] * 60
        per_fill = g["fade_cents_per_fill"] / 100.0
        cap = g["fade_max_cents"] / 100.0
        yes_n = no_n = 0
        for f in self.broker.fills_log:
            if f.get("taker") or f.get("exit"):
                continue
            if f.get("cid") == market.condition_id and now - f["ts"] <= window:
                if f["side"] == "YES":
                    yes_n += 1
                else:
                    no_n += 1
        return min(yes_n * per_fill, cap), min(no_n * per_fill, cap)

    def _sample_rewards(self) -> None:
        haircut = 1.0
        if self.paper:
            # The estimator only sees displayed competition and assumes full
            # eligibility; discount paper accrual until live data calibrates it.
            haircut = float((self.cfg.get("paper") or {}).get("reward_haircut", 0.7))
        for m in self.markets:
            share = strategy.estimate_reward_share(
                m,
                self.tracker.books[m.yes_token],
                self.tracker.books[m.no_token],
                self.broker.open_quotes(m),
            )
            usd = m.daily_pool * share * haircut / MINUTES_PER_DAY
            self.broker.accrue_rewards(usd)
            self.metrics.record_reward_sample(m.condition_id, usd)

    def _sample_inventory(self, now: float) -> None:
        """Record current per-market inventory facts without affecting decisions."""
        if self.broker is None:
            return
        manual_hold = self._manual_hold_cids()
        managed = {m.condition_id: m for m in self.markets if m.condition_id not in manual_hold}
        for market in self.broker.held_markets():
            if market.condition_id not in manual_hold:
                managed.setdefault(market.condition_id, market)
        basis_fn = getattr(self.broker, "unpaired_cost_basis", None)
        for market in managed.values():
            unpaired = self.broker.unpaired_shares(market)
            basis = basis_fn(market) if basis_fn and abs(unpaired) > 1e-9 else None
            self.metrics.record_inventory_snapshot(
                market.condition_id, market.question[:50],
                unpaired_shares=unpaired,
                cost_basis=basis,
                exposure_usd=self.broker.net_yes_exposure_usd(market),
                status="unpaired" if abs(unpaired) > 1e-9 else "flat",
                ts=now,
                yes_mid=self.tracker.books[market.yes_token].mid if market.yes_token in self.tracker.books else None,
                position_shares=getattr(self.broker, "position_shares", lambda m: (0.0, 0.0))(market),
                book_updated_ts=self.tracker.books[market.yes_token].updated_ts if market.yes_token in self.tracker.books else None,
            )

    def _print_status(self) -> None:
        table = Table(title=f"pmbot — {'PAPER' if self.paper else 'LIVE'}")
        for col in ("Market", "Mid", "Our bid YES", "Our bid NO", "未报价原因", "Net exposure"):
            table.add_column(col)
        now = time.time()
        for m in self.markets:
            book = self.tracker.books[m.yes_token]
            quotes = {q.token_id: q for q in self.broker.open_quotes(m)}
            yq, nq = quotes.get(m.yes_token), quotes.get(m.no_token)
            reason = "—"
            if not yq or not nq:
                blocked = []
                for label, token in (("YES", m.yes_token), ("NO", m.no_token)):
                    remaining = self.guards.side_block_remaining(token, now)
                    if remaining > 0:
                        blocked.append(
                            f"{label} 买单：单边流量失衡，暂停 {math.ceil(remaining / 60):.0f} 分钟")
                reason = "；".join(blocked) or self._quote_block_reasons.get(
                    m.condition_id, "等待本轮报价诊断")
            table.add_row(
                m.question[:45],
                f"{book.mid:.3f}" if book.mid else "—",
                f"{yq.price:.3f} × {yq.size:.0f}" if yq else "—",
                f"{nq.price:.3f} × {nq.size:.0f}" if nq else "—",
                reason,
                f"${self.broker.net_yes_exposure_usd(m):+.2f}",
            )
        console.print(table)
        stats = self.markouts.session_stats()
        if any(n for _, (_, n) in stats.items()):
            console.print("markouts: " + "  ".join(
                f"{avg:+.2f}c @{int(h)}s (n={n})"
                for h, (avg, n) in sorted(stats.items()) if n))
        uptime = self.metrics.session_uptime_pct()
        if uptime > 0:
            console.print(f"in-band uptime: {uptime:.1f}%")
        if self.controller.enabled:
            console.print(self.controller.status_line())
        rewards = self.metrics.reward_totals()
        ledger = self.metrics.trading_pnl_ledger()
        console.print(
            f"rewards realized ${rewards['realized_total']:+.2f} total / "
            f"${rewards['realized_24h']:+.2f} 24h   "
            f"trading P&L (ledger) ${ledger['mtm_total']:+.2f} mtm / "
            f"${ledger['realized_24h']:+.2f} 24h"
        )
        rate = self.metrics.reward_rate_recent(60)
        if rate["minutes"] > 0:
            console.print(
                f"est reward rate ${rate['usd_per_hr']:.3f}/hr "
                f"({rate['minutes']}m sampled, ${rate['usd']:.4f})"
            )
        eq = self.broker.equity()
        if eq != eq:
            return
        if self.paper:
            st = self.broker.state
            console.print(
                f"equity ${eq:.2f}  (cash ${st.cash:.2f}, est. rewards ${st.est_rewards:.4f}, "
                f"fills {sum(p.fills for p in st.positions.values())}, "
                f"PnL ${eq - st.start_equity:+.2f})"
            )
        else:
            console.print(
                f"equity ${eq:.2f}  (unpaired inventory ${self.broker.total_inventory_usd():.2f}, "
                f"day PnL ${eq - self.risk.day_start_equity:+.2f}, sizing ×{self._scale:.2f})"
            )


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="pmbot")
    sub = parser.add_subparsers(dest="command", required=True)

    for name in ("scan", "run", "report", "trades", "performance", "quote-risk-report", "reward-calibration",
                 "recovery-history", "recovery-episodes", "recovery-replay", "outcomes"):
        sub.add_parser(name)

    report_p = sub.choices["report"]
    report_p.add_argument("--date", default=None,
                          help="UTC date YYYY-MM-DD (default: today)")
    trades_p = sub.choices["trades"]
    trades_p.add_argument("--limit", type=int, default=50,
                          help="max fills to show (default 50)")
    trades_p.add_argument("--hours", type=float, default=None,
                          help="only fills from the last N hours")
    trades_p.add_argument("--csv", dest="export_csv", default=None,
                          help="export fills to CSV instead of printing")

    perf_p = sub.choices["performance"]
    perf_p.add_argument("--date", default=None,
                        help="UTC date YYYY-MM-DD (default: today)")
    quote_risk_p = sub.choices["quote-risk-report"]
    quote_risk_p.add_argument("--date", default=None,
                              help="UTC date YYYY-MM-DD (default: today)")
    calibration_p = sub.choices["reward-calibration"]
    calibration_p.add_argument("--days", type=int, default=7,
                               help="number of UTC days to inspect (default: 7)")
    recovery_p = sub.choices["recovery-history"]
    recovery_p.add_argument("condition_id", help="condition id to inspect")

    episodes_p = sub.choices["recovery-episodes"]
    episodes_p.add_argument("--limit", type=int, default=50,
                           help="max episodes to show (default 50)")

    outcomes_p = sub.choices["outcomes"]
    outcomes_p.add_argument("--date", default=None,
                           help="UTC date YYYY-MM-DD (default: today)")
    outcomes_p.add_argument("--days", type=int, default=1,
                           help="number of UTC days to span (default: 1)")

    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    cfg = load_config(args.config)

    # Paper mode uses a separate log directory so simulated runs don't mix with live.
    log_dir = "logs"
    if cfg.get("mode") == "paper":
        log_dir = "logs_paper"

    configure_logging(log_dir)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("py_clob_client_v2.http_helpers.helpers").setLevel(logging.WARNING)
    if args.command == "scan":
        cmd_scan(cfg)
    elif args.command == "report":
        cmd_report(cfg, args.date)
    elif args.command == "trades":
        cmd_trades(cfg, args.limit, args.hours, args.export_csv)
    elif args.command == "performance":
        cmd_performance(cfg, args.date)
    elif args.command == "quote-risk-report":
        cmd_quote_risk_report(cfg, args.date)
    elif args.command == "reward-calibration":
        cmd_reward_calibration(cfg, args.days)
    elif args.command == "recovery-history":
        cmd_recovery_history(cfg, args.condition_id)
    elif args.command == "recovery-episodes":
        cmd_recovery_episodes(cfg, args.limit)
    elif args.command == "recovery-replay":
        cmd_recovery_replay(cfg)
    elif args.command == "outcomes":
        cmd_outcomes(cfg, args.date, args.days)
    else:
        if cfg["mode"] == "live":
            console.print("[bold red]LIVE mode — real orders will be placed. Ctrl-C cancels all and exits.[/]")
        try:
            asyncio.run(Bot(cfg).run())
        except KeyboardInterrupt:
            console.print("stopped — all orders cancelled.")


if __name__ == "__main__":
    main()
