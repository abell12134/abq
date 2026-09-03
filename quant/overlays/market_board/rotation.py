"""板块轮动矩阵：池内按申万一级行业聚合，近 5/10/20 日平均涨幅热力表。

纯本地 qlib 数据（复用 limit_stats 的 rows 明细，45 日窗口内重采样），
不依赖外网。识别资金在池内行业间的切换方向。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from .limit_stats import LimitStatsResult

WINDOWS = (("ret_5d", 5), ("ret_10d", 10), ("ret_20d", 20))
MIN_STOCKS = 3           # 行业内池内股票数下限，少于则不展示


@dataclass
class RotationResult:
    rows: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"rows": self.rows}


def compute(stats: LimitStatsResult) -> RotationResult:
    res = RotationResult()
    if stats.rows is None or not stats.days:
        return res

    df = stats.rows
    # 每行业×每日等权收益
    day_ind = (df.groupby(["industry", "day"])["ret"]
               .mean().reset_index())
    # 每行业覆盖股票数（全窗口去重）
    ind_n = df.groupby("industry")["instrument"].nunique()

    days = stats.days
    rows: list[dict[str, Any]] = []
    for ind, g in day_ind.groupby("industry"):
        n = int(ind_n.get(ind, 0))
        if n < MIN_STOCKS or ind == "未知":
            continue
        g = g.set_index("day")["ret"]
        row: dict[str, Any] = {"industry": ind, "n_stocks": n}
        for col, w in WINDOWS:
            wd = days[-w:]
            vals = [g.get(d) for d in wd]
            vals = [v for v in vals if v is not None and v == v]
            row[col] = round(float(pd.Series(vals).sum()), 4) if vals else None
        if all(row[c] is None for c, _ in WINDOWS):
            continue
        # 动量切换信号：5日强而20日弱（新资金流入）或反之
        r5, r20 = row.get("ret_5d"), row.get("ret_20d")
        if r5 is not None and r20 is not None:
            if r5 > 0.02 and r20 < 0:
                row["shift"] = "流入↑"
            elif r5 < -0.02 and r20 > 0:
                row["shift"] = "流出↓"
            else:
                row["shift"] = ""
        else:
            row["shift"] = ""
        rows.append(row)

    rows.sort(key=lambda x: -(x.get("ret_5d") or -9))
    res.rows = rows
    return res
