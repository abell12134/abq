"""L2 portfolio settlement — three independent adjudications (paper metrics)."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from agent.core.caliber import CALIBER
from agent.settlement.settle import PriceSeries, load_price_series_qlib, resolve_benchmark_id, _ordered_days


def _portfolio_daily_rets(
    constituents: list[dict[str, Any]],
    entry_date: str,
    resolve_day: str,
) -> pd.Series:
    """Equal or given weights; close-to-close from entry_date to resolve_day."""
    frames = []
    weights = []
    for c in constituents:
        inst = str(c["instrument"]).upper()
        w = float(c.get("weight") or 0)
        if w <= 0:
            continue
        series = load_price_series_qlib(inst, entry_date, resolve_day)
        closes = pd.Series(series.close, dtype=float).sort_index()
        closes = closes.loc[(closes.index >= entry_date) & (closes.index <= resolve_day)]
        rets = closes.pct_change()
        frames.append(rets.rename(inst))
        weights.append(w)
    if not frames:
        return pd.Series(dtype=float)
    mat = pd.concat(frames, axis=1).dropna(how="all")
    w = np.array(weights, dtype=float)
    w = w / w.sum()
    # align columns order
    port = mat.fillna(0.0).values @ w
    return pd.Series(port, index=mat.index)


def settle_l2_target(
    *,
    pred: dict[str, Any],
    asof: str,
) -> dict[str, Any]:
    """Three binary adjudications: excess / target / constraints."""
    claim = pred["claim"]
    constituents = claim.get("constituents") or []
    horizon = int(pred["horizon"])
    pred_date = pred["pred_date"]
    bench_id = resolve_benchmark_id(pred["benchmark"])

    # find entry = first day after pred with any constituent open
    start = (pd.Timestamp(pred_date) - pd.Timedelta(days=5)).strftime("%Y-%m-%d")
    # use first constituent to discover calendar
    probe = load_price_series_qlib(
        str(constituents[0]["instrument"]).upper() if constituents else bench_id,
        start,
        asof,
    )
    after = _ordered_days(probe, pred_date, asof)
    entry_date = None
    for d in after:
        if probe.open.get(d) and probe.open[d] == probe.open[d]:
            entry_date = d
            break
    if entry_date is None:
        return {"status": "pending", "ready": False, "reason": "waiting_entry"}

    # need horizon trading days of portfolio returns
    rets = _portfolio_daily_rets(constituents, entry_date, asof)
    rets = rets[rets.index >= entry_date]
    if len(rets) < horizon:
        return {
            "status": "pending",
            "ready": False,
            "reason": "waiting_horizon",
            "entry_date": entry_date,
            "held_days": int(len(rets)),
        }

    span = rets.iloc[:horizon]
    resolve_day = str(span.index[-1])
    nav = (1.0 + span).cumprod()
    total_ret = float(nav.iloc[-1] - 1.0)
    # annualize by trading days
    ann = (1.0 + total_ret) ** (252.0 / horizon) - 1.0
    # max drawdown
    peak = nav.cummax()
    dd = float(((nav - peak) / peak).min())
    vol = float(span.std(ddof=1) * np.sqrt(252)) if len(span) > 1 else 0.0

    bench = load_price_series_qlib(bench_id, entry_date, resolve_day)
    b0 = bench.close.get(entry_date)
    b1 = bench.close.get(resolve_day)
    if not (b0 and b1 and b0 > 0 and b1 > 0):
        return {"status": "pending", "ready": False, "reason": "bench_missing", "entry_date": entry_date}
    bench_ret = float(b1) / float(b0) - 1.0
    excess = total_ret - bench_ret

    target = float(claim.get("target_ann_return") or 0)
    max_dd = float(claim.get("max_drawdown") or 1)
    max_vol = float(claim.get("max_vol") or 10)

    adj_excess = excess > 0
    adj_target = ann >= target
    adj_constraints = (abs(dd) <= max_dd) and (vol <= max_vol)

    return {
        "status": "resolved",
        "ready": True,
        "settlement_caliber": pred.get("settlement_caliber") or CALIBER,
        "entry_date": entry_date,
        "resolve_at": resolve_day,
        "portfolio_return": round(total_ret, 6),
        "ann_return": round(ann, 6),
        "bench_ret": round(bench_ret, 6),
        "excess_return": round(excess, 6),
        "max_drawdown": round(dd, 6),
        "ann_vol": round(vol, 6),
        "adjudication": {
            "excess": adj_excess,
            "target": adj_target,
            "constraints": adj_constraints,
        },
        # L2 has no single hit; expose all-pass for scoreboard convenience
        "hit": bool(adj_excess and adj_target and adj_constraints),
        "paper_metrics": True,
    }
