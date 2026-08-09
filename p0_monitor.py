#!/usr/bin/env python3
"""P0 Guard Monitor — query DB, check logs, produce report."""

import sqlite3
import time
import os
import re
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

BEIJING_TZ = ZoneInfo("Asia/Shanghai")
DB_PATH = "/sessions/wizardly-sharp-ritchie/mnt/pmbot/data/metrics.db"
LOGS_DIR = "/sessions/wizardly-sharp-ritchie/mnt/pmbot/logs"

now_epoch = time.time()
now_beijing = datetime.now(BEIJING_TZ)
cutoff = now_epoch - 1200  # 20 minutes
today_str = now_beijing.strftime("%Y-%m-%d")
log_file = os.path.join(LOGS_DIR, f"pmbot.{today_str}.log")

print(f"=== P0 Guard 监控报告 [{now_beijing.strftime('%Y-%m-%d %H:%M:%S')} CST] ===\n")

# ── 1. Query recent decisions ──
print("📊 当前监控市场:")
conn = None
rows = None
signal_rows = None
try:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("""
        SELECT market, cid, COUNT(*) as cnt,
               GROUP_CONCAT(DISTINCT yes_action) as yes_actions,
               GROUP_CONCAT(DISTINCT no_action) as no_actions,
               MIN(score) as min_score,
               MAX(score) as max_score,
               GROUP_CONCAT(DISTINCT mode) as modes
        FROM quote_risk_decisions
        WHERE ts >= ?
        GROUP BY cid
        ORDER BY cnt DESC
    """, (cutoff,))
    rows = cur.fetchall()

    if rows:
        for r in rows:
            market = r["market"] or "unknown"
            cid = r["cid"]
            cnt = r["cnt"]
            yes_a = r["yes_actions"] or "?"
            no_a = r["no_actions"] or "?"
            min_s = r["min_score"]
            max_s = r["max_score"]
            modes = r["modes"] or "?"

            has_non_allow = ("widen" in (yes_a or "") or "pull" in (yes_a or "") or
                             "widen" in (no_a or "") or "pull" in (no_a or ""))

            non_allow_flag = ""
            if has_non_allow:
                non_allow_flag = " ⚠️ 含non-allow"

            print(f"  - {market}: {cnt} 条决策, score [{min_s:.3f}-{max_s:.3f}], "
                  f"YES={yes_a} NO={no_a}, mode={modes}{non_allow_flag}")
    else:
        print("  ⚠️ 无市场处于P0监控下")

    # ── 2. Signal changes (non-allow only) ──
    print("\n⚠️ 信号变化 (非allow决策):")
    cur.execute("""
        SELECT market, cid, yes_action, no_action, score, reason, mode, ts
        FROM quote_risk_decisions
        WHERE ts >= ? AND (yes_action != 'allow' OR no_action != 'allow')
        ORDER BY ts DESC
        LIMIT 30
    """, (cutoff,))
    signal_rows = cur.fetchall()

    if signal_rows:
        for r in signal_rows:
            ts_beijing = datetime.fromtimestamp(r["ts"], BEIJING_TZ).strftime("%H:%M:%S")
            print(f"  [{ts_beijing}] {r['market']}: YES={r['yes_action']} NO={r['no_action']} "
                  f"score={r['score']:.3f} reason={r['reason']} mode={r['mode']}")
    else:
        print("  ✅ 无信号变化（全部 allow）")

    # ── 3. Check for consecutive widen/pull patterns ──
    print("\n🔍 异常模式检测:")
    if signal_rows:
        signals_by_market = {}
        for r in signal_rows:
            key = r["market"]
            if key not in signals_by_market:
                signals_by_market[key] = []
            signals_by_market[key].append(r)

        found_anomaly = False
        for market, sigs in signals_by_market.items():
            non_allow_count = len(sigs)
            if non_allow_count >= 5:
                print(f"  ⚠️ {market}: 连续 {non_allow_count} 次非allow，需关注")
                found_anomaly = True
        if not found_anomaly:
            print("  ✅ 无异常模式（无市场连续5次以上非allow）")
    else:
        print("  ✅ 无异常模式")
except Exception as e:
    print(f"  ❌ 数据库查询失败: {e}")
    import traceback
    traceback.print_exc()
finally:
    if conn:
        conn.close()

# ── 4. Log grep for P0 events ──
print("\n📝 日志中的 P0 事件 (最近20分钟):")
if os.path.exists(log_file):
    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()

        p0_lines = []
        for line in lines:
            if "P0" in line:
                match = re.match(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', line)
                if match:
                    try:
                        line_ts = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=BEIJING_TZ)
                        line_epoch = line_ts.timestamp()
                        if line_epoch >= cutoff:
                            p0_lines.append(line.rstrip())
                    except ValueError:
                        p0_lines.append(line.rstrip())

        if p0_lines:
            for l in p0_lines[-5:]:
                print(f"  {l[:250]}")
            if len(p0_lines) > 5:
                print(f"  ... (共 {len(p0_lines)} 条，显示最近5条)")
        else:
            print("  无 P0 事件记录")
    except Exception as e:
        print(f"  ❌ 日志读取失败: {e}")
else:
    print(f"  ⚠️ 日志文件不存在: {log_file}")

# ── 5. Summary ──
print("\n---")
if rows is None or len(rows) == 0:
    print("⚠️ 需关注: 无市场处于P0监控下")
elif signal_rows and len(signal_rows) > 0:
    print(f"⚠️ 需关注: {len(signal_rows)} 条非allow决策")
else:
    print("✅ 状态正常")
