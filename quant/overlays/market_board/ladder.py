"""连板梯队：按连板数分层 + 梯队存活率（晋级率）。

抢筹强度（集合竞价额/昨成交）依赖竞价数据，P2 接入；P1 字段留 None。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .limit_stats import LimitStatsResult, StockDayRow


@dataclass
class LadderResult:
    tiers: list[dict[str, Any]] = field(default_factory=list)   # 高板→首板
    survival: list[dict[str, Any]] = field(default_factory=list)  # 各板位晋级率
    first_boards: list[dict[str, Any]] = field(default_factory=list)  # 首板名单（折叠展示）

    def to_dict(self) -> dict[str, Any]:
        return {"tiers": self.tiers, "survival": self.survival,
                "first_boards": self.first_boards}


def _row_brief(r: StockDayRow) -> dict[str, Any]:
    return {
        "instrument": r.instrument, "name": r.name, "industry": r.industry,
        "streak": r.streak, "limit_type": r.limit_type, "ret": r.ret,
        "amount": r.amount,
        "grab_strength": None,   # P2：竞价额/昨成交
        "unlock_alert": None,    # P3：解禁标注
    }


def compute(stats: LimitStatsResult, survival_window: int = 10) -> LadderResult:
    res = LadderResult()
    if not stats.stocks_latest or not stats.days:
        return res

    latest = stats.days[-1]
    ups = [r for r in stats.stocks_latest if r.limit_up and r.streak >= 1]
    ups.sort(key=lambda r: (-r.streak, -(r.amount or 0)))

    # 梯队：≥2 板按层展示；首板折叠进 first_boards
    tiers_map: dict[int, list[dict[str, Any]]] = {}
    for r in ups:
        if r.streak >= 2:
            tiers_map.setdefault(r.streak, []).append(_row_brief(r))
        else:
            res.first_boards.append(_row_brief(r))
    res.tiers = [
        {"streak": s, "count": len(rows), "stocks": rows}
        for s, rows in sorted(tiers_map.items(), reverse=True)
    ]

    # 存活率：近 survival_window 日，昨日 N 板 → 今日 N+1 板 的晋级率
    if stats.rows is not None and len(stats.days) >= 2:
        import pandas as pd
        df = stats.rows
        recent_days = stats.days[-(survival_window + 1):]
        agg: dict[int, list[float]] = {}
        for i in range(1, len(recent_days)):
            prev_day, day = recent_days[i - 1], recent_days[i]
            prev = df[(df["day"] == prev_day) & df["limit_up"] & df["streak"] >= 1]
            today = df[(df["day"] == day) & df["limit_up"]][["instrument", "streak"]]
            if prev.empty:
                continue
            today_map = dict(zip(today["instrument"], today["streak"]))
            for s in sorted(set(prev["streak"])):
                cohort = prev[prev["streak"] == s]
                promoted = sum(1 for inst in cohort["instrument"]
                               if today_map.get(inst, 0) >= s + 1)
                agg.setdefault(int(s), []).append(promoted / len(cohort))
        res.survival = [
            {"streak": s, "rate": round(sum(v) / len(v), 3), "samples": len(v)}
            for s, v in sorted(agg.items())
        ]
    return res
