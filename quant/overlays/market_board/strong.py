"""强势个股评分（P1 三维版：动量/量能/相对强度，横截面分位打分）。

设计文档五维口径（动量25 + 量能20 + 强度20 + 题材20 + 资金15）中，
题材热度与主力净流入依赖外延数据源（P3）；P1 先以三维重归一权重
（动量40 / 量能30 / 强度30），评分卡标注"P1 口径"。

分级：S≥80 / A≥65 / B≥50 / C<50（与设计文档一致）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from .limit_stats import LimitStatsResult

MOM_WINDOW = 20          # 动量窗口（交易日）
VOL_SHORT = 5            # 量比短窗
VOL_LONG = 20            # 量比长窗
W_MOM, W_VOL, W_RS = 0.40, 0.30, 0.30


@dataclass
class StrongResult:
    stocks: list[dict[str, Any]] = field(default_factory=list)
    weights: dict[str, float] = field(default_factory=lambda: {
        "momentum": W_MOM, "volume_ratio": W_VOL, "relative_strength": W_RS})
    caliber: str = "P1 三维口径（动量/量能/强度）；题材与资金维度 P3 接入"

    def to_dict(self) -> dict[str, Any]:
        return {"stocks": self.stocks, "weights": self.weights, "caliber": self.caliber}


def _grade(score: float) -> str:
    if score >= 80:
        return "S"
    if score >= 65:
        return "A"
    if score >= 50:
        return "B"
    return "C"


def _pct_rank(s: pd.Series) -> pd.Series:
    """横截面分位数 → 0~100。"""
    return s.rank(pct=True) * 100


def compute(stats: LimitStatsResult, top_n: int = 50) -> StrongResult:
    res = StrongResult()
    if stats.rows is None or not stats.days:
        return res

    df = stats.rows
    latest = stats.days[-1]
    window_days = stats.days[-(VOL_LONG + 1):]
    w = df[df["day"].isin(window_days)]

    rows: list[dict[str, Any]] = []
    for inst, g in w.groupby("instrument"):
        g = g.sort_values("day")
        if len(g) < VOL_LONG:
            continue
        name = g["name"].iloc[-1]
        industry = g["industry"].iloc[-1]
        closes = g["close"].values
        vols = g["volume"].values
        rets = g["ret"].values

        mom = closes[-1] / closes[-MOM_WINDOW - 1] - 1 if len(closes) > MOM_WINDOW else None
        v_short = vols[-VOL_SHORT:].mean()
        v_long = vols[-VOL_LONG:].mean()
        vol_ratio = float(v_short / v_long) if v_long > 0 else None
        if mom is None or vol_ratio is None or v_short <= 0:
            continue
        rows.append({
            "instrument": inst, "name": name, "industry": industry,
            "momentum_20d": float(mom),
            "vol_ratio": vol_ratio,
            "ret_20d_pool_bench": None,   # 占位，下面统一算
            "ret": float(rets[-1]),
            "close": float(closes[-1]),
            "streak": int(g["streak"].iloc[-1]),
            "limit_up": bool(g["limit_up"].iloc[-1]),
        })

    if not rows:
        return res

    # 相对强度：个股 20 日动量 − 池内等权 20 日累计收益（自洽基准，免查指数）
    bench = 1.0
    bench_days = stats.days[-MOM_WINDOW:]
    eq_map = {d.day: d.eq_ret for d in stats.daily}
    for d in bench_days:
        bench *= 1 + eq_map.get(d, 0.0)
    pool_ret_20d = bench - 1

    out = pd.DataFrame(rows)
    out["rs_20d"] = out["momentum_20d"] - pool_ret_20d

    out["s_mom"] = _pct_rank(out["momentum_20d"])
    out["s_vol"] = _pct_rank(out["vol_ratio"].clip(upper=out["vol_ratio"].quantile(0.98)))
    out["s_rs"] = _pct_rank(out["rs_20d"])
    out["score"] = (out["s_mom"] * W_MOM + out["s_vol"] * W_VOL + out["s_rs"] * W_RS).round(1)
    out["grade"] = out["score"].map(_grade)
    out = out.sort_values("score", ascending=False).head(top_n)

    for _, r in out.iterrows():
        res.stocks.append({
            "instrument": r["instrument"], "name": r["name"], "industry": r["industry"],
            "score": float(r["score"]), "grade": r["grade"],
            "dimensions": {
                "momentum": round(float(r["s_mom"]), 1),
                "volume_ratio": round(float(r["s_vol"]), 1),
                "relative_strength": round(float(r["s_rs"]), 1),
            },
            "momentum_20d": round(float(r["momentum_20d"]), 4),
            "vol_ratio": round(float(r["vol_ratio"]), 2),
            "rs_20d": round(float(r["rs_20d"]), 4),
            "pool_ret_20d": round(pool_ret_20d, 4),
            "close": r["close"], "ret": round(float(r["ret"]), 5),
            "streak": int(r["streak"]), "limit_up": bool(r["limit_up"]),
        })
    return res
