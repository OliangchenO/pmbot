#!/usr/bin/env python3
"""PMBot health monitor — log analysis + database metrics in one report.

Runs from a scheduled task; updates PROGRESS.md with a timestamped report.
"""

import datetime
import json
import os
import re
import sqlite3
import subprocess
import sys
from collections import Counter
from pathlib import Path

BEIJING_TZ = datetime.timezone(datetime.timedelta(hours=8))
ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "logs"
DB_PATH = ROOT / "data" / "metrics.db"
BANNED_PATH = ROOT / "data" / "banned_markets.json"
PROGRESS_PATH = ROOT / "PROGRESS.md"


def _hdr(text: str) -> str:
    return f"\n{'='*60}\n  {text}\n{'='*60}"


def check_logs(minutes: int = 30) -> dict:
    """Scan today's log file for issues in the last N minutes."""
    now = datetime.datetime.now(BEIJING_TZ)
    log_file = LOG_DIR / f"pmbot.{now:%Y-%m-%d}.log"
    result = {
        "log_file": str(log_file),
        "exists": log_file.exists(),
        "total_lines": 0,
        "recent_lines": 0,
        "errors": [],
        "phase_changes": [],
        "hedge_deferrals": {},
        "recovery_skips": [],
        "duplicates": {},
        "order_failures": [],
        "ws_events": [],
        "warnings": [],
    }

    if not log_file.exists():
        return result

    lines = log_file.read_text(encoding="utf-8").splitlines()
    result["total_lines"] = len(lines)

    cutoff = (now - datetime.timedelta(minutes=minutes)).timestamp()
    recent = []
    for line in lines:
        try:
            ts_str = line[:19]
            ts = (
                datetime.datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                .replace(tzinfo=BEIJING_TZ)
                .timestamp()
            )
            if ts >= cutoff:
                recent.append(line)
        except (ValueError, IndexError):
            pass

    result["recent_lines"] = len(recent)

    # ERRORs
    result["errors"] = [l.strip()[:300] for l in recent if "ERROR" in l[:30]]

    # Phase transitions
    result["phase_changes"] = [
        l.strip()[:200] for l in recent if "补单阶段" in l
    ]

    # Hedge deferrals per market
    hedge_markets = Counter()
    for l in recent:
        if "强制对冲推迟" in l:
            m = re.search(r"强制对冲推迟 '([^']+)'", l)
            if m:
                hedge_markets[m.group(1)[:60]] += 1
    result["hedge_deferrals"] = dict(hedge_markets.most_common())

    # Recovery skips
    result["recovery_skips"] = [
        l.strip()[:300] for l in recent if "补单跳过" in l
    ][-5:]

    # Order duplicates
    placed = re.findall(
        r"ORDER_PLACED.*price=([\d.]+).*size=([\d.]+).*side=(\w+)",
        "\n".join(recent),
    )
    placed_counts = Counter(placed)
    cancelled_counts = Counter()
    for line in recent:
        m = re.search(
            r"ORDER_CANCELLED.*price=([\d.]+).*size=([\d.]+).*side=(\w+)", line
        )
        if m:
            cancelled_counts[(m.group(1), m.group(2), m.group(3))] += 1
    dupes = {
        f"{side} x{size} @ {price}": (v, cancelled_counts.get(k, 0))
        for k, v in placed_counts.items()
        if v - cancelled_counts.get(k, 0) > 1
    }
    result["duplicates"] = dupes

    # Order failures
    result["order_failures"] = [
        l.strip()[:300]
        for l in recent
        if any(
            kw in l
            for kw in ("ORDER_POST_FAILED", "ORDER_CANCEL_FAILED", "request error")
        )
    ]

    # WebSocket / feed
    result["ws_events"] = [
        l.strip()[:200]
        for l in recent
        if any(
            kw in l
            for kw in (
                "WebSocket", "websocket", "ws_fill", "subscripti",
                "disconnect", "feed stale", "feed_age",
            )
        )
    ][-5:]

    # Other warnings
    covered = {
        *result["errors"],
        *result["phase_changes"],
        *[l for l in recent if "强制对冲推迟" in l],
        *result["recovery_skips"],
        *result["order_failures"],
        *[l for l in recent if any(kw in l for kw in (
            "WebSocket", "websocket", "ws_fill", "subscripti",
            "disconnect", "feed stale", "feed_age",
        ))],
    }
    result["warnings"] = [
        l.strip()[:300]
        for l in recent
        if "WARNING" in l and l not in covered
    ][-10:]

    return result


def query_db() -> dict:
    """Extract key health metrics from the SQLite database."""
    result: dict = {
        "db_exists": DB_PATH.exists(),
        "latest_equity": None,
        "latest_inventory_usd": None,
        "latest_equity_ts": None,
        "today_fills": 0,
        "today_maker_fills": 0,
        "today_taker_fills": 0,
        "today_trading_pnl": 0.0,
        "today_recovery_loss": 0.0,
        "open_episodes": [],
        "closed_episodes_today": 0,
        "cumulative_loss_by_market": {},
        "banned_markets": [],
        "recent_markouts": [],
        "uptime_pct": 0.0,
        "guard_events_today": 0,
    }

    if not DB_PATH.exists():
        return result

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        now_utc = datetime.datetime.now(datetime.timezone.utc).timestamp()
        today_start = now_utc - (now_utc % 86400)

        # Latest equity
        row = conn.execute(
            "SELECT ts, equity, inventory_usd FROM equity ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        if row:
            result["latest_equity"] = row["equity"]
            result["latest_inventory_usd"] = row["inventory_usd"]
            result["latest_equity_ts"] = row["ts"]

        # Today's fills
        result["today_fills"] = conn.execute(
            "SELECT COUNT(*) FROM fills WHERE ts >= ?", (today_start,)
        ).fetchone()[0]
        result["today_maker_fills"] = conn.execute(
            "SELECT COUNT(*) FROM fills WHERE ts >= ? AND taker=0 AND exit=0",
            (today_start,),
        ).fetchone()[0]
        result["today_taker_fills"] = conn.execute(
            "SELECT COUNT(*) FROM fills WHERE ts >= ? AND taker=1",
            (today_start,),
        ).fetchone()[0]

        # Today's trading PnL — computed from equity change, not raw fills.
        # merges+sells-buys-fees double-counts cross-day positions.
        # Equity snapshots capture true portfolio value. Add back recovery
        # loss because it's a non-trading drag already subtracted from equity.
        eq_rows = list(conn.execute(
            "SELECT equity FROM equity WHERE ts >= ? ORDER BY ts",
            (today_start,),
        ))
        if len(eq_rows) >= 2:
            equity_change = eq_rows[-1]["equity"] - eq_rows[0]["equity"]
        elif len(eq_rows) == 1 and result["latest_equity"] is not None:
            # Only one snapshot today — use it against the latest
            equity_change = result["latest_equity"] - eq_rows[0]["equity"]
        else:
            equity_change = 0.0
        # Recovery loss will be queried next; we need it to back out
        # non-trading drag. Compute it early.
        result["today_recovery_loss"] = (
            conn.execute(
                "SELECT COALESCE(SUM(actual_loss_usd),0) FROM recovery_episodes "
                "WHERE is_closed=1 AND closed_ts >= ?",
                (today_start,),
            ).fetchone()[0]
            or 0.0
        )
        result["today_trading_pnl"] = equity_change + result["today_recovery_loss"]

        # Open recovery episodes
        for row in conn.execute(
            "SELECT cid, started_ts, initial_unpaired, peak_abs_exposure_usd, "
            "stage, chosen_path, expected_loss_usd "
            "FROM recovery_episodes WHERE is_closed=0"
        ):
            result["open_episodes"].append(dict(row))

        # Closed episodes today
        result["closed_episodes_today"] = conn.execute(
            "SELECT COUNT(*) FROM recovery_episodes "
            "WHERE is_closed=1 AND closed_ts >= ?",
            (today_start,),
        ).fetchone()[0]

        # Cumulative loss per market
        for row in conn.execute(
            "SELECT cid, SUM(actual_loss_usd) as total_loss, COUNT(*) as cnt "
            "FROM recovery_episodes WHERE is_closed=1 "
            "GROUP BY cid ORDER BY total_loss DESC"
        ):
            result["cumulative_loss_by_market"][row["cid"]] = {
                "total_loss": row["total_loss"],
                "episode_count": row["cnt"],
            }

        # Recent markouts (last 24h)
        cutoff_24h = now_utc - 86400
        for row in conn.execute(
            "SELECT ts, cid, market, horizon, markout FROM markouts "
            "WHERE ts >= ? ORDER BY ts DESC LIMIT 20",
            (cutoff_24h,),
        ):
            result["recent_markouts"].append(dict(row))

        # Today's uptime
        uptime_rows = conn.execute(
            "SELECT in_band FROM uptime "
            "WHERE minute_ts >= ? AND minute_ts < ?",
            (int(today_start) // 60, int(now_utc) // 60),
        ).fetchall()
        if uptime_rows:
            result["uptime_pct"] = (
                sum(r["in_band"] for r in uptime_rows) / len(uptime_rows) * 100
            )

        # Today's guard events
        result["guard_events_today"] = conn.execute(
            "SELECT COUNT(*) FROM guard_events WHERE ts >= ?",
            (today_start,),
        ).fetchone()[0]

    finally:
        conn.close()

    # Banned markets
    if BANNED_PATH.exists():
        try:
            with open(BANNED_PATH) as f:
                data = json.load(f)
            result["banned_markets"] = data.get("banned_cids", [])
        except (json.JSONDecodeError, OSError):
            pass

    return result


def check_bot_process() -> dict:
    """Check if a pmbot process is currently running.

    Uses ``ps aux`` + grep rather than ``pgrep -f`` so we can match the
    full command line (``python -m pmbot.main run``) even when the
    process table only shows ``python`` as the proc name.
    """
    result = {"running": False, "pids": [], "count": 0}
    try:
        out = subprocess.check_output(
            ["ps", "aux"], text=True, timeout=5,
        )
        for line in out.splitlines():
            if "pmbot.main" in line and "grep" not in line:
                parts = line.split()
                if len(parts) >= 2:
                    try:
                        result["pids"].append(int(parts[1]))
                    except ValueError:
                        pass
        result["count"] = len(result["pids"])
        result["running"] = len(result["pids"]) > 0
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    return result


def format_report(log_result: dict, db_result: dict, proc_result: dict) -> str:
    """Render the health report as terminal output + markdown for PROGRESS.md."""
    now = datetime.datetime.now(BEIJING_TZ)
    lines = []
    a = lines.append

    a(f"# PMBot 健康报告 — {now:%Y-%m-%d %H:%M:%S} 北京时间")
    a("")

    # ── Process status ──
    a(_hdr("进程状态"))
    if proc_result["running"]:
        a(f"  ✅ pmbot.main 运行中 (PID: {', '.join(map(str, proc_result['pids']))})")
    else:
        a("  🔴 pmbot.main 未运行")

    # ── Equity ──
    a(_hdr("资金"))
    if db_result["latest_equity"] is not None:
        eq_ts = datetime.datetime.fromtimestamp(
            db_result["latest_equity_ts"], tz=BEIJING_TZ
        ).strftime("%Y-%m-%d %H:%M:%S")
        a(f"  最新净值:      ${db_result['latest_equity']:,.2f}")
        a(f"  持仓市值:      ${db_result['latest_inventory_usd']:,.2f}")
        a(f"  采样时间:      {eq_ts}")
        a(f"  今日交易盈亏:  ${db_result['today_trading_pnl']:+,.2f}")
        a(f"  今日补单损失:  ${-db_result['today_recovery_loss']:+,.2f}")
    else:
        a("  ⚠️ 无净值数据")

    # ── Trading activity ──
    a(_hdr("今日成交"))
    a(f"  总成交:    {db_result['today_fills']} 笔")
    a(f"  Maker:     {db_result['today_maker_fills']} 笔")
    a(f"  Taker:     {db_result['today_taker_fills']} 笔")
    a(f"  今日守卫触发: {db_result['guard_events_today']} 次")
    a(f"  今日在带时间: {db_result['uptime_pct']:.1f}%")

    # ── Recovery episodes ──
    a(_hdr("补单状态"))
    open_eps = db_result["open_episodes"]
    if open_eps:
        a(f"  进行中的补单: {len(open_eps)} 个")
        for ep in open_eps:
            started = datetime.datetime.fromtimestamp(
                ep["started_ts"], tz=BEIJING_TZ
            ).strftime("%m-%d %H:%M")
            cid_short = ep["cid"][:12] + "..."
            a(f"    {cid_short} | 初始敞口: {ep['initial_unpaired']:+.1f}股"
              f" | 峰值暴露: ${ep['peak_abs_exposure_usd']:.2f}"
              f" | 阶段: {ep['stage']}"
              f" | 路径: {ep['chosen_path'] or '未决策'}"
              f" | 开始: {started}")
    else:
        a("  ✅ 无进行中的补单")

    a(f"  今日关闭补单: {db_result['closed_episodes_today']} 个")

    # ── Cumulative loss / banned ──
    a(_hdr("累计损失与禁入"))
    cum_loss = db_result["cumulative_loss_by_market"]
    if cum_loss:
        for cid, info in cum_loss.items():
            if info["total_loss"] is not None and abs(info["total_loss"]) > 0.01:
                cid_short = cid[:12] + "..."
                flag = " 🚫 已禁入" if cid in db_result["banned_markets"] else ""
                a(f"    {cid_short}: ${info['total_loss']:.2f} "
                  f"({info['episode_count']} 个episode){flag}")
    else:
        a("  ✅ 无补单损失记录")

    banned = db_result["banned_markets"]
    if banned:
        a(f"\n  🚫 禁入市场总数: {len(banned)}")
        for cid in banned:
            cid_short = cid[:16] + "..."
            # Check if still has open episode
            still_open = any(ep["cid"] == cid for ep in open_eps)
            extra = " (仍有进行中补单)" if still_open else ""
            a(f"    {cid_short}{extra}")
    else:
        a("  ✅ 无禁入市场")

    # ── Markouts ──
    a(_hdr("Markout 滑点 (最近 24h)"))
    if db_result["recent_markouts"]:
        for m in db_result["recent_markouts"][:8]:
            ts_str = datetime.datetime.fromtimestamp(m["ts"], tz=BEIJING_TZ).strftime(
                "%m-%d %H:%M"
            )
            mkt_short = (m.get("market") or m["cid"][:20])[:40]
            a(f"    {ts_str} | {mkt_short} | h={m['horizon']}s | {m['markout']:+.3f}¢")
    else:
        a("  ⚠️ 无最近 markout 数据")

    # ── Log analysis ──
    a(_hdr(f"日志分析 (最近 30 分钟)"))
    a(f"  文件: {log_result['log_file']}")
    if log_result["exists"]:
        a(f"  总行数: {log_result['total_lines']}, 最近 30 分钟: {log_result['recent_lines']} 行")

        # Errors
        if log_result["errors"]:
            a(f"\n  🔴 ERROR ({len(log_result['errors'])}):")
            for e in log_result["errors"][-5:]:
                a(f"    {e}")
        else:
            a("  ✅ 无 ERROR")

        # Phase changes
        if log_result["phase_changes"]:
            a(f"\n  📋 补单阶段变化 ({len(log_result['phase_changes'])}):")
            for p in log_result["phase_changes"][-3:]:
                a(f"    {p}")

        # Hedge deferrals
        if log_result["hedge_deferrals"]:
            a(f"\n  📊 强制对冲推迟:")
            for mk, cnt in log_result["hedge_deferrals"].items():
                flag = " ⚠️ >3次" if cnt > 3 else ""
                a(f"    {mk}: {cnt}次{flag}")

        # Recovery skips
        if log_result["recovery_skips"]:
            a(f"\n  ⏭️ 补单跳过 ({len(log_result['recovery_skips'])}):")
            for s in log_result["recovery_skips"]:
                a(f"    {s}")

        # Duplicates
        if log_result["duplicates"]:
            a(f"\n  🔴 重复报价:")
            for desc, (placed_n, cancelled_n) in log_result["duplicates"].items():
                a(f"    {desc} — placed {placed_n}, cancelled {cancelled_n}")
        else:
            a("  ✅ 无重复报价")

        # Order failures
        if log_result["order_failures"]:
            a(f"\n  🔴 订单失败 ({len(log_result['order_failures'])}):")
            for f in log_result["order_failures"][-5:]:
                a(f"    {f}")

        # Warnings
        if log_result["warnings"]:
            a(f"\n  ⚠️ 其他 WARNING ({len(log_result['warnings'])}):")
            for w in log_result["warnings"][-5:]:
                a(f"    {w}")

        # WebSocket
        if log_result["ws_events"]:
            a(f"\n  🔌 WebSocket/Feed ({len(log_result['ws_events'])}):")
            for w in log_result["ws_events"]:
                a(f"    {w}")
    else:
        a(f"  ⚠️ 日志文件不存在")

    # ── Summary status ──
    a(_hdr("综合状态"))
    issues = []

    if not proc_result["running"]:
        issues.append("🔴 pmbot 未运行")
    if log_result["errors"]:
        issues.append(f"🔴 {len(log_result['errors'])} 个 ERROR")
    if log_result["duplicates"]:
        issues.append(f"🔴 重复报价: {len(log_result['duplicates'])} 个")
    if log_result["order_failures"]:
        issues.append(f"🔴 {len(log_result['order_failures'])} 个订单失败")
    hedge_high = {
        k: v for k, v in (log_result.get("hedge_deferrals") or {}).items() if v > 3
    }
    if hedge_high:
        issues.append(f"⚠️ 对冲推迟过多: {len(hedge_high)} 个市场")
    if db_result["today_trading_pnl"] < -5:
        issues.append(f"⚠️ 今日交易亏损: ${db_result['today_trading_pnl']:+,.2f}")
    if db_result["uptime_pct"] < 60:
        issues.append(f"⚠️ 今日在带时间低: {db_result['uptime_pct']:.1f}%")
    bad_markouts = [
        m for m in db_result["recent_markouts"]
        if m["horizon"] == 300 and m["markout"] < -1.0
    ]
    if bad_markouts:
        issues.append(f"⚠️ {len(bad_markouts)} 个 300s markout < -1.0¢")

    if not issues:
        a("  ✅ 一切正常")
    else:
        for i in issues:
            a(f"  {i}")

    a("")
    return "\n".join(lines)


def main():
    print(f"=== PMBot Health Monitor === {datetime.datetime.now(BEIJING_TZ):%Y-%m-%d %H:%M:%S} 北京时间\n")

    # 1. Log analysis
    print("📋 分析日志...")
    log_result = check_logs(minutes=30)

    # 2. Database metrics
    print("📊 查询数据库...")
    db_result = query_db()

    # 3. Process check
    print("🔍 检查进程...")
    proc_result = check_bot_process()

    # 4. Render report
    report = format_report(log_result, db_result, proc_result)

    # 5. Output to terminal
    print(report)

    # 6. Update PROGRESS.md
    try:
        # Write a brief top section + keep existing content below if present
        brief_lines = [
            f"# PMBot 运行状态 — {datetime.datetime.now(BEIJING_TZ):%Y-%m-%d %H:%M:%S} 北京时间\n",
            "",
        ]

        # Key metrics summary
        if proc_result["running"]:
            brief_lines.append(f"- **进程**: ✅ 运行中 (PID: {', '.join(map(str, proc_result['pids']))})")
        else:
            brief_lines.append("- **进程**: 🔴 未运行")

        if db_result["latest_equity"] is not None:
            brief_lines.append(
                f"- **净值**: ${db_result['latest_equity']:,.2f} "
                f"(持仓 ${db_result['latest_inventory_usd']:,.2f})"
            )
            brief_lines.append(
                f"- **今日交易盈亏**: ${db_result['today_trading_pnl']:+,.2f}"
            )
            brief_lines.append(
                f"- **今日补单损失**: ${-db_result['today_recovery_loss']:+,.2f}"
            )

        open_eps = db_result["open_episodes"]
        brief_lines.append(f"- **进行中补单**: {len(open_eps)} 个")

        cum_loss = db_result["cumulative_loss_by_market"]
        if cum_loss:
            total_cum_loss = sum(
                v["total_loss"] for v in cum_loss.values()
                if v["total_loss"] is not None
            )
            brief_lines.append(f"- **累计补单损失**: ${-total_cum_loss:+.2f}")

        brief_lines.append(f"- **今日总成交**: {db_result['today_fills']} 笔")
        brief_lines.append(
            f"- **今日在带时间**: {db_result['uptime_pct']:.1f}%"
        )

        error_count = len(log_result["errors"])
        dup_count = len(log_result["duplicates"])
        fail_count = len(log_result["order_failures"])
        brief_lines.append(
            f"- **日志**: {'🔴' if error_count else '✅'} "
            f"ERROR={error_count}, 重复报价={dup_count}, 订单失败={fail_count}"
        )

        brief_lines.append("")
        brief_lines.append("---")
        brief_lines.append("")
        brief_lines.append("### 最近一次详细报告")
        brief_lines.append("")
        brief_lines.append("```")
        brief_lines.append(report)
        brief_lines.append("```")

        PROGRESS_PATH.write_text("\n".join(brief_lines), encoding="utf-8")
        print(f"\n✅ 已更新 {PROGRESS_PATH}")
    except OSError as e:
        print(f"\n⚠️ 更新 PROGRESS.md 失败: {e}")

    print("\n=== 检查完毕 ===")


if __name__ == "__main__":
    main()
