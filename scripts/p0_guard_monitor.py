#!/usr/bin/env python3
"""P0 Guard Monitor - 逆向选择防护监控"""

import sqlite3
import time
import subprocess
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

DB_PATH = "/sessions/cool-dazzling-wozniak/mnt/pmbot/data/metrics.db"
BEIJING_TZ = ZoneInfo("Asia/Shanghai")
NOW = time.time()
CUTOFF = NOW - 1200  # 20 minutes
NOW_BJ = datetime.now(BEIJING_TZ)
LOG_DATE = NOW_BJ.strftime("%Y-%m-%d")
LOG_PATH = f"/sessions/cool-dazzling-wozniak/mnt/pmbot/logs/pmbot.{LOG_DATE}.log"

def query_db(query, params=(), retries=3):
    for attempt in range(retries):
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute(query, params)
            rows = [dict(r) for r in cur.fetchall()]
            conn.close()
            return rows
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and attempt < retries - 1:
                time.sleep(2)
            else:
                raise

print(f"=== P0 Guard 监控报告 [{NOW_BJ.strftime('%H:%M:%S')}] ===\n")

# 1. 当前监控市场
print("📊 当前监控市场:")
rows = query_db(
    """SELECT cid, COUNT(*) as cnt,
              AVG(score) as avg_score,
              MAX(score) as max_score,
              MIN(score) as min_score,
              GROUP_CONCAT(DISTINCT yes_action) as yes_actions,
              GROUP_CONCAT(DISTINCT no_action) as no_actions,
              GROUP_CONCAT(DISTINCT mode) as modes
       FROM quote_risk_decisions
       WHERE ts >= ?
       GROUP BY cid
       ORDER BY cnt DESC""",
    (CUTOFF,)
)

non_allow_markets = []
if not rows:
    print("  ⚠️ 无市场处于P0监控下 (最近20分钟无决策记录)")
else:
    for r in rows:
        yes_actions = r['yes_actions'] or '?'
        no_actions = r['no_actions'] or '?'
        all_allow = (yes_actions == 'allow' and no_actions == 'allow')
        status = "✅" if all_allow else "⚠️"
        if not all_allow:
            non_allow_markets.append(r['cid'])

        print(f"  {status} {r['cid']}: {r['cnt']} 条决策, "
              f"score [{r['min_score']:.3f}-{r['max_score']:.3f}] avg={r['avg_score']:.3f}, "
              f"YES={yes_actions} NO={no_actions} mode={r['modes']}")

print(f"\n  共 {len(rows)} 个市场在监控中")

# 2. 信号变化 - 非 allow 决策
print("\n⚠️ 信号变化 (非 allow 决策, 最近20分钟):")
non_allow_rows = query_db(
    """SELECT cid, yes_action, no_action, score, reason, mode, ts
       FROM quote_risk_decisions
       WHERE ts >= ?
         AND (yes_action != 'allow' OR no_action != 'allow')
       ORDER BY ts DESC""",
    (CUTOFF,)
)

if not non_allow_rows:
    print("  无信号变化 (无 widen/pull/skip 决策)")
else:
    for r in non_allow_rows:
        bj_time = datetime.fromtimestamp(r['ts'], BEIJING_TZ).strftime('%H:%M:%S')
        actions = []
        if r['yes_action'] != 'allow':
            actions.append(f"YES={r['yes_action']}")
        if r['no_action'] != 'allow':
            actions.append(f"NO={r['no_action']}")
        print(f"  {bj_time} {r['cid']}: {', '.join(actions)} "
              f"score={r['score']:.3f} reason={r['reason']} mode={r['mode']}")
    print(f"\n  共 {len(non_allow_rows)} 条非 allow 决策")

# 3. 日志中的 P0 事件
print("\n📝 日志中的 P0 事件:")
try:
    result = subprocess.run(
        ["grep", "P0 逆向选择防护", LOG_PATH],
        capture_output=True, text=True, timeout=10
    )
    lines = result.stdout.strip().split('\n')
    # Filter to last 30 lines, then take most recent
    recent_lines = [l for l in lines if l.strip()]
    recent_lines = recent_lines[-30:]

    if not recent_lines:
        print("  无 P0 相关日志")
    else:
        for line in recent_lines[-10:]:  # show last 10
            print(f"  {line.strip()}")
        print(f"\n  共 {len(recent_lines)} 条 P0 日志 (最近30条)")
except FileNotFoundError:
    print(f"  ⚠️ 日志文件不存在: {LOG_PATH}")
except Exception as e:
    print(f"  ⚠️ 读取日志失败: {e}")

# 4. 统计总结
print(f"\n--- 总结 ---")

# 统计各 action 频次
action_stats = query_db(
    """SELECT yes_action, no_action, COUNT(*) as cnt
       FROM quote_risk_decisions
       WHERE ts >= ?
       GROUP BY yes_action, no_action
       ORDER BY cnt DESC""",
    (CUTOFF,)
)

widen_count = 0
pull_count = 0
skip_count = 0
for r in action_stats:
    if r['yes_action'] in ('widen', 'pull', 'skip'):
        if r['yes_action'] == 'widen':
            widen_count += r['cnt']
        elif r['yes_action'] == 'pull':
            pull_count += r['cnt']
        else:
            skip_count += r['cnt']
    if r['no_action'] in ('widen', 'pull', 'skip'):
        if r['no_action'] == 'widen':
            widen_count += r['cnt']
        elif r['no_action'] == 'pull':
            pull_count += r['cnt']
        else:
            skip_count += r['cnt']

print(f"  widen: {widen_count} 次, pull: {pull_count} 次, skip: {skip_count} 次")

issues = []
if not rows:
    issues.append("无市场处于P0监控下")
if widen_count > 5:
    issues.append(f"widen 次数较多 ({widen_count}次)")
if pull_count > 3:
    issues.append(f"pull 次数较多 ({pull_count}次)")

if issues:
    print(f"⚠️ 需关注: {'; '.join(issues)}")
else:
    print("✅ 状态正常")
