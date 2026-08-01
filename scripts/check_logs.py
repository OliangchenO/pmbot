"""Check recent pmbot logs for issues."""
import datetime
import re
import sys
from collections import Counter
from pathlib import Path

BEIJING_TZ = datetime.timezone(datetime.timedelta(hours=8))
LOG_DIR = Path(__file__).resolve().parent.parent / "logs"

def main(minutes: int = 10):
    now = datetime.datetime.now(BEIJING_TZ)
    log_file = LOG_DIR / f"pmbot.{now:%Y-%m-%d}.log"
    if not log_file.exists():
        print(f"[!] log file not found: {log_file}")
        sys.exit(1)

    print(f"=== pmbot 日志检查 ({now:%Y-%m-%d %H:%M:%S} 北京时间) ===")
    print(f"文件: {log_file}")

    lines = log_file.read_text(encoding="utf-8").splitlines()
    cutoff = (now - datetime.timedelta(minutes=minutes)).timestamp()
    recent = []
    for line in lines:
        try:
            ts_str = line[:19]
            ts = datetime.datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")\
                    .replace(tzinfo=BEIJING_TZ).timestamp()
            if ts >= cutoff:
                recent.append(line)
        except (ValueError, IndexError):
            pass

    print(f"最近 {minutes} 分钟: {len(recent)} 行\n")

    # ── 1. ERRORs ──
    errors = [l for l in recent if "ERROR" in l[:30]]
    if errors:
        print(f"🔴 ERROR ({len(errors)}):")
        for e in errors:
            print(f"  {e.strip()[:300]}")
    else:
        print("✅ 无 ERROR")

    # ── 2. Phase transitions ──
    phases = [l for l in recent if "补单阶段" in l]
    if phases:
        print(f"\n📋 补单阶段变化 ({len(phases)}):")
        for p in phases:
            print(f"  {p.strip()[:200]}")

    # ── 3. Hedge deferrals per market ──
    hedges = [l for l in recent if "强制对冲推迟" in l]
    hedge_markets = Counter()
    for h in hedges:
        m = re.search(r"强制对冲推迟 '([^']+)'", h)
        if m:
            hedge_markets[m.group(1)[:60]] += 1
    if hedge_markets:
        print(f"\n📊 强制对冲推迟:")
        for mk, cnt in hedge_markets.most_common():
            flag = " ⚠️ >3次" if cnt > 3 else ""
            print(f"  {mk}: {cnt}次{flag}")

    # ── 4. Recovery skips ──
    skips = [l for l in recent if "补单跳过" in l]
    if skips:
        print(f"\n⏭️ 补单跳过 ({len(skips)}):")
        for s in skips[-5:]:
            print(f"  {s.strip()[:300]}")

    # ── 5. Order duplicates ──
    placed = re.findall(r"ORDER_PLACED.*price=([\d.]+).*size=([\d.]+).*side=(\w+)", "\n".join(recent))
    placed_counts = Counter(placed)
    cancelled_counts = Counter()
    for line in recent:
        m = re.search(r"ORDER_CANCELLED.*price=([\d.]+).*size=([\d.]+).*side=(\w+)", line)
        if m:
            cancelled_counts[(m.group(3), m.group(1), m.group(2))] += 1
    # GTD refresh overlap: new order placed before old one is cancelled,
    # so one "extra" PLACED per side at any moment is normal.
    dupes = {k: (v, cancelled_counts.get(k, 0)) for k, v in placed_counts.items()
             if v - cancelled_counts.get(k, 0) > 1}
    if dupes:
        print(f"\n🔴 重复报价（placed - cancelled > 1）:")
        for (side, price, size), (placed_n, cancelled_n) in dupes.items():
            print(f"  {side} x{size} @ {price} — placed {placed_n}, cancelled {cancelled_n}")
    else:
        print("✅ 无重复报价")

    # ── 6. Order failures ──
    fails = [l for l in recent if "ORDER_POST_FAILED" in l or "ORDER_CANCEL_FAILED" in l
             or "request error" in l]
    if fails:
        print(f"\n🔴 订单失败 ({len(fails)}):")
        for f in fails:
            print(f"  {f.strip()[:300]}")

    # ── 7. WebSocket ──
    ws = [l for l in recent if any(k in l for k in ("WebSocket", "websocket", "ws_fill",
                "subscripti", "disconnect", "feed stale", "feed_age"))]
    if ws:
        print(f"\n🔌 WebSocket/Feed ({len(ws)}):")
        for w in ws[-5:]:
            print(f"  {w.strip()[:200]}")

    # ── 8. Controller ──
    ctrl = [l for l in recent if "controller adjusted" in l]
    if ctrl:
        print(f"\n🎛️ Controller:")
        for c in ctrl[-3:]:
            print(f"  {c.strip()[:300]}")

    # ── 9. WARNINGs not covered above ──
    covered = {*errors, *phases, *hedges, *skips, *fails, *ws, *ctrl}
    other_warn = [l for l in recent if "WARNING" in l and l not in covered]
    if other_warn:
        print(f"\n⚠️ 其他 WARNING ({len(other_warn)}):")
        for w in other_warn[-10:]:
            print(f"  {w.strip()[:300]}")

    print("\n=== 检查完毕 ===")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("-m", "--minutes", type=int, default=10)
    args = p.parse_args()
    main(args.minutes)
