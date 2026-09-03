"""池内涨停/跌停/连板/炸板统计：纯本地 qlib 日线，盘后零外网依赖。

口径（与设计文档 §7 对齐）：
  涨停  ret ≥ _limit_pct(inst) − 0.002   （复用 ops/common 阈值）
  炸板  盘中 high 触板但收盘未封
  一字板 open==close==high==low 且涨停
  T字板  开盘涨停、盘中开过（low < 收盘）、收盘涨停
  换手板 其余收盘涨停
  连板  向前逐日递推收盘涨停天数

复权口径：open/close/high/low 均除 $factor（后复权），避免除权日误判。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

log = logging.getLogger(__name__)

LOOKBACK_DAYS = 45        # 统计窗口（交易日），覆盖 30 日周期 + 连板递推
_LIMIT_TOL = 0.002


def _limit_pct(inst: str) -> float:
    code = inst[2:]
    if inst.startswith("BJ"):
        return 0.30
    if code.startswith(("688", "689", "300", "301")):
        return 0.20
    if code.startswith(("8", "4")):
        return 0.30       # 北交所（SH/SZ 前缀异常情况兜底）
    return 0.10


def _is_st_name(name: str) -> bool:
    return "ST" in (name or "").upper()


def _st_limit(inst: str, name: str) -> float:
    """ST 股主板 5%；创业/科创/北交 ST 仍 20%/30%（规则不变）。"""
    base = _limit_pct(inst)
    if base == 0.10 and _is_st_name(name):
        return 0.05
    return base


@dataclass
class DayStat:
    day: str
    limit_up: int = 0
    limit_down: int = 0
    broken: int = 0                 # 炸板（触板未封）
    up_gt5: int = 0
    down_lt5: int = 0
    up: int = 0                     # 上涨家数
    down: int = 0                   # 下跌家数
    flat: int = 0
    traded: int = 0                 # 当日有成交的池内股票数
    max_streak: int = 0             # 最高连板
    eq_ret: float = 0.0             # 池内等权日收益（温度计/周期用）

    @property
    def broken_rate(self) -> float:
        total = self.broken + self.limit_up
        return round(self.broken / total, 4) if total else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "day": self.day, "limit_up": self.limit_up, "limit_down": self.limit_down,
            "broken": self.broken, "up_gt5": self.up_gt5, "down_lt5": self.down_lt5,
            "up": self.up, "down": self.down, "flat": self.flat,
            "traded": self.traded, "max_streak": self.max_streak,
            "eq_ret": round(self.eq_ret, 5),
            "broken_rate": self.broken_rate,
        }


@dataclass
class StockDayRow:
    instrument: str
    name: str
    industry: str
    day: str
    close: float
    ret: float
    volume: float
    amount: float
    limit_up: bool = False
    limit_down: bool = False
    broken: bool = False
    limit_type: str | None = None    # 一字/T字/换手
    streak: int = 0                  # 截至当日连板数

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument": self.instrument, "name": self.name, "industry": self.industry,
            "day": self.day, "close": round(self.close, 3), "ret": round(self.ret, 5),
            "volume": self.volume, "amount": round(self.amount, 0),
            "limit_up": self.limit_up, "limit_down": self.limit_down,
            "broken": self.broken, "limit_type": self.limit_type, "streak": self.streak,
        }


@dataclass
class LimitStatsResult:
    days: list[str]
    daily: list[DayStat]                       # 按日聚合（时间升序）
    rows: pd.DataFrame | None = None           # 全量明细（instrument×day）
    stocks_latest: list[StockDayRow] = field(default_factory=list)   # 最新日明细
    errors: list[str] = field(default_factory=list)


def _fetch_ohlcv(instruments: list[str], start: str, end: str) -> pd.DataFrame | None:
    """批量取后复权 OHLCV + 成交额；索引 (instrument, datetime)。"""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ops"))
    import common as C
    C.reset_qlib()
    from qlib.data import D
    fields = [
        "$open/$factor", "$close/$factor", "$high/$factor", "$low/$factor",
        "$volume", "$amount",
        "Ref($close/$factor,1)",
    ]
    df = D.features(instruments, fields, start_time=start, end_time=end)
    if df is None or df.empty:
        return None
    df.columns = ["open", "close", "high", "low", "volume", "amount", "prev_close"]
    return df


def compute(pool: list[dict], end_day: str, lookback: int = LOOKBACK_DAYS) -> LimitStatsResult:
    """主入口：池子 × 最近 lookback 个交易日 → 每日聚合 + 最新日明细。"""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ops"))
    import common as C

    res = LimitStatsResult(days=[], daily=[])
    if not pool:
        res.errors.append("空池子")
        return res

    cal = [str(pd.Timestamp(d))[:10] for d in C.calendar()]
    if end_day not in cal:
        res.errors.append(f"{end_day} 非交易日")
        return res
    end_idx = cal.index(end_day)
    start_idx = max(0, end_idx - lookback - 2)
    start_day = cal[start_idx]

    instruments = [p["instrument"] for p in pool]
    meta = {p["instrument"]: p for p in pool}
    try:
        df = _fetch_ohlcv(instruments, start_day, end_day)
    except Exception as e:  # noqa: BLE001
        res.errors.append(f"qlib 行情读取失败: {e}")
        return res
    if df is None:
        res.errors.append("qlib 返回空")
        return res

    df = df.dropna(subset=["close", "prev_close"])
    df = df[df["prev_close"] > 0]
    if df.empty:
        res.errors.append("清洗后无有效行情")
        return res

    df["ret"] = df["close"] / df["prev_close"] - 1

    # 逐票计算涨跌停/类型/连板（向量化主循环 + 每票递推连板）
    all_rows: list[dict[str, Any]] = []
    daily_map: dict[str, DayStat] = {}
    days_in_data = sorted({str(ts)[:10] for ts in df.index.get_level_values("datetime")})
    for d in days_in_data:
        daily_map[d] = DayStat(day=d)

    for inst, g in df.groupby(level="instrument"):
        g = g.droplevel("instrument").sort_index()
        name = meta.get(inst, {}).get("name", "")
        industry = meta.get(inst, {}).get("industry", "未知")
        lim = _st_limit(inst, name)
        streak = 0
        prev_streak = 0
        for ts, r in g.iterrows():
            day = str(ts)[:10]
            ret = float(r["ret"])
            o, c, h, lo = float(r["open"]), float(r["close"]), float(r["high"]), float(r["low"])
            vol = float(r["volume"]) if r["volume"] == r["volume"] else 0.0
            amt = float(r["amount"]) if r["amount"] == r["amount"] else 0.0
            traded = vol > 0
            prev_close = float(r["prev_close"])

            limit_up = traded and ret >= lim - _LIMIT_TOL
            limit_down = traded and ret <= -(lim - _LIMIT_TOL)
            touch = traded and (h / prev_close - 1) >= lim - _LIMIT_TOL
            broken = touch and not limit_up

            limit_type = None
            if limit_up:
                one_word = (abs(o - c) < 1e-9 and abs(c - h) < 1e-9 and abs(h - lo) < 1e-9)
                open_at_limit = (o / prev_close - 1) >= lim - _LIMIT_TOL
                if one_word:
                    limit_type = "一字"
                elif open_at_limit and lo < c * (1 - 1e-6):
                    limit_type = "T字"
                else:
                    limit_type = "换手"
                streak = prev_streak + 1
            else:
                streak = 0

            ds = daily_map.get(day)
            if ds is not None and traded:
                ds.traded += 1
                ds.eq_ret += ret
                if limit_up:
                    ds.limit_up += 1
                    ds.max_streak = max(ds.max_streak, streak)
                if limit_down:
                    ds.limit_down += 1
                if broken:
                    ds.broken += 1
                if ret > 0.0005:
                    ds.up += 1
                elif ret < -0.0005:
                    ds.down += 1
                else:
                    ds.flat += 1
                if ret >= 0.05:
                    ds.up_gt5 += 1
                if ret <= -0.05:
                    ds.down_lt5 += 1

            all_rows.append({
                "instrument": inst, "name": name, "industry": industry, "day": day,
                "close": c, "ret": ret, "volume": vol, "amount": amt,
                "limit_up": limit_up, "limit_down": limit_down, "broken": broken,
                "limit_type": limit_type, "streak": streak,
            })
            prev_streak = streak

    days = sorted(daily_map)
    daily: list[DayStat] = []
    for d in days:
        ds = daily_map[d]
        if ds.traded:
            ds.eq_ret /= ds.traded
        daily.append(ds)

    rows_df = pd.DataFrame(all_rows)
    res.days = days
    res.daily = daily
    res.rows = rows_df

    latest = days[-1] if days else None
    if latest:
        latest_rows = rows_df[rows_df["day"] == latest]
        res.stocks_latest = [
            StockDayRow(**{k: (v.item() if hasattr(v, "item") else v)
                           for k, v in row.items()})
            for row in latest_rows.to_dict("records")
        ]
    return res
