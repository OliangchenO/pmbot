"""
分析每笔建仓后市场价格是否达到过「成交价+0.01」（同向和互补两个维度）。

数据来源：Polymarket CLOB prices-history API（真实市场中间价），非 bot 自身报价。

同向：buy YES 0.56 → 后续 YES 市场价 >= 0.57？
互补：buy YES 0.56 → 后续 NO 市场价 <= 0.43？（1 - 0.56 - 0.01）

输出 Excel：scripts/analyze_fill_targets.xlsx
"""

import json
import sqlite3
import time as _time
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "metrics.db"
OUTPUT = Path(__file__).with_suffix(".xlsx")

# Polymarket CLOB API
PRICES_HISTORY_URL = "https://clob.polymarket.com/prices-history"
GAMMA_URL = "https://gamma-api.polymarket.com/markets"

# 缓存：hex_token -> decimal_token 映射
TOKEN_CACHE: dict[str, str] = {}
# 缓存：decimal_token -> [{"t":..., "p":...}, ...]
PRICE_CACHE: dict[str, list[dict]] = {}


def _fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _fmt_delta(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "—"
    seconds = int(seconds)
    h, remainder = divmod(seconds, 3600)
    m, s = divmod(remainder, 60)
    parts = []
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    if s or not parts:
        parts.append(f"{s}s")
    return " ".join(parts)


def _hex_to_decimal(hex_token: str) -> str:
    """hex token_id -> decimal token_id（用于 API）"""
    if hex_token not in TOKEN_CACHE:
        TOKEN_CACHE[hex_token] = str(int(hex_token))
    return TOKEN_CACHE[hex_token]


def fetch_price_history(dec_token: str) -> list[dict]:
    """获取一个 token 的完整历史价格，返回 [{"t": ts, "p": price}, ...]"""
    if dec_token in PRICE_CACHE:
        return PRICE_CACHE[dec_token]

    try:
        r = httpx.get(
            PRICES_HISTORY_URL,
            params={"market": dec_token, "interval": "max"},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        history = data.get("history", [])
        PRICE_CACHE[dec_token] = history
        return history
    except Exception as e:
        print(f"  ⚠ API 获取失败 token={dec_token[:20]}...: {e}")
        PRICE_CACHE[dec_token] = []
        return []


def find_first_hit(history: list[dict], after_ts: float,
                   condition) -> tuple[float | None, float | None]:
    """在 history 中找 after_ts 之后第一个满足 condition(price) 的点。
    condition: callable(price) -> bool
    返回 (hit_ts, hit_price) 或 (None, None)
    """
    for entry in history:
        t = entry["t"]
        p = entry["p"]
        if t <= after_ts:
            continue
        if condition(p):
            return t, p
    return None, None


def find_first_hit_before(history: list[dict], after_ts: float, until_ts: float,
                          condition) -> tuple[float | None, float | None]:
    """Find a midpoint hit strictly inside an inventory episode/window."""
    for entry in history:
        t = entry["t"]
        if t <= after_ts:
            continue
        if t > until_ts:
            break
        if condition(entry["p"]):
            return t, entry["p"]
    return None, None


def build_inventory_episodes(fills: list[dict]) -> list[dict]:
    """Group same-token entries until a recorded same-token exit.

    This is deliberately conservative: an episode without an explicit exit is
    left open and must later be censored at the analysis window/market end.
    """
    open_episodes: dict[tuple[str, str], dict] = {}
    completed: list[dict] = []
    for fill in sorted(fills, key=lambda item: float(item["ts"])):
        key = (str(fill["cid"]), str(fill["token"]))
        if bool(fill.get("exit")):
            episode = open_episodes.pop(key, None)
            if episode is not None:
                episode["end_ts"] = float(fill["ts"])
                completed.append(episode)
            continue
        size = float(fill["size"])
        price = float(fill["price"])
        episode = open_episodes.get(key)
        if episode is None:
            episode = {
                "episode_id": f"{fill['cid']}:{fill['token']}:{fill['id']}",
                "cid": fill["cid"], "token": fill["token"],
                "market": fill.get("market", ""), "side": fill.get("side", ""),
                "start_ts": float(fill["ts"]), "end_ts": None,
                "shares": 0.0, "total_cost": 0.0,
            }
            open_episodes[key] = episode
        episode["shares"] += size
        episode["total_cost"] += size * price
    completed.extend(open_episodes.values())
    for episode in completed:
        episode["basis"] = episode["total_cost"] / episode["shares"]
    return sorted(completed, key=lambda item: item["start_ts"])


def get_complementary_token(fills_for_cid: list[dict], my_token: str) -> str | None:
    """在同个 cid 的 fill 列表中找到互补 token"""
    for f in fills_for_cid:
        if f["token"] != my_token:
            return f["token"]
    return None


def main():
    import openpyxl
    from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
    from openpyxl.utils import get_column_letter
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    # ── 1. 读取完整成交序列，并聚合为库存 episode ──
    fills = conn.execute(
        "SELECT id, ts, cid, market, side, token, price, size, taker, exit "
        "FROM fills ORDER BY ts ASC"
    ).fetchall()
    fill_dicts = [dict(row) for row in fills]
    episodes = build_inventory_episodes(fill_dicts)

    # 按 cid 分组，方便找互补 token
    fills_by_cid: dict[str, list[dict]] = {}
    for f in fill_dicts:
        cid = f["cid"]
        fills_by_cid.setdefault(cid, []).append(f)

    # ── 2. 收集所有需要的 token，预取价格历史 ──
    all_hex_tokens = set()
    for f in fills:
        all_hex_tokens.add(f["token"])
        # 也尝试找互补 token（如果用 Gamma API）
    for cid_fills in fills_by_cid.values():
        all_tokens = {ff["token"] for ff in cid_fills}
        for ff in cid_fills:
            others = all_tokens - {ff["token"]}
            if others:
                all_hex_tokens.add(next(iter(others)))

    print(f"去重后 token 数: {len(all_hex_tokens)}")
    # 预取所有价格历史
    for i, hex_tok in enumerate(sorted(all_hex_tokens)):
        dec = _hex_to_decimal(hex_tok)
        print(f"  [{i+1}/{len(all_hex_tokens)}] 获取价格历史... {hex_tok[:20]}...", end=" ")
        h = fetch_price_history(dec)
        print(f"{len(h)} 条")
        _time.sleep(0.1)  # 温和限流

    # ── 3. 分析每笔 fill ──
    results = []
    for f in episodes:
        fill_ts = f["start_ts"]
        cid = f["cid"]
        token = f["token"]
        side = f["side"]
        fill_price = f["basis"]
        size = f["shares"]
        market = f["market"]
        kind = "episode"
        # Open episodes are censored at the current observation time; never
        # count an unlimited future path as an achievable exit opportunity.
        analysis_end = min(fill_ts + 24 * 3600, f["end_ts"] or _time.time())

        # ── 同向：同一 token 价格 >= fill_price + 0.01 ──
        same_target = round(fill_price + 0.01, 4)
        dec_token = _hex_to_decimal(token)
        same_history = fetch_price_history(dec_token)
        same_hit_ts, same_hit_price = find_first_hit_before(
            same_history, fill_ts, analysis_end,
            lambda p: p >= same_target - 1e-9,
        )

        # ── 互补方向：互补 token 价格 <= 1 - fill_price - 0.01 ──
        comp_target = round(1.0 - fill_price - 0.01, 4)
        cid_fills = fills_by_cid.get(cid, [])
        comp_token = None
        comp_hit_ts = None
        comp_hit_price = None
        comp_history_len = 0

        all_cid_tokens = {ff["token"] for ff in cid_fills}
        others = all_cid_tokens - {token}
        if others:
            comp_token = next(iter(others))
            dec_comp = _hex_to_decimal(comp_token)
            comp_history = fetch_price_history(dec_comp)
            comp_history_len = len(comp_history)
            comp_hit_ts, comp_hit_price = find_first_hit_before(
                comp_history, fill_ts, analysis_end,
                lambda p: p <= comp_target + 1e-9,
            )

        results.append({
            "fill_id": f["episode_id"],
            "fill_time_utc": _fmt_ts(fill_ts),
            "fill_ts": fill_ts,
            "market": market,
            "cid": cid,
            "side": side,
            "kind": kind,
            "fill_price": fill_price,
            "size": size,
            "episode_end_ts": f["end_ts"],
            "censored": f["end_ts"] is None,

            # 同向
            "same_target": same_target,
            "same_reached": same_hit_ts is not None,
            "same_hit_time_utc": _fmt_ts(same_hit_ts) if same_hit_ts else None,
            "same_hit_price": same_hit_price,
            "same_time_sec": (same_hit_ts - fill_ts) if same_hit_ts else None,
            "same_time_str": _fmt_delta(same_hit_ts - fill_ts) if same_hit_ts else "未到达",
            "same_history_len": len(same_history),

            # 互补方向
            "comp_target": comp_target,
            "comp_reached": comp_hit_ts is not None,
            "comp_hit_time_utc": _fmt_ts(comp_hit_ts) if comp_hit_ts else None,
            "comp_hit_price": comp_hit_price,
            "comp_time_sec": (comp_hit_ts - fill_ts) if comp_hit_ts else None,
            "comp_time_str": _fmt_delta(comp_hit_ts - fill_ts) if comp_hit_ts else "未到达",
            "comp_history_len": comp_history_len,

            # 综合
            "any_reached": (same_hit_ts is not None) or (comp_hit_ts is not None),
        })

    conn.close()

    # ── 4. 写入 Excel ──
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Fill Target +0.01"

    # 样式
    header_font = Font(name="微软雅黑", bold=True, size=10, color="FFFFFF")
    header_fill_blue = PatternFill(start_color="2F5496", end_color="2F5496", fill_type="solid")
    header_fill_green = PatternFill(start_color="538135", end_color="538135", fill_type="solid")
    header_fill_gold = PatternFill(start_color="BF8F00", end_color="BF8F00", fill_type="solid")
    header_alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    center_align = Alignment(horizontal="center", vertical="center")
    right_align = Alignment(horizontal="right", vertical="center")
    thin_border = Border(
        left=Side(style="thin"), right=Side(style="thin"),
        top=Side(style="thin"), bottom=Side(style="thin"),
    )
    green_fill = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
    red_fill = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
    red_row = PatternFill(start_color="FFEBEB", end_color="FFEBEB", fill_type="solid")

    headers = [
        "Episode ID", "建仓时间 (UTC)", "市场", "类型", "方向",
        "成交价", "数量",
        # 同向
        "同向:目标价\n(+0.01)", "同向:到达", "同向:到达时间", "同向:到达价", "同向:用时", "历史条数",
        # 互补
        "互补:目标价\n(1-p-0.01)", "互补:到达", "互补:到达时间", "互补:到达价", "互补:用时", "历史条数",
        # 综合
        "任一到\n达",
        "Condition ID",
    ]

    for col_idx, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col_idx, value=header)
        cell.font = header_font
        if col_idx <= 7:
            cell.fill = header_fill_blue
        elif col_idx <= 13:
            cell.fill = header_fill_green
        elif col_idx <= 19:
            cell.fill = header_fill_gold
        else:
            cell.fill = header_fill_blue
        cell.alignment = header_alignment
        cell.border = thin_border

    for row_idx, r in enumerate(results, 2):
        values = [
            r["fill_id"], r["fill_time_utc"], r["market"], r["kind"], r["side"],
            r["fill_price"], r["size"],
            r["same_target"],
            "是 ✓" if r["same_reached"] else "否 ✗",
            r["same_hit_time_utc"],
            round(r["same_hit_price"], 4) if r["same_hit_price"] is not None else None,
            r["same_time_str"],
            r["same_history_len"],
            r["comp_target"],
            "是 ✓" if r["comp_reached"] else "否 ✗",
            r["comp_hit_time_utc"],
            round(r["comp_hit_price"], 4) if r["comp_hit_price"] is not None else None,
            r["comp_time_str"],
            r["comp_history_len"],
            "是 ✓" if r["any_reached"] else "否 ✗",
            r["cid"],
        ]
        for col_idx, val in enumerate(values, 1):
            cell = ws.cell(row=row_idx, column=col_idx, value=val)
            cell.border = thin_border
            if col_idx in (1, 4, 5, 9, 14, 19):
                cell.alignment = center_align
            elif col_idx in (6, 7, 8, 11, 12, 13, 16, 17, 18):
                cell.alignment = right_align
            else:
                cell.alignment = Alignment(vertical="center")

        # 着色
        ws.cell(row=row_idx, column=9).fill = green_fill if r["same_reached"] else red_fill
        ws.cell(row=row_idx, column=14).fill = green_fill if r["comp_reached"] else red_fill
        ws.cell(row=row_idx, column=19).fill = green_fill if r["any_reached"] else red_fill
        if not r["any_reached"]:
            for col_idx in range(1, len(headers) + 1):
                ws.cell(row=row_idx, column=col_idx).fill = red_row

    # ── 5. 汇总 sheet ──
    ws2 = wb.create_sheet("汇总")
    total = len(results)
    same_reached = sum(1 for r in results if r["same_reached"])
    comp_reached = sum(1 for r in results if r["comp_reached"])
    any_reached = sum(1 for r in results if r["any_reached"])
    both_reached = sum(1 for r in results if r["same_reached"] and r["comp_reached"])
    none_reached = total - any_reached

    summary_data = [
        ["统计项", "数值"],
        ["数据来源", "Polymarket CLOB prices-history API（midpoint，机会率上界）"],
        ["库存 episode 数", total],
        ["", ""],
        ["同向到达 (token >= p+0.01)",
         f"{same_reached} ({same_reached/total*100:.1f}%)" if total else "N/A"],
        ["互补到达 (complement <= 1-p-0.01)",
         f"{comp_reached} ({comp_reached/total*100:.1f}%)" if total else "N/A"],
        ["任一方向到达",
         f"{any_reached} ({any_reached/total*100:.1f}%)" if total else "N/A"],
        ["双向都到达",
         f"{both_reached} ({both_reached/total*100:.1f}%)" if total else "N/A"],
        ["都未到达",
         f"{none_reached} ({none_reached/total*100:.1f}%)" if total else "N/A"],
        ["", ""],
    ]

    # 用时分布
    for label in ["同向到达用时分布", "互补到达用时分布", "任一方向到达用时分布"]:
        if "同向" in label:
            times_key = [r["same_time_sec"] for r in results
                         if r["same_reached"] and r["same_time_sec"] is not None]
        elif "互补" in label:
            times_key = [r["comp_time_sec"] for r in results
                         if r["comp_reached"] and r["comp_time_sec"] is not None]
        else:
            arr = []
            for r in results:
                ts_list = []
                if r["same_time_sec"] is not None:
                    ts_list.append(r["same_time_sec"])
                if r["comp_time_sec"] is not None:
                    ts_list.append(r["comp_time_sec"])
                if ts_list:
                    arr.append(min(ts_list))
            times_key = arr

        if not times_key:
            continue
        summary_data.append([f"--- {label} ---", ""])
        summary_data.append(["最快", _fmt_delta(min(times_key))])
        summary_data.append(["最慢", _fmt_delta(max(times_key))])
        summary_data.append(["平均", _fmt_delta(sum(times_key) / len(times_key))])
        sorted_times = sorted(times_key)
        mid = len(sorted_times) // 2
        median = sorted_times[mid] if len(sorted_times) % 2 else (
            sorted_times[mid - 1] + sorted_times[mid]) / 2
        summary_data.append(["中位数", _fmt_delta(median)])

        summary_data.append(["", ""])
        summary_data.append(["时间分段", "笔数"])
        segments = [
            (0, 60, "≤1分钟"), (60, 300, "1-5分钟"), (300, 600, "5-10分钟"),
            (600, 1800, "10-30分钟"), (1800, 3600, "30分钟-1小时"),
            (3600, 7200, "1-2小时"), (7200, 14400, "2-4小时"),
            (14400, 86400, "4小时-1天"), (86400, float("inf"), ">1天"),
        ]
        for lo, hi, seg_label in segments:
            count = sum(1 for t in times_key if lo <= t < hi)
            summary_data.append([seg_label, count])

    for row_idx, (col1, col2) in enumerate(summary_data, 1):
        c1 = ws2.cell(row=row_idx, column=1, value=col1)
        c2 = ws2.cell(row=row_idx, column=2, value=col2)
        c1.border = thin_border
        c2.border = thin_border
        if row_idx == 1:
            c1.font = header_font; c1.fill = header_fill_blue; c1.alignment = header_alignment
            c2.font = header_font; c2.fill = header_fill_blue; c2.alignment = header_alignment

    # ── 6. 列宽 ──
    col_widths = [7, 21, 50, 7, 5, 9, 7, 15, 8, 21, 9, 13, 9, 16, 8, 21, 9, 13, 9, 8, 68]
    for col_idx, width in enumerate(col_widths, 1):
        ws.column_dimensions[get_column_letter(col_idx)].width = width

    ws2.column_dimensions["A"].width = 40
    ws2.column_dimensions["B"].width = 25

    ws.freeze_panes = "A2"

    wb.save(str(OUTPUT))
    print(f"\n✅ 分析完成！")
    print(f"   数据来源: Polymarket CLOB prices-history API（midpoint，机会率上界）")
    print(f"   库存 episode 数: {total}")
    print(f"   同向到达 (+0.01):  {same_reached} ({same_reached/total*100:.1f}%)" if total else "")
    print(f"   互补到达 (1-p-0.01): {comp_reached} ({comp_reached/total*100:.1f}%)" if total else "")
    print(f"   任一方向到达:       {any_reached} ({any_reached/total*100:.1f}%)" if total else "")
    print(f"   都未到达:           {none_reached}")
    print(f"   输出文件: {OUTPUT}")


if __name__ == "__main__":
    main()
