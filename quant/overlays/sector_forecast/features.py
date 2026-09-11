"""行业×日特征面板：qlib 池内等权，T 日收盘可见，前向标签从 T+1 起算。"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .schema import (
    BENCHMARK,
    FEATURE_COLS,
    FEATURE_WARMUP,
    HORIZON_10,
    HORIZON_20,
    LOOKBACK_DAYS,
    MIN_STOCKS,
)

log = logging.getLogger(__name__)

QUANT = Path(__file__).resolve().parents[2]
_LIMIT_TOL = 0.002


def _limit_pct(inst: str) -> float:
    code = inst[2:]
    if inst.startswith("BJ") or code.startswith(("8", "4")):
        return 0.30
    if code.startswith(("688", "689", "300", "301")):
        return 0.20
    return 0.10


def _fwd_sum(s: pd.Series, horizon: int) -> pd.Series:
    """sum(s[t+1] … s[t+horizon])，不含当日。"""
    c = s.cumsum()
    return c.shift(-horizon) - c


def _mkt_temp(pct_up: float, limit_share: float) -> float:
    score = 50.0 + min(limit_share * 800.0, 25.0) + (pct_up - 0.5) * 40.0
    return float(max(0.0, min(100.0, score)))


def fetch_stock_rets(instruments: list[str], start: str, end: str) -> pd.DataFrame | None:
    sys.path.insert(0, str(QUANT / "ops"))
    import common as C
    C.reset_qlib()
    from qlib.data import D
    fields = ["$close/$factor", "Ref($close/$factor, 1)"]
    df = D.features(instruments, fields, start_time=start, end_time=end)
    if df is None or df.empty:
        return None
    df.columns = ["close", "prev_close"]
    df = df.dropna(subset=["close", "prev_close"])
    df = df[df["prev_close"] > 0]
    if df.empty:
        return None
    df = df.copy()
    df["ret"] = df["close"] / df["prev_close"] - 1
    return df


def fetch_bench_ret(start: str, end: str, bench: str = BENCHMARK) -> pd.Series:
    sys.path.insert(0, str(QUANT / "ops"))
    import common as C
    C.reset_qlib()
    from qlib.data import D
    df = D.features([bench], ["$close", "Ref($close, 1)"], start_time=start, end_time=end)
    if df is None or df.empty:
        return pd.Series(dtype=float)
    df.columns = ["close", "prev"]
    df = df.droplevel("instrument").dropna()
    df = df[df["prev"] > 0]
    s = df["close"] / df["prev"] - 1
    s.index = pd.Index([str(x)[:10] for x in s.index], name="day")
    return s.astype(float)


def build_panel(end_day: str, lookback: int = LOOKBACK_DAYS,
                pool: list[dict] | None = None) -> pd.DataFrame:
    """返回行业×日面板，含特征与 10/20 日超额标签（末日标签可为 NaN）。"""
    sys.path.insert(0, str(QUANT / "ops"))
    sys.path.insert(0, str(QUANT))
    import common as C
    from overlays.market_board import pool as pool_mod

    cal = [str(pd.Timestamp(d))[:10] for d in C.calendar()]
    if end_day not in cal:
        raise ValueError(f"{end_day} 非交易日")
    end_idx = cal.index(end_day)
    start_idx = max(0, end_idx - lookback - FEATURE_WARMUP)
    start_day = cal[start_idx]

    pool = pool if pool is not None else pool_mod.load_pool(end_day)
    if not pool:
        raise ValueError("空池子")
    meta = {p["instrument"]: p for p in pool}
    instruments = [p["instrument"] for p in pool]

    raw = fetch_stock_rets(instruments, start_day, end_day)
    if raw is None:
        raise RuntimeError("qlib 股票收益为空")

    df = raw.reset_index()
    # qlib 索引名可能是 instrument / datetime
    colmap = {c.lower(): c for c in df.columns}
    inst_col = colmap.get("instrument", df.columns[0])
    day_col = colmap.get("datetime", df.columns[1])
    df["instrument"] = df[inst_col].astype(str)
    df["day"] = df[day_col].map(lambda x: str(x)[:10])
    df["industry"] = df["instrument"].map(lambda i: (meta.get(i) or {}).get("industry") or "未知")
    df = df[df["industry"] != "未知"]
    df["lim"] = df["instrument"].map(_limit_pct)
    df["limit_up"] = df["ret"] >= (df["lim"] - _LIMIT_TOL)
    df["up"] = df["ret"] > 0.0005

    g = df.groupby(["industry", "day"], sort=False).agg(
        n_stocks=("instrument", "nunique"),
        eq_ret=("ret", "mean"),
        pct_up=("up", "mean"),
        limit_up_share=("limit_up", "mean"),
        ret_std=("ret", "std"),
    ).reset_index()
    g = g[g["n_stocks"] >= MIN_STOCKS]

    mkt = df.groupby("day", sort=False).agg(
        mkt_eq_ret=("ret", "mean"),
        mkt_pct_up=("up", "mean"),
        mkt_limit_up_share=("limit_up", "mean"),
    )
    mkt["mkt_temp"] = [
        _mkt_temp(float(a), float(b))
        for a, b in zip(mkt["mkt_pct_up"], mkt["mkt_limit_up_share"])
    ]
    mkt["mkt_ret_5d"] = mkt["mkt_eq_ret"].rolling(5, min_periods=5).sum()

    bench = fetch_bench_ret(start_day, end_day)
    g["bench_ret"] = g["day"].map(bench)

    g = g.sort_values(["industry", "day"])
    parts: list[pd.DataFrame] = []
    for _ind, x in g.groupby("industry", sort=False):
        x = x.copy()
        x["ret_1d"] = x["eq_ret"]
        x["ret_5d"] = x["eq_ret"].rolling(5, min_periods=5).sum()
        x["ret_10d"] = x["eq_ret"].rolling(10, min_periods=10).sum()
        x["ret_20d"] = x["eq_ret"].rolling(20, min_periods=20).sum()
        x["bench_5d"] = x["bench_ret"].rolling(5, min_periods=5).sum()
        x["bench_10d"] = x["bench_ret"].rolling(10, min_periods=10).sum()
        x["bench_20d"] = x["bench_ret"].rolling(20, min_periods=20).sum()
        x["rs_5d"] = x["ret_5d"] - x["bench_5d"]
        x["rs_10d"] = x["ret_10d"] - x["bench_10d"]
        x["rs_20d"] = x["ret_20d"] - x["bench_20d"]
        x["accel"] = x["ret_5d"] - x["ret_20d"] / 4.0
        x["vol_20"] = x["eq_ret"].rolling(20, min_periods=10).std()
        x["crowded"] = x["ret_5d"].clip(lower=0)
        x["fwd_10"] = _fwd_sum(x["eq_ret"], HORIZON_10)
        x["fwd_20"] = _fwd_sum(x["eq_ret"], HORIZON_20)
        x["bench_fwd_10"] = _fwd_sum(x["bench_ret"], HORIZON_10)
        x["bench_fwd_20"] = _fwd_sum(x["bench_ret"], HORIZON_20)
        x["excess_10"] = x["fwd_10"] - x["bench_fwd_10"]
        x["excess_20"] = x["fwd_20"] - x["bench_fwd_20"]
        x["y_10"] = np.where(x["excess_10"].isna(), np.nan, (x["excess_10"] > 0).astype(float))
        x["y_20"] = np.where(x["excess_20"].isna(), np.nan, (x["excess_20"] > 0).astype(float))
        parts.append(x)
    panel = pd.concat(parts, ignore_index=True)
    panel = panel.merge(mkt.reset_index(), on="day", how="left")
    panel["ret_std"] = panel["ret_std"].fillna(0.0)
    return panel


def feature_matrix(panel: pd.DataFrame, dropna: bool = True) -> pd.DataFrame:
    cols = ["industry", "day", *FEATURE_COLS, "excess_10", "excess_20", "y_10", "y_20",
            "eq_ret"]
    seen: list[str] = []
    for c in cols:
        if c in panel.columns and c not in seen:
            seen.append(c)
    out = panel[seen].copy()
    if dropna:
        out = out.dropna(subset=[c for c in FEATURE_COLS if c in out.columns])
    return out


def row_to_drivers(row: dict[str, Any] | pd.Series) -> dict[str, Any]:
    keys = ["ret_5d", "ret_10d", "ret_20d", "rs_10d", "accel", "pct_up",
            "limit_up_share", "crowded", "n_stocks"]
    out = {}
    for k in keys:
        v = row[k] if isinstance(row, pd.Series) else row.get(k)
        if v is None or (isinstance(v, float) and (v != v)):
            continue
        out[k] = round(float(v), 4) if k != "n_stocks" else int(v)
    return out
