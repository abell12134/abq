"""单票评分卡：点击散点气泡/榜单时弹出。合并盘后 strong 三维 + 实时行情补充。

P2 五维口径：
  动量/强度     盘后 strong（横截面分位）
  量能          盘后量比分位 × 实时量比修正
  情绪          所属行业当日涨停家数占比 + 连板身份加成
  资金          实时量比×换手率活跃度近似（P3 升级主力净流入）
分级沿用 S≥80 / A≥65 / B≥50 / C<50。
"""

from __future__ import annotations

from typing import Any

from . import store


def _grade(score: float) -> str:
    if score >= 80:
        return "S"
    if score >= 65:
        return "A"
    if score >= 50:
        return "B"
    return "C"


def build_card(instrument: str) -> dict[str, Any]:
    """instrument 大写。合并 盘后快照 + 盘中快照（如有）。"""
    instrument = instrument.upper()
    card: dict[str, Any] = {"instrument": instrument, "ok": False}

    day, snap = store.load_latest_daily()
    intra = store.load_intraday()

    strong_map: dict[str, dict] = {}
    ind_hot: dict[str, int] = {}
    if snap:
        for s in (snap.get("strong") or {}).get("stocks") or []:
            strong_map[s["instrument"]] = s
        for d in snap.get("industry_dist") or []:
            ind_hot[d["industry"]] = d["count"]

    # 实时行（若有盘中快照）
    q = None
    if intra and intra.get("ok"):
        for r in (intra.get("limit_ups") or []) + (intra.get("gainers") or []) + \
                 (intra.get("losers") or []) + (intra.get("warn_near_limit") or []):
            if r["instrument"] == instrument:
                q = r
                break

    s = strong_map.get(instrument)
    if not s and not q:
        card["error"] = "该票不在盘后快照/盘中快照中（非池内或无数据）"
        return card

    industry = (s or {}).get("industry") or (q or {}).get("industry") or "未知"
    name = (s or {}).get("name") or (q or {}).get("name") or ""

    # 五维（缺失维度给中性 50 并标注）
    dims: dict[str, float] = {}
    notes: list[str] = []
    if s:
        dims["动量"] = s["dimensions"]["momentum"]
        dims["强度"] = s["dimensions"]["relative_strength"]
        vol_base = s["dimensions"]["volume_ratio"]
    else:
        dims["动量"] = 50.0
        dims["强度"] = 50.0
        vol_base = 50.0
        notes.append("未进盘后强势榜，动量/强度给中性分")

    # 量能：盘后分位 + 实时量比修正（实时量比>2 加成）
    vol_adj = vol_base
    if q and q.get("vol_ratio"):
        vol_adj = min(100.0, vol_base * 0.6 + min(q["vol_ratio"] / 3, 1) * 100 * 0.4)
    dims["量能"] = round(vol_adj, 1)

    # 情绪：行业涨停热度 + 连板身份
    n_hot = ind_hot.get(industry, 0)
    streak = (q or {}).get("streak") or (s or {}).get("streak") or 0
    emo = min(n_hot * 15, 60) + min(streak * 15, 40)
    dims["情绪"] = round(min(100.0, emo if (n_hot or streak) else 35.0), 1)

    # 资金：P3 起用真实主力净流入（盘后 fundflow 榜）；无则量比×换手近似
    fund_map: dict[str, dict] = {}
    if snap:
        for r in ((snap.get("fundflow") or {}).get("inflow") or []) + \
                 ((snap.get("fundflow") or {}).get("outflow") or []):
            fund_map[r["instrument"]] = r
    fr = fund_map.get(instrument)
    if fr:
        net_pct = abs(fr.get("main_net_pct") or 0)
        dims["资金"] = round(min(100.0, 40 + min(net_pct / 20, 1) * 60)
                             if (fr.get("main_net_yi") or 0) > 0 else
                             max(0.0, 40 - min(net_pct / 20, 1) * 40), 1)
        notes.append(f"主力净流入 {fr.get('main_net_yi')}亿（净占比 {fr.get('main_net_pct')}%）")
    elif q and q.get("turnover_pct") and q.get("vol_ratio"):
        act = min(q["vol_ratio"] / 3, 1) * 50 + min(q["turnover_pct"] / 15, 1) * 50
        dims["资金"] = round(min(100.0, act), 1)
        notes.append("资金维度为量比×换手近似（当日未进资金流榜）")
    else:
        dims["资金"] = 50.0
        notes.append("无资金数据，给中性分")

    # 综合（五维等权——P2 简化；盘后三维分高者优先已在动量/强度体现）
    score = round(sum(dims.values()) / len(dims), 1)
    if s:
        # 盘后三维综合分占 60%，五维均分占 40%（盘后横截面口径更严谨）
        score = round(s["score"] * 0.6 + score * 0.4, 1)

    card.update({
        "ok": True,
        "name": name,
        "industry": industry,
        "score": score,
        "grade": _grade(score),
        "dimensions": dims,
        "notes": notes,
        "snapshot_day": day,
        "intraday_day": intra.get("generated") if intra else None,
        "realtime": q,
        "strong": {
            "score": s.get("score"), "grade": s.get("grade"),
            "momentum_20d": s.get("momentum_20d"), "vol_ratio": s.get("vol_ratio"),
            "rs_20d": s.get("rs_20d"),
        } if s else None,
        "streak": streak,
        "industry_limit_ups": n_hot,
        "disclaimer": "评分卡为池内横截面统计口径，仅供研究参考，不构成投资建议。",
    })
    return card
