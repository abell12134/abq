"""盘中实时快照：腾讯批量报价 → 温度计/涨跌榜/涨停预警/封单散点数据。

与盘后快照（run_board，本地 qlib 收盘口径）的关系：
  - 盘后快照 = 历史序列与连板的真相源（连板数、昨日涨停表现）
  - 盘中快照 = 手动刷新时的实时叠加（价格、封单、预警）
  合并策略：连板数/历史来自盘后快照；实时涨跌幅/封单来自腾讯。
  盘中「涨停」以腾讯涨停价字段判定（含 ST 5% 精度），比昨收推算更准。
"""

from __future__ import annotations

import logging
from typing import Any

from . import batch_quotes, pool as pool_mod, store

log = logging.getLogger(__name__)

WARN_NEAR_LIMIT_PCT = 8.0      # 涨停预警：主板涨幅 ≥8% 未封板
WARN_NEAR_LIMIT_PCT_20 = 15.0  # 创业/科创预警线


def _warn_threshold(inst: str) -> float:
    lim = 0.20 if inst[2:].startswith(("300", "301", "688", "689")) else 0.10
    if inst.startswith("BJ"):
        lim = 0.30
    return (lim - 0.02) * 100


def build_intraday(pool: list[dict] | None = None) -> dict[str, Any]:
    """手动刷新入口：拉实时报价 → 合并盘后连板 → 写 intraday/latest.json。"""
    pool = pool or pool_mod.load_pool()
    instruments = [p["instrument"] for p in pool]
    meta = {p["instrument"]: p for p in pool}

    quotes = batch_quotes.fetch_pool_quotes(instruments)
    if not quotes:
        return {"ok": False, "error": "批量报价全部失败（外网不可用？）"}

    # 盘后快照补连板数/昨日涨停（无快照也能跑，streak 记 None）
    _, snap = store.load_latest_daily()
    streak_map: dict[str, int] = {}
    if snap:
        for sect, key in (("gainers", None), ("losers", None)):
            for r in snap.get(sect) or []:
                streak_map[r["instrument"]] = r.get("streak") or 0
        for t in (snap.get("ladder") or {}).get("tiers") or []:
            for s in t.get("stocks") or []:
                streak_map[s["instrument"]] = s.get("streak") or 0
        for s in (snap.get("ladder") or {}).get("first_boards") or []:
            streak_map.setdefault(s["instrument"], s.get("streak") or 0)

    rows: list[dict[str, Any]] = []
    limit_ups, warns = [], []
    up = down = flat = up_gt5 = down_lt5 = 0
    broken_n = 0
    for inst, q in quotes.items():
        m = meta.get(inst, {})
        prev_streak = streak_map.get(inst, 0)
        # 实时连板数：昨 N 板 + 今涨停 = N+1（昨首板在 streak_map 里为 1）
        streak = prev_streak + 1 if q["at_limit_up"] else 0
        row = {
            "instrument": inst,
            "name": q["name"] or m.get("name", ""),
            "industry": m.get("industry", "未知"),
            "price": q["price"], "chg_pct": q["chg_pct"],
            "turnover_pct": q["turnover_pct"], "vol_ratio": q["vol_ratio"],
            "amount_yi": round(q["amount_wan"] / 1e4, 2),
            "float_mv_yi": q["float_mv_yi"],
            "at_limit_up": q["at_limit_up"], "at_limit_down": q["at_limit_down"],
            "broken": q["broken"],
            "sealed_amt": q["sealed_amt"], "sealed_ratio": q["sealed_ratio"],
            "streak": streak,
            "quote_time": q["quote_time"],
        }
        rows.append(row)
        r = q["ret"]
        if q["at_limit_up"]:
            limit_ups.append(row)
        elif q["at_limit_down"]:
            down += 0  # 单独统计在下方
        if q["broken"]:
            broken_n += 1
        if not q["at_limit_up"] and q["chg_pct"] >= _warn_threshold(inst):
            warns.append(row)
        if r > 0.0005:
            up += 1
        elif r < -0.0005:
            down += 1
        else:
            flat += 1
        if r >= 0.05:
            up_gt5 += 1
        if r <= -0.05:
            down_lt5 += 1

    limit_downs = [r for r in rows if r["at_limit_down"]]
    gainers = sorted(rows, key=lambda x: -x["chg_pct"])[:20]
    losers = sorted(rows, key=lambda x: x["chg_pct"])[:20]

    # 封单散点：x=封单/流通市值% y=连板数 气泡=成交额 色=涨停类型
    scatter = []
    for r in limit_ups:
        scatter.append({
            "instrument": r["instrument"], "name": r["name"], "industry": r["industry"],
            "x": round(r["sealed_ratio"] * 100, 3),          # 封单强度 %
            "y": r["streak"],                                 # 连板数
            "r_amt": r["amount_yi"],                          # 气泡=成交额（亿）
            "sealed_yi": round(r["sealed_amt"] / 1e8, 2),
            "chg_pct": r["chg_pct"], "turnover_pct": r["turnover_pct"],
            "float_mv_yi": r["float_mv_yi"],
        })
    scatter.sort(key=lambda x: -x["x"])

    # 实时温度（与 cycle._temperature 同思路，盘中口径）
    n = max(len(rows), 1)
    temp = 50.0
    temp += min(len(limit_ups) / n * 100 * 8, 25)
    temp -= min(len(limit_downs) / n * 100 * 12, 30)
    temp -= (broken_n / max(broken_n + len(limit_ups), 1)) * 25
    temp += (up - down) / n * 20
    temp = max(0.0, min(100.0, temp))

    payload = {
        "ok": True,
        "trade_day_like": rows[0]["quote_time"][:8] if rows and rows[0]["quote_time"] else None,
        "pool_size": len(rows),
        "quotes_ok": len(quotes), "quotes_total": len(instruments),
        "thermometer": {
            "temperature": round(temp, 1),
            "limit_up": len(limit_ups), "limit_down": len(limit_downs),
            "broken": broken_n,
            "up": up, "down": down, "flat": flat,
            "up_gt5": up_gt5, "down_lt5": down_lt5,
            "max_streak": max((r["streak"] for r in rows), default=0),
        },
        "gainers": gainers, "losers": losers,
        "limit_ups": sorted(limit_ups, key=lambda x: -x["sealed_amt"]),
        "warn_near_limit": sorted(warns, key=lambda x: -x["chg_pct"]),
        "scatter": scatter,
        "snapshot_day": snap.get("day") if snap else None,
        "disclaimer": "盘中实时数据（腾讯报价，手动刷新）；连板数基于盘后快照推算。不构成投资建议。",
    }
    store.save_intraday(payload)
    return payload


def load_or_build(force: bool = False) -> dict[str, Any]:
    """读缓存；force 或无缓存时重建。"""
    if not force:
        old = store.load_intraday()
        if old:
            return old
    return build_intraday()
