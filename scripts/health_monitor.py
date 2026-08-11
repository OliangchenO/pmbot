#!/usr/bin/env python3
"""PMBot 健康监控 — 统一检查 + 持久化问题追踪

覆盖维度:
  - P0 Guard 逆向选择信号 (quote_risk_decisions 表)
  - 日志 ERROR / WARNING
  - 补单阶段变化 & 跳过
  - 强制对冲推迟
  - 重复报价
  - 订单失败
  - WebSocket / Feed 异常
  - Controller 参数变化

输出: PROGRESS.md (追加/更新问题条目，保留历史)
"""

import datetime
import hashlib
import json
import os
import re
import sqlite3
import sys
import textwrap
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

# ── paths ────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
DB_PATH = PROJECT_DIR / "data" / "metrics.db"
LOG_DIR = PROJECT_DIR / "logs"
PROGRESS_PATH = PROJECT_DIR / "PROGRESS.md"

BEIJING_TZ = ZoneInfo("Asia/Shanghai")

# ── severity constants ───────────────────────────────────────
SEV_CRITICAL = "🔴 Critical"
SEV_HIGH = "🟠 High"
SEV_MEDIUM = "🟡 Medium"
SEV_LOW = "🔵 Low"

SCAN_WINDOW_MINUTES = 20  # lookback for DB queries and log grep
AUTO_RESOLVE_HOURS = 24     # auto-close issues silent for this long


# ═══════════════════════════════════════════════════════════════
# Data model
# ═══════════════════════════════════════════════════════════════

@dataclass
class Finding:
    """A single discovered issue."""
    group: str          # logical grouping key (stable across runs)
    category: str       # human-readable category
    severity: str       # SEV_*
    detail: str         # one-line description
    extra: str = ""     # multi-line support detail (log excerpt, query result, …)
    count: int = 1      # occurrences within this window


# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════

def now_bj() -> datetime.datetime:
    return datetime.datetime.now(BEIJING_TZ)

def ts_bj(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts, BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")

def fmt_time(dt: datetime.datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")

def issue_id(group: str) -> str:
    """Stable, short ID derived from the group key."""
    return f"PM-{hashlib.md5(group.encode()).hexdigest()[:6].upper()}"

def db_query(sql: str, params=(), retries: int = 3) -> list[dict]:
    for attempt in range(retries):
        try:
            conn = sqlite3.connect(str(DB_PATH))
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute(sql, params)
            rows = [dict(r) for r in cur.fetchall()]
            conn.close()
            return rows
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and attempt < retries - 1:
                time.sleep(2)
            else:
                raise
    return []

def grep_log(pattern: str, minutes: int = SCAN_WINDOW_MINUTES) -> list[str]:
    """Return matching lines from today's log within the last *minutes*."""
    log_file = LOG_DIR / f"pmbot.{now_bj():%Y-%m-%d}.log"
    if not log_file.exists():
        return []
    cutoff = (now_bj() - datetime.timedelta(minutes=minutes)).timestamp()
    lines = log_file.read_text(encoding="utf-8").splitlines()
    matches = []
    for line in lines:
        try:
            ts = datetime.datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S")\
                    .replace(tzinfo=BEIJING_TZ).timestamp()
            if ts >= cutoff and pattern in line:
                matches.append(line.rstrip())
        except (ValueError, IndexError):
            pass
    return matches

def grep_log_regex(pattern: str, minutes: int = SCAN_WINDOW_MINUTES) -> list[str]:
    """Regex version of grep_log."""
    log_file = LOG_DIR / f"pmbot.{now_bj():%Y-%m-%d}.log"
    if not log_file.exists():
        return []
    cutoff = (now_bj() - datetime.timedelta(minutes=minutes)).timestamp()
    lines = log_file.read_text(encoding="utf-8").splitlines()
    matches = []
    pat = re.compile(pattern)
    for line in lines:
        try:
            ts = datetime.datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S")\
                    .replace(tzinfo=BEIJING_TZ).timestamp()
            if ts >= cutoff and pat.search(line):
                matches.append(line.rstrip())
        except (ValueError, IndexError):
            pass
    return matches


# ═══════════════════════════════════════════════════════════════
# Individual check functions → list[Finding]
# ═══════════════════════════════════════════════════════════════

def check_p0_guard() -> list[Finding]:
    """Quote risk decisions: active markets, non-allow signals, anomalies."""
    findings: list[Finding] = []
    cutoff = time.time() - SCAN_WINDOW_MINUTES * 60

    rows = db_query(
        """SELECT cid, COUNT(*) as cnt,
                  AVG(score) as avg_score, MAX(score) as max_score,
                  GROUP_CONCAT(DISTINCT yes_action) as yes_actions,
                  GROUP_CONCAT(DISTINCT no_action) as no_actions,
                  GROUP_CONCAT(DISTINCT mode) as modes
           FROM quote_risk_decisions WHERE ts >= ?
           GROUP BY cid ORDER BY cnt DESC""",
        (cutoff,))

    if not rows:
        findings.append(Finding(
            group="p0-no-markets",
            category="P0 Guard",
            severity=SEV_HIGH,
            detail="无市场处于 P0 监控下（最近 20 分钟无决策记录）",
        ))
        return findings

    for r in rows:
        yes_a = r["yes_actions"] or ""
        no_a = r["no_actions"] or ""
        if yes_a == "allow" and no_a == "allow":
            continue
        # Non-allow decisions exist
        findings.append(Finding(
            group=f"p0-signal-{r['cid'][:20]}",
            category="P0 Guard · 信号变化",
            severity=SEV_MEDIUM,
            detail=f"市场 {r['cid'][:12]}... YES={yes_a} NO={no_a} "
                   f"score={r['avg_score']:.3f} max={r['max_score']:.3f}",
            count=r["cnt"],
        ))

    # Check for excessive widen/pull
    action_stats = db_query(
        """SELECT yes_action, no_action, COUNT(*) as cnt
           FROM quote_risk_decisions WHERE ts >= ?
           GROUP BY yes_action, no_action""",
        (cutoff,))
    widen = pull = skip = 0
    for r in action_stats:
        for field in ("yes_action", "no_action"):
            v = r[field]
            if v == "widen": widen += r["cnt"]
            elif v == "pull":  pull  += r["cnt"]
            elif v == "skip":  skip  += r["cnt"]
    if widen > 10:
        findings.append(Finding(
            group="p0-excessive-widen",
            category="P0 Guard",
            severity=SEV_MEDIUM,
            detail=f"widen 次数偏高: {widen} 次 / {SCAN_WINDOW_MINUTES}min",
        ))
    if pull > 5:
        findings.append(Finding(
            group="p0-excessive-pull",
            category="P0 Guard",
            severity=SEV_HIGH,
            detail=f"pull 次数偏高: {pull} 次 / {SCAN_WINDOW_MINUTES}min",
        ))

    return findings


def check_guard_events() -> list[Finding]:
    """传统 guard 触发事件（guard_events 表）：市场暂停、单边保护等。

    覆盖 P0 quote_risk_decisions 覆盖不到的传统 guard 路径
    （check_flow, record_trade, record_mid 触发的一级保护）。
    """
    findings: list[Finding] = []
    cutoff = time.time() - SCAN_WINDOW_MINUTES * 60

    rows = db_query(
        """SELECT scope, reason, cid, market, COUNT(*) as cnt
           FROM guard_events WHERE ts >= ?
           GROUP BY scope, reason, cid ORDER BY cnt DESC""",
        (cutoff,))

    if not rows:
        return findings

    for r in rows:
        scope = r["scope"] or ""
        reason = r["reason"] or ""
        cid = (r["cid"] or "")[:20]
        market = (r.get("market") or "")[:60]
        cnt = r["cnt"]

        # market_guard_pull / side_guard_pull are the actionable signals
        if reason == "market_guard_pull":
            findings.append(Finding(
                group=f"guard-market-pull-{cid}",
                category="Guard · 市场暂停",
                severity=SEV_HIGH,
                detail=f"市场 {market} 触发全市场暂停（{cnt} 次）",
                count=cnt,
            ))
        elif reason == "side_guard_pull":
            findings.append(Finding(
                group=f"guard-side-pull-{cid}",
                category="Guard · 单边保护",
                severity=SEV_MEDIUM,
                detail=f"市场 {market} 触发单边保护（{cnt} 次）",
                count=cnt,
            ))
        elif reason == "queue_depth_pull":
            findings.append(Finding(
                group=f"guard-queue-depth-{cid}",
                category="Guard · 队列深度",
                severity=SEV_LOW,
                detail=f"市场 {market} 触发队列深度保护（{cnt} 次）",
                count=cnt,
            ))
        else:
            # Catch other/new guard event types
            findings.append(Finding(
                group=f"guard-other-{hashlib.md5(f'{scope}{reason}{cid}'.encode()).hexdigest()[:8]}",
                category="Guard · 其他",
                severity=SEV_LOW,
                detail=f"scope={scope} reason={reason} market={market}（{cnt} 次）",
                count=cnt,
            ))

    return findings


def check_errors() -> list[Finding]:
    """Log ERROR lines."""
    lines = grep_log("ERROR")
    if not lines:
        return []
    # Group by error signature (first 80 chars after timestamp)
    groups: dict[str, list[str]] = {}
    for l in lines:
        sig = l[20:100] if len(l) > 20 else l
        groups.setdefault(sig, []).append(l)

    findings = []
    for sig, matches in groups.items():
        findings.append(Finding(
            group=f"error-{hashlib.md5(sig.encode()).hexdigest()[:8]}",
            category="ERROR",
            severity=SEV_HIGH,
            detail=f"{len(matches)} 条 ERROR: {sig.strip()[:120]}",
            extra="\n".join(matches[-3:]),
            count=len(matches),
        ))
    return findings


def check_recovery_phase() -> list[Finding]:
    """补单阶段变化."""
    lines = grep_log("补单阶段")
    if not lines:
        return []
    # Only flag Phase transitions that look unusual
    phase2 = [l for l in lines if "Phase 2" in l]
    escalate = [l for l in lines if "升级" in l or "escalat" in l.lower()]
    findings = []
    if phase2:
        findings.append(Finding(
            group="recovery-phase2",
            category="补单",
            severity=SEV_MEDIUM,
            detail=f"{len(phase2)} 个市场进入 Phase 2 补单",
            extra="\n".join(phase2[-5:]),
            count=len(phase2),
        ))
    return findings


def check_hedge_deferrals() -> list[Finding]:
    """强制对冲推迟."""
    lines = grep_log("强制对冲推迟")
    if not lines:
        return []
    by_market: dict[str, int] = Counter()
    for l in lines:
        m = re.search(r"强制对冲推迟\s+'([^']+)'", l)
        key = m.group(1)[:60] if m else "unknown"
        by_market[key] += 1

    findings = []
    for mk, cnt in by_market.most_common():
        sev = SEV_HIGH if cnt > 10 else SEV_MEDIUM if cnt > 3 else SEV_LOW
        findings.append(Finding(
            group=f"hedge-defer-{hashlib.md5(mk.encode()).hexdigest()[:8]}",
            category="强制对冲",
            severity=sev,
            detail=f"市场 {mk}: 对冲推迟 {cnt} 次",
            count=cnt,
        ))
    return findings


def check_recovery_skips() -> list[Finding]:
    """补单跳过."""
    lines = grep_log("补单跳过")
    if not lines:
        return []
    # Deduplicate by cid
    by_cid: dict[str, list[str]] = {}
    for l in lines:
        m = re.search(r"cid[=:]\s*'?([\w\d]+)'?", l)
        key = m.group(1)[:20] if m else "unknown"
        by_cid.setdefault(key, []).append(l)

    findings = []
    for cid, matches in by_cid.items():
        findings.append(Finding(
            group=f"recovery-skip-{cid[:12]}",
            category="补单跳过",
            severity=SEV_LOW,
            detail=f"{len(matches)} 次补单跳过 (cid={cid})",
            extra="\n".join(matches[-2:]),
            count=len(matches),
        ))
    return findings


def check_duplicate_orders() -> list[Finding]:
    """重复报价 (placed - cancelled > 1)."""
    log_file = LOG_DIR / f"pmbot.{now_bj():%Y-%m-%d}.log"
    if not log_file.exists():
        return []
    cutoff = (now_bj() - datetime.timedelta(minutes=SCAN_WINDOW_MINUTES)).timestamp()

    lines = log_file.read_text(encoding="utf-8").splitlines()
    recent = []
    for line in lines:
        try:
            ts = datetime.datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S")\
                    .replace(tzinfo=BEIJING_TZ).timestamp()
            if ts >= cutoff:
                recent.append(line)
        except (ValueError, IndexError):
            pass

    text = "\n".join(recent)
    placed = re.findall(r"ORDER_PLACED.*price=([\d.]+).*size=([\d.]+).*side=(\w+)", text)
    placed_counts = Counter(placed)

    cancelled_counts = Counter()
    for line in recent:
        m = re.search(r"ORDER_CANCELLED.*price=([\d.]+).*size=([\d.]+).*side=(\w+)", line)
        if m:
            cancelled_counts[(m.group(1), m.group(2), m.group(3))] += 1

    dupes = {k: (v, cancelled_counts.get(k, 0))
             for k, v in placed_counts.items()
             if v - cancelled_counts.get(k, 0) > 1}
    if not dupes:
        return []
    detail = ", ".join(
        f"{side} x{size} @{price} ({placed_n}p/{cancelled_n}c)"
        for (price, size, side), (placed_n, cancelled_n) in list(dupes.items())[:5])
    return [Finding(
        group="duplicate-orders",
        category="重复报价",
        severity=SEV_HIGH,
        detail=f"{len(dupes)} 组重复报价: {detail}",
        extra="\n".join(
            f"  {side} x{size} @ {price} — placed {placed_n}, cancelled {cancelled_n}"
            for (price, size, side), (placed_n, cancelled_n) in dupes.items()),
        count=len(dupes),
    )]


def check_order_failures() -> list[Finding]:
    """订单失败 (POST/CANCEL/request error)."""
    lines = grep_log_regex(r"ORDER_POST_FAILED|ORDER_CANCEL_FAILED|request error")
    if not lines:
        return []
    return [Finding(
        group="order-failures",
        category="订单失败",
        severity=SEV_HIGH,
        detail=f"{len(lines)} 条订单失败",
        extra="\n".join(lines[-5:]),
        count=len(lines),
    )]


def check_websocket() -> list[Finding]:
    """WebSocket / feed 异常."""
    lines = grep_log_regex(
        r"WebSocket|websocket|ws_fill|disconnect|feed stale|feed_age|subscripti")
    if not lines:
        return []

    # Categorize
    stale = [l for l in lines if "stale" in l or "feed_age" in l]
    disc = [l for l in lines if "disconnect" in l]
    other = [l for l in lines if l not in stale and l not in disc]

    findings = []
    if stale:
        findings.append(Finding(
            group="ws-feed-stale",
            category="WebSocket",
            severity=SEV_MEDIUM,
            detail=f"Feed 落后: {len(stale)} 条",
            extra="\n".join(stale[-3:]),
            count=len(stale),
        ))
    if disc:
        findings.append(Finding(
            group="ws-disconnect",
            category="WebSocket",
            severity=SEV_HIGH,
            detail=f"WebSocket 断开: {len(disc)} 条",
            extra="\n".join(disc[-3:]),
            count=len(disc),
        ))
    return findings


def check_controller() -> list[Finding]:
    """Controller 参数变化."""
    lines = grep_log("controller adjusted")
    if not lines:
        return []
    return [Finding(
        group="controller-adj",
        category="Controller",
        severity=SEV_LOW,
        detail=f"{len(lines)} 次参数调整",
        extra="\n".join(lines[-3:]),
        count=len(lines),
    )]


def check_warnings() -> list[Finding]:
    """Other WARNING lines not covered above."""
    # Get all WARNING lines, then filter out known categories
    covered_patterns = [
        "补单阶段", "强制对冲推迟", "补单跳过",
        "ORDER_POST_FAILED", "ORDER_CANCEL_FAILED", "request error",
        "WebSocket", "websocket", "disconnect", "feed stale",
        "controller adjusted", "P0 逆向选择防护",
        "P0 guard 历史清理", "quote_risk_decision",
        "市场风控触发", "单边流量失衡", "方向性成交流",
        "guard_events",
    ]
    lines = grep_log("WARNING")
    filtered = []
    for l in lines:
        if not any(p in l for p in covered_patterns):
            filtered.append(l)

    if not filtered:
        return []
    return [Finding(
        group="other-warnings",
        category="WARNING",
        severity=SEV_LOW,
        detail=f"{len(filtered)} 条其他 WARNING",
        extra="\n".join(filtered[-5:]),
        count=len(filtered),
    )]


# ═══════════════════════════════════════════════════════════════
# PROGRESS.md 持久化
# ═══════════════════════════════════════════════════════════════

# We embed structured metadata in HTML comments so the script can
# parse it back without disrupting human readability.
# Format:
#   <!-- pmbot-issue: {"id":"PM-XXXXXX","group":"...","status":"open","first_seen":"...","last_seen":"..."} -->
#   ### [PM-XXXXXX] Category · Severity — Detail
#
#   - First seen: ...
#   - Last seen: ...
#   - Count this window: N
#   - Status: open / resolved / wontfix
#
#   <extra detail>
#   ---

def read_progress_issues() -> dict[str, dict]:
    """Parse PROGRESS.md, return {group_key: metadata} for existing issues."""
    if not PROGRESS_PATH.exists():
        return {}
    text = PROGRESS_PATH.read_text(encoding="utf-8")
    issues = {}
    for m in re.finditer(
        r'<!--\s*pmbot-issue:\s*({.*?})\s*-->',
        text, re.DOTALL
    ):
        try:
            meta = json.loads(m.group(1))
            issues[meta["group"]] = meta
        except (json.JSONDecodeError, KeyError):
            pass
    return issues


def write_progress_report(findings: list[Finding]) -> None:
    """Merge findings into PROGRESS.md."""
    existing = read_progress_issues()
    now_dt = now_bj()
    now_str = fmt_time(now_dt)

    # Update existing entries / create new ones
    updated: dict[str, dict] = {}  # group → meta
    kept_findings: dict[str, Finding] = {}  # group → finding

    for f in findings:
        meta = existing.get(f.group, {
            "id": issue_id(f.group),
            "group": f.group,
            "status": "open",
            "first_seen": now_str,
            "severity": f.severity,
            "category": f.category,
        })
        meta["last_seen"] = now_str
        meta["severity"] = f.severity   # may upgrade
        meta["category"] = f.category
        if "count_window" not in meta:
            meta["count_window"] = {}
        meta["count_window"][now_str] = f.count
        # Keep status unless it was manually resolved or auto-expired
        if meta.get("status") in ("resolved", "resolved_inactive"):
            # It reappeared → re-open
            meta["status"] = "open"
            meta["reopened_at"] = now_str
        updated[f.group] = meta
        kept_findings[f.group] = f

    # Also carry over resolved/wontfix issues that didn't reappear.
    # Auto-resolve open issues that have been silent for > AUTO_RESOLVE_HOURS.
    for group, meta in existing.items():
        if group not in updated:
            if meta.get("status") == "open":
                try:
                    last_seen_dt = datetime.datetime.strptime(
                        meta.get("last_seen", ""), "%Y-%m-%d %H:%M:%S"
                    ).replace(tzinfo=BEIJING_TZ)
                    if (now_dt - last_seen_dt).total_seconds() > AUTO_RESOLVE_HOURS * 3600:
                        meta["status"] = "resolved_inactive"
                        meta["resolved_at"] = now_str
                except ValueError:
                    pass
            updated[group] = meta  # keep as-is

    # Build the markdown
    open_issues = [(g, m) for g, m in updated.items() if m.get("status") == "open"]
    closed_issues = [(g, m) for g, m in updated.items() if m.get("status") != "open"]

    lines = []
    lines.append("# PMBot 问题追踪")
    lines.append(f"上次扫描: {now_str}")
    lines.append("")
    lines.append("> 自动生成，手动管理。标记为 `resolved` 或 `wontfix` 的问题不会再被自动重新打开，除非问题复现。")
    lines.append("")

    # ── Open issues ──
    lines.append(f"## 🔴 待处理 ({len(open_issues)})")
    lines.append("")
    if not open_issues:
        lines.append("✅ 无待处理问题。")
        lines.append("")
    else:
        # Sort by severity then first_seen
        sev_order = {SEV_CRITICAL: 0, SEV_HIGH: 1, SEV_MEDIUM: 2, SEV_LOW: 3}
        open_issues.sort(key=lambda x: (sev_order.get(x[1].get("severity", ""), 99),
                                          x[1].get("first_seen", "")))

        for group, meta in open_issues:
            f = kept_findings.get(group)
            lines.append(build_issue_block(meta, f))
            lines.append("")

    # ── Closed issues ──
    lines.append(f"## ✅ 已关闭 / 自动过期 ({len(closed_issues)})")
    lines.append("")
    if not closed_issues:
        lines.append("暂无已关闭问题。")
        lines.append("")
    else:
        for group, meta in sorted(closed_issues, key=lambda x: x[1].get("last_seen", ""),
                                  reverse=True)[:30]:  # keep last 30
            f = kept_findings.get(group)
            lines.append(build_issue_block(meta, f))
            lines.append("")

    PROGRESS_PATH.write_text("\n".join(lines), encoding="utf-8")


def build_issue_block(meta: dict, finding: Optional[Finding]) -> str:
    """Render one issue as Markdown with embedded JSON metadata."""
    blocks = []
    blocks.append(f"<!-- pmbot-issue: {json.dumps(meta, ensure_ascii=False)} -->")
    sev = meta.get("severity", SEV_LOW)
    cat = meta.get("category", "Unknown")
    status = meta.get("status", "open")
    status_icon = {"open": "🔴", "resolved": "✅", "resolved_inactive": "💤", "wontfix": "⚫"}.get(status, "❓")

    blocks.append(f"### [{meta['id']}] {sev} · {cat} {status_icon}")
    blocks.append("")

    if finding:
        blocks.append(f"- **描述**: {finding.detail}")
        blocks.append(f"- **当前窗口**: {finding.count} 次")
        if finding.extra:
            blocks.append(f"- **详情**:")
            blocks.append("  ```")
            for line in finding.extra.splitlines()[-8:]:
                blocks.append(f"  {line[:200]}")
            blocks.append("  ```")
    else:
        blocks.append(f"- **描述**: (最近窗口未出现)")
        blocks.append(f"- **当前窗口**: 0 次")

    blocks.append(f"- **首次发现**: {meta.get('first_seen', '?')}")
    blocks.append(f"- **最近出现**: {meta.get('last_seen', '?')}")
    if meta.get("reopened_at"):
        blocks.append(f"- **重新打开**: {meta['reopened_at']}")
    blocks.append(f"- **状态**: {status}  ← 手动改为 `resolved` 或 `wontfix` 以关闭")
    blocks.append("")
    blocks.append("---")
    return "\n".join(blocks)


# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════

def main() -> None:
    now = now_bj()
    print(f"=== PMBot 健康监控 [{fmt_time(now)}] ===")
    print(f"窗口: {SCAN_WINDOW_MINUTES} 分钟")
    print()

    all_findings: list[Finding] = []

    checks = [
        ("P0 Guard 信号", check_p0_guard),
        ("传统 Guard 事件", check_guard_events),
        ("ERROR", check_errors),
        ("补单阶段", check_recovery_phase),
        ("强制对冲推迟", check_hedge_deferrals),
        ("补单跳过", check_recovery_skips),
        ("重复报价", check_duplicate_orders),
        ("订单失败", check_order_failures),
        ("WebSocket", check_websocket),
        ("Controller", check_controller),
        ("其他 WARNING", check_warnings),
    ]

    for label, func in checks:
        try:
            findings = func()
        except Exception as exc:
            print(f"  ❌ {label}: 检查失败 — {exc}")
            continue
        all_findings.extend(findings)
        if findings:
            cnt = sum(f.count for f in findings)
            print(f"  ⚠️  {label}: {len(findings)} 类问题 ({cnt} 条)")
        else:
            print(f"  ✅ {label}: 正常")

    print()

    if not all_findings:
        print("✅ 全部检查通过，无异常。")
    else:
        total = sum(f.count for f in all_findings)
        print(f"📋 共发现 {len(all_findings)} 类问题 ({total} 条事件)")
        print()
        for f in all_findings:
            icon = f.severity[0]  # first char of emoji
            print(f"  {f.severity}  {f.category}: {f.detail}")
            print(f"     ({f.count} 次, id={issue_id(f.group)})")

    # Persist
    try:
        write_progress_report(all_findings)
        print(f"\n📝 问题追踪已更新: {PROGRESS_PATH}")
    except Exception as exc:
        print(f"\n❌ 写入 PROGRESS.md 失败: {exc}")

    print("\n=== 监控完毕 ===")


if __name__ == "__main__":
    main()
