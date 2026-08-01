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
from datetime import date, datetime, timedelta, timezone
from logging.handlers import QueueHandler, QueueListener
from pathlib import Path

import yaml
from dotenv import load_dotenv
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from . import gamma, strategy
from .books import Book, BookTracker
from .brokers import LiveBroker, PaperBroker
from .controller import AdaptiveController
from .metrics import MetricsStore
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
        log.warning("DingTalk is enabled but DINGTALK_WEBHOOK_URL is unset")
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


def _metrics_store(cfg: dict) -> MetricsStore:
    m = cfg.get("metrics") or {}
    db_path = m.get("db_path", "data/metrics.db")
    # Paper mode uses a separate DB so simulated data doesn't mix with live.
    if cfg.get("mode") == "paper" and "db_path" not in m:
        db_path = "data/metrics_paper.db"
    return MetricsStore(db_path,
                        trades_log=m.get("trades_log"),
                        inception_date=m.get("inception_date"))


def cmd_report(cfg: dict) -> None:
    store = _metrics_store(cfg)
    report = store.daily_report()
    rewards = store.reward_totals()
    ledger = store.trading_pnl_ledger()
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
    score.add_column("Last 24h", justify="right")
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
        f"${ledger['mtm_total']:+.2f} mark-to-market. Realized 24h reads low while "
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
    for col in ("Time (UTC)", "Market", "Type", "Side", "Price", "Size", "Merged", "Fee"):
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
            fill["market"][:40],
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
    table = Table(title=f"Per-market performance — {report['date']}")
    for col in ("Market", "Maker", "Taker", "Exit", "Buy $", "Merge $",
                "Trading", "Est Rwd", "Est Net", "Hedge $", "Fees", "Markout",
                "Uptime", "Recovery / Evidence"):
        table.add_column(col)
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
            (m["market"] or m["cid"][:12])[:40],
            str(m["maker_fills"]),
            str(m["taker_fills"]),
            str(m["exits"]),
            f"${m['buy_cost_usd']:.2f}",
            f"${m['merge_proceeds_usd']:.2f}",
            f"${m['trading_pnl_usd']:+.2f}",
            f"${m['est_rewards_usd']:+.2f}",
            f"${m['net_pnl_est_usd']:+.2f}",
            f"${m['hedge_cost_usd']:.2f}",
            f"${m['fees_usd']:.2f}",
            markout,
            f"{m['uptime_pct']:.0f}%",
            reco,
        )
    console.print(table)
    console.print("[bold]Condition IDs（可直接复制给 recovery-history）[/]")
    for m in markets:
        console.print(f"  {m['cid']}  {m['market'] or '—'}")
    console.print(
        "\n[dim]Est Net = trading cashflow + estimated reward; it excludes "
        "market-level realized rewards and inventory MTM. Account rewards are not "
        "allocated to markets without an explicit market-level source. A terminal status needs "
        "a flat snapshot plus enough observed merge/exit/hedge quantity; otherwise "
        "it remains unresolved. Carry means today's closing cashflow may include "
        "pre-day inventory, so it is not attributed to today's selection.[/]"
    )


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
        self._last_rotate = 0.0
        self._rotate_pending = False
        self._last_reward_sample = 0.0
        self._last_inventory_sample = 0.0
        self._last_status = 0.0
        self._last_pos_refresh = 0.0
        self._last_merge_check = 0.0
        self._last_realized_reward = 0.0
        self._merge_task: asyncio.Task | None = None
        self._over_since: dict[str, float] = {}
        self._last_flatten: dict[str, float] = {}
        # P1.3: markout-trip banned markets — 持久化到 data/banned_markets.json
        self._banned_cids: set[str] = set()
        self._banned_path = (
            Path((cfg.get("metrics") or {}).get("db_path", "data/metrics.db")).parent
            / "banned_markets.json"
        )
        self._load_banned_cids()
        self._recovery_skip_logged_at: dict[str, float] = {}
        self._recovery_phase_logged: dict[str, str] = {}
        self._recovery_pricing: dict[str, dict[str, float | str]] = {}
        self._scale = 1.0
        self._was_paused = False
        self._pause_day_active = False
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
            await self._manage_inventory(time.time())
            log.warning("scanner found no eligible markets — retrying in %.0fs "
                        "(loosen config filters to match more markets)",
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
                        await self._broker_call(self.broker.cancel_quotes)
                        self._was_paused = True
                    for m in self.markets:
                        self.metrics.sample_uptime(m.condition_id, False)
                    await self._manage_inventory(now)
                    continue

                self._was_paused = False
                await self._quote_all()
                await self._manage_inventory(now)

                if now - self._last_reward_sample >= REWARD_SAMPLE_SECONDS:
                    self._sample_rewards()
                    self._last_reward_sample = now
                if now - self._last_status >= STATUS_SECONDS:
                    self._print_status()
                    self._last_status = now
        finally:
            log.info("shutting down — cancelling all orders")
            for task in list(self._pull_tasks):
                task.cancel()
            if self._pull_tasks:
                await asyncio.gather(*self._pull_tasks, return_exceptions=True)
            if self.userfeed:
                await self.userfeed.stop()
            if self._merge_task and not self._merge_task.done():
                with contextlib.suppress(asyncio.CancelledError):
                    await self._merge_task
            await self._broker_call(self.broker.cancel_all)
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
        self.userfeed.start()
        self.broker.metrics = self.metrics
        self.tracker.on_trade(self._on_market_trade)
        await self.tracker.start()
        await self._ensure_held_market_books()

    async def _ensure_held_market_books(self) -> None:
        """Subscribe position-only markets before routing them to inventory logic."""
        if self.paper or self.broker is None or self.tracker is None:
            return
        held = self.broker.held_markets()
        for market in held:
            self._token_market[market.yes_token] = market
            self._token_market[market.no_token] = market
        missing = [token for token in self.broker.position_tokens()
                   if token not in self.tracker.books]
        if not missing:
            return
        log.warning("subscribing %d held-position token(s) for inventory management",
                    len(missing))
        await self.tracker.resubscribe([*self.tracker.books, *missing])

    async def _broker_call(self, fn, *args):
        """Dispatch broker order ops off the event loop in live mode."""
        if self.paper:
            return fn(*args)
        return await asyncio.to_thread(fn, *args)

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
            log.warning("guard trip — pulling quotes from '%s' now", m.question[:45])
            self.metrics.record_guard_event(
                cid, "market", "market_guard_pull")
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
            log.warning("side block — pulling %s bid in '%s' now",
                        "YES" if token_id == m.yes_token else "NO",
                        m.question[:45])
            self.metrics.record_guard_event(
                m.condition_id, "side", "side_guard_pull")
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
        locked = list(locked or [])
        # A small, still-manageable residual cannot earn rewards until it reaches
        # this market's rewardsMinSize. Keep it on the recovery path without
        # spending a normal scan slot; once it reaches min_size it occupies one.
        ranked_cids = {m.condition_id for m in ranked}
        # A locked market that is still in ranked would survive via sticky
        # selection on its own score — it doesn't need to occupy a slot.
        # Only markets that depend SOLELY on inventory to stay (not in
        # ranked) count as occupying.
        occupying = [
            m for m in locked
            if (abs(self.broker.unpaired_shares(m)) >= m.min_size
                and m.condition_id not in ranked_cids)
        ][:top_n]
        occupying_cids = {m.condition_id for m in occupying}
        free_locked = [m for m in locked if m.condition_id not in occupying_cids]
        locked_cids = occupying_cids | {m.condition_id for m in free_locked}
        slots = top_n - len(occupying)
        if slots <= 0:
            return occupying + free_locked
        if not bool(sc.get("sticky_swap", True)):
            return (occupying + free_locked
                    + [m for m in ranked if m.condition_id not in locked_cids][:slots])
        margin = float(sc.get("swap_score_margin", 0.0))
        by_cid = {m.condition_id: m for m in ranked}
        held = [m.condition_id for m in self.markets if m.condition_id not in locked_cids]
        # Currently-quoted markets still eligible this scan, freshest score first.
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
                weak = min(displaceable, key=lambda m: m.score)
                if cand.score < weak.score * (1.0 + margin):
                    break  # sorted desc — nothing further clears the margin
                chosen.remove(weak)
                chosen.append(cand)
                chosen_cids = (chosen_cids - {weak.condition_id}) | {cand.condition_id}
                survivor_cids.discard(weak.condition_id)
        return occupying + free_locked + chosen

    async def _rescan(self, initial: bool = False, rotate: bool = False) -> None:
        self._rotate_pending = False
        if rotate:
            self._last_rotate = time.time()
        exclude = set() if initial else self._rotatable_tripped_cids()
        # P1.3: 把 markout-ban 的 cid 也排除，确保 banned 市场不会被重新扫入。
        exclude |= self._banned_cids
        log.info("scanning for reward markets…%s",
                 f" (rotating out {len(exclude)} tripped)" if exclude else "")
        ranked = await asyncio.to_thread(gamma.scan, self.cfg, exclude, True)
        if not ranked:
            if not initial:
                log.warning("rescan found no markets; keeping current set")
            self._last_scan = time.time()
            return
        # P2.1: calculate and persist a passive net-economic ranking.  This is
        # intentionally after gamma's legacy scan: neither this calculation nor
        # a failed SQLite write may influence eligibility or market selection.
        shadow_cfg = (self.cfg.get("scanner") or {}).get("net_shadow") or {}
        lookback_hours = float(shadow_cfg.get("lookback_hours", 24.0))
        try:
            inputs_by_cid = self.metrics.net_shadow_inputs(lookback_hours)
            for market in ranked:
                market.net_shadow_score, market.net_shadow_inputs = (
                    strategy.compute_net_shadow_score(
                        market, inputs_by_cid.get(market.condition_id, {}), self.cfg))
            self.metrics.record_net_shadow_snapshot(
                ranked, time.time(), {"top_n": self.cfg["scanner"]["top_n_markets"],
                                      "net_shadow": shadow_cfg})
        except Exception as exc:  # noqa: BLE001 - observation must fail open
            log.warning("net shadow scan recording failed; legacy selection unchanged: %s", exc)
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
                    log.warning("rescan found only cooled-down markets; "
                                "keeping current set")
                self._last_scan = time.time()
                return
        locked = self._locked_inventory_markets(self.markets)
        markets = self._select_markets(ranked, locked)

        old_markets = list(self.markets)
        new_cids = {m.condition_id for m in markets}
        old_cids = {m.condition_id for m in old_markets}
        new_tokens = {t for m in markets for t in (m.yes_token, m.no_token)}
        old_tokens = {t for m in old_markets for t in (m.yes_token, m.no_token)}
        set_changed = new_cids != old_cids

        for m in markets:
            log.info("quoting: %s  (pool $%.0f/day, score %.3f)",
                     m.question[:60], m.daily_pool, m.score)

        self.markets = markets
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
                if old_m.condition_id in old_cids - new_cids:
                    async with self._market_lock(old_m.condition_id):
                        if hasattr(self.broker, "cancel_quotes_for_market"):
                            await self._broker_call(
                                self.broker.cancel_quotes_for_market, old_m)
                        else:
                            await self._broker_call(
                                self.broker.set_quotes, old_m, [])

        token_ids = list(new_tokens)
        if self.tracker:
            carry_books = {
                t: self.tracker.books[t]
                for t in new_tokens & old_tokens
                if t in self.tracker.books
            }
            if self.broker:
                for t in self.broker.position_tokens():
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
            proposed_price: float | None = None,
            soft_expected_pair_pnl: float | None = None) -> None:
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
            "soft_expected_pair_pnl=%s "
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
            "not_applicable" if soft_expected_pair_pnl is None
            else f"{soft_expected_pair_pnl:+.6f}",
            yes_book.best_bid, yes_book.bids.get(yes_book.best_bid, 0.0),
            yes_book.best_ask, yes_book.asks.get(yes_book.best_ask, 0.0),
            no_book.best_bid, no_book.bids.get(no_book.best_bid, 0.0),
            no_book.best_ask, no_book.asks.get(no_book.best_ask, 0.0),
            pricing["yes_microprice"], pricing["flow_imbalance"], pricing["flow_drift"],
            pricing["fair"], pricing["base_offset"], pricing["adaptive_offset"],
            pricing["offset"], pricing["skew"], pricing["fade_yes"], pricing["fade_no"],
            pricing["yes_bid_quote"], pricing["no_bid_quote"],
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
        """Keep only a capped complement bid while a market is unpaired.

        The strict fee-inclusive break-even cap always limits the passive
        bid.  ``recovery_max_loss_cents`` remains an audit-only estimate of
        the fill probability trade-off; it must never silently raise a quote.
        """
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
                                      unpaired: float, now: float) -> list[strategy.Quote]:
        """Keep a pair-capped recovery bid even if its normal quote side is paused."""
        if abs(unpaired) >= MIN_TAKER_SHARES:
            return desired
        return [q for q in desired if self.guards.allow_side(q.token_id, now)]

    def _cooldown_recovery_quotes(self, m: gamma.Market,
                                   desired: list[strategy.Quote],
                                   unpaired: float,
                                   now: float | None = None) -> list[strategy.Quote]:
        """Keep only risk-reducing complementary bids during market cooldown.

        Cooldown markets also escalate after the window, same as held-only."""
        if abs(unpaired) < MIN_TAKER_SHARES:
            return []
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
            return []
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
            return []
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

        selected_cids = {m.condition_id for m in self.markets}
        for m in all_markets:
            unpaired = self.broker.unpaired_shares(m)
            needs_recovery = abs(unpaired) >= MIN_TAKER_SHARES
            h = hours_to_end(m, now)
            if h is not None and h <= exit_h:
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
            quote_cfg = (self._quote_cfg_for_inventory_recovery(unpaired)
                         if needs_recovery else self.cfg)
            desired = strategy.compute_quotes(
                m, yes_book, exposure, quote_cfg, eff_max_inv,
                fade_yes=fade_yes + widen + flow_yes,
                fade_no=fade_no + widen + flow_no,
                scale=self._scale,
                flow_imbalance=flow_imb,
                markout_avg=markout_avg,
                size_factor=size_factor,
                pricing=pricing,
                min_quote_size=abs(unpaired) if needs_recovery else None,
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
                desired = self._inventory_recovery_quotes(m, desired, unpaired, now)
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
                    desired, unpaired=unpaired, now=now)
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
            current = self.broker.open_quotes(m)
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
                        soft_expected = None
                        if basis is not None and cap is not None:
                            risk_cfg = self.cfg.get("risk") or {}
                            soft_window = float(risk_cfg.get(
                                "recovery_soft_window_minutes", 0)) * 60.0
                            soft_loss = float(risk_cfg.get(
                                "recovery_max_loss_cents", 0)) / 100.0
                            last_fill = self.broker.last_fill_ts(m.condition_id)
                            if (soft_window > 0 and soft_loss > 0 and last_fill is not None
                                    and now - last_fill <= soft_window):
                                soft_price = min(1.0 - m.tick, cap + soft_loss)
                                soft_fee = m.fee_bps / 10_000.0 * (
                                    soft_price * (1.0 - soft_price)) ** m.fee_exponent
                                soft_expected = 1.0 - basis - soft_price - soft_fee
                        self._log_inventory_recovery_quote(
                            m, unpaired=unpaired, quote=recovery_quote,
                            yes_book=yes_book, no_book=no_book, pricing=pricing,
                            pair_cap=cap, hard_cap=cap, cost_basis=basis,
                            proposed_price=proposed_price,
                            soft_expected_pair_pnl=soft_expected)
                        if self.metrics:
                            self.metrics.record_recovery_event(
                                m.condition_id, "quote_placed", unpaired,
                                recovery_path=recovery_path,
                                quote_price=recovery_quote.price,
                                pair_cap=cap, proposed_price=proposed_price,
                                cost_basis=basis, fee_per_share=fee,
                                expected_pair_pnl=expected,
                                soft_expected_pair_pnl=soft_expected, hard_cap=cap)
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
                updates.append((m, final, audit_context))

        if updates:
            await asyncio.gather(
                *(self._set_quotes_locked(m, q, audit) for m, q, audit in updates))

    async def _manage_inventory(self, now: float) -> None:
        quoted = {m.condition_id for m in self.markets}
        managed = {m.condition_id: m for m in self.markets}
        for m in self.broker.held_markets():
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
        if not urgent and abs(exposure) >= threshold and passive and cid in quoted:
            await self._update_exit_sell(m, unpaired)
        if now - self._last_flatten.get(cid, 0.0) < FLATTEN_RETRY_SECONDS:
            return
        self._last_flatten[cid] = now
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
            log.warning(
                "FORCED_HEDGE_DEFERRED market='%s' reason=%s unpaired=%.0f "
                "urgent=%s exposure=%.4f threshold=%.4f waited=%.0fs/%.0fs "
                "bid=%.3f ask=%.3f cost_basis=%s hard_cap=%s fee_per_share=%.6f "
                "expected_pair_pnl=%s detail=%s "
                "说明=未提交吃单；风险条件或单对经济约束未满足",
                m.question[:45], reason, unpaired, urgent, exposure, threshold,
                now - start, wait, bid, ask,
                "unknown" if basis is None else f"{basis:.3f}",
                "unknown" if cap is None else f"{cap:.3f}", fee,
                "unknown" if expected is None else f"{expected:+.6f}", detail)
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
        managed = {m.condition_id: m for m in self.markets}
        for market in self.broker.held_markets():
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
            )

    def _print_status(self) -> None:
        table = Table(title=f"pmbot — {'PAPER' if self.paper else 'LIVE'}")
        for col in ("Market", "Mid", "Our bid YES", "Our bid NO", "Net exposure"):
            table.add_column(col)
        for m in self.markets:
            book = self.tracker.books[m.yes_token]
            quotes = {q.token_id: q for q in self.broker.open_quotes(m)}
            yq, nq = quotes.get(m.yes_token), quotes.get(m.no_token)
            table.add_row(
                m.question[:45],
                f"{book.mid:.3f}" if book.mid else "—",
                f"{yq.price:.3f} × {yq.size:.0f}" if yq else "—",
                f"{nq.price:.3f} × {nq.size:.0f}" if nq else "—",
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

    for name in ("scan", "run", "report", "trades", "performance", "reward-calibration",
                 "recovery-history"):
        sub.add_parser(name)

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
    calibration_p = sub.choices["reward-calibration"]
    calibration_p.add_argument("--days", type=int, default=7,
                               help="number of UTC days to inspect (default: 7)")
    recovery_p = sub.choices["recovery-history"]
    recovery_p.add_argument("condition_id", help="condition id to inspect")

    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    cfg = load_config(args.config)

    # Paper mode uses a separate log directory so simulated runs don't mix with live.
    log_dir = "logs"
    if cfg.get("mode") == "paper":
        log_dir = "logs_paper"

    configure_logging(log_dir)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if args.command == "scan":
        cmd_scan(cfg)
    elif args.command == "report":
        cmd_report(cfg)
    elif args.command == "trades":
        cmd_trades(cfg, args.limit, args.hours, args.export_csv)
    elif args.command == "performance":
        cmd_performance(cfg, args.date)
    elif args.command == "reward-calibration":
        cmd_reward_calibration(cfg, args.days)
    elif args.command == "recovery-history":
        cmd_recovery_history(cfg, args.condition_id)
    else:
        if cfg["mode"] == "live":
            console.print("[bold red]LIVE mode — real orders will be placed. Ctrl-C cancels all and exits.[/]")
        try:
            asyncio.run(Bot(cfg).run())
        except KeyboardInterrupt:
            console.print("stopped — all orders cancelled.")


if __name__ == "__main__":
    main()
