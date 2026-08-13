"""Deterministic L1 settlement — excess return vs benchmark.

Caliber v1 / v1.1:
  entry = first trading day AFTER pred_date with valid open (T+1 open, hfq)
  horizon N = count of trading days with valid close from entry (skip halt NaN)
  exit = N-th such close
  stock_ret = exit_close / entry_open - 1
  bench_ret = bench_close(exit) / bench_close(entry) - 1   (same calendar window)
  excess = stock_ret - bench_ret
  direction hit: sign(excess) matches claim direction (0 excess → miss)
  interval hit: excess ∈ [low, high]
  v1.1: annotate limit-up/down; ST/delist → early settle on event day
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal

import pandas as pd

from agent.core.caliber import BENCHMARK_MAP, CALIBER
from agent.core.events import EventFlags, annotate_outcome, flags_from_closes


Direction = Literal["up", "down"]


@dataclass
class PriceSeries:
    """open/close indexed by trading date string YYYY-MM-DD; NaN = halt."""

    open: dict[str, float]
    close: dict[str, float]


LoadSeriesFn = Callable[[str, str, str], PriceSeries]
# (instrument, start, end) -> PriceSeries


def resolve_benchmark_id(benchmark: str) -> str:
    return BENCHMARK_MAP.get(benchmark, benchmark)


def _ordered_days(series: PriceSeries, start_exclusive: str | None, end: str) -> list[str]:
    days = sorted(set(series.open) | set(series.close))
    out = []
    for d in days:
        if start_exclusive is not None and d <= start_exclusive:
            continue
        if d > end:
            break
        out.append(d)
    return out


def settle_direction(
    *,
    claim_direction: Direction,
    pred_date: str,
    horizon: int,
    stock: PriceSeries,
    bench: PriceSeries,
    asof: str,
    caliber: str = CALIBER,
    flags: EventFlags | None = None,
    instrument: str = "",
) -> dict[str, Any]:
    """Settle or advance a direction claim as of `asof`. Idempotent structure."""
    after = _ordered_days(stock, pred_date, asof)
    entry_date = None
    entry_price = None
    for d in after:
        px = stock.open.get(d)
        if px is not None and px == px and px > 0:
            entry_date, entry_price = d, float(px)
            break
    if entry_date is None:
        return {
            "status": "pending",
            "ready": False,
            "reason": "waiting_entry",
            "settlement_caliber": caliber,
        }

    ev = flags or EventFlags()
    if not flags and instrument:
        ev = flags_from_closes(instrument, stock.close)

    # trading days with valid close from entry
    held: list[str] = []
    for d in _ordered_days(stock, None, asof):
        if d < entry_date:
            continue
        c = stock.close.get(d)
        if c is not None and c == c and c > 0:
            held.append(d)
        if len(held) >= horizon:
            break

    early_reason = None
    event_day = ev.delist_event_day or ev.st_event_day
    if event_day and event_day >= entry_date:
        # early settle if event falls on or after entry and we have a close
        c_ev = stock.close.get(event_day)
        if c_ev is not None and c_ev == c_ev and c_ev > 0 and event_day <= asof:
            # truncate held to event day inclusive
            held_early = [d for d in held if d <= event_day]
            if event_day not in held_early:
                held_early.append(event_day)
                held_early.sort()
            if held_early:
                held = held_early
                early_reason = "delist" if ev.delist_event_day == event_day else "st"
                horizon_eff = len(held)
            else:
                horizon_eff = horizon
        else:
            horizon_eff = horizon
    else:
        horizon_eff = horizon

    need = horizon if early_reason is None else min(horizon, max(1, len(held)))
    if early_reason is None and len(held) < horizon:
        return {
            "status": "pending",
            "ready": False,
            "reason": "waiting_horizon",
            "entry_date": entry_date,
            "entry_price": entry_price,
            "held_days": len(held),
            "settlement_caliber": caliber,
        }
    if early_reason and not held:
        return {
            "status": "pending",
            "ready": False,
            "reason": "waiting_st_event_close",
            "entry_date": entry_date,
            "entry_price": entry_price,
            "settlement_caliber": caliber,
        }

    resolve_day = held[-1] if early_reason else held[horizon - 1]
    exit_price = float(stock.close[resolve_day])
    stock_ret = exit_price / entry_price - 1.0

    b_entry = bench.close.get(entry_date)
    b_exit = bench.close.get(resolve_day)
    if b_entry is None or b_exit is None or not (b_entry > 0 and b_exit > 0):
        return {
            "status": "pending",
            "ready": False,
            "reason": "bench_missing",
            "entry_date": entry_date,
            "entry_price": entry_price,
            "settlement_caliber": caliber,
        }
    bench_ret = float(b_exit) / float(b_entry) - 1.0
    excess = stock_ret - bench_ret

    if excess > 0:
        sign = 1
    elif excess < 0:
        sign = -1
    else:
        sign = 0
    want = 1 if claim_direction == "up" else -1
    hit = sign == want and sign != 0

    raw = {
        "status": "resolved",
        "ready": True,
        "settlement_caliber": caliber,
        "entry_date": entry_date,
        "entry_price": entry_price,
        "resolve_at": resolve_day,
        "exit_price": exit_price,
        "stock_ret": round(stock_ret, 6),
        "bench_ret": round(bench_ret, 6),
        "excess_return": round(excess, 6),
        "hit": hit,
        "claim_direction": claim_direction,
        "horizon": horizon,
        "held_days": len(held) if early_reason else horizon,
        "horizon_effective": need if early_reason else horizon,
    }
    return annotate_outcome(raw, ev, early_reason=early_reason)


def settle_interval(
    *,
    low: float,
    high: float,
    pred_date: str,
    horizon: int,
    stock: PriceSeries,
    bench: PriceSeries,
    asof: str,
    caliber: str = CALIBER,
    flags: EventFlags | None = None,
    instrument: str = "",
) -> dict[str, Any]:
    base = settle_direction(
        claim_direction="up",  # unused for hit
        pred_date=pred_date,
        horizon=horizon,
        stock=stock,
        bench=bench,
        asof=asof,
        caliber=caliber,
        flags=flags,
        instrument=instrument,
    )
    if not base.get("ready"):
        return base
    excess = float(base["excess_return"])
    hit = low <= excess <= high
    width = high - low
    base.update(
        {
            "hit": hit,
            "pic": hit,
            "width": width,
            "claim_direction": None,
            "interval": [low, high],
        }
    )
    return base


def load_price_series_qlib(instrument: str, start: str, end: str) -> PriceSeries:
    """Load hfq open/close via Qlib ($open/$factor, $close/$factor)."""
    import sys
    from pathlib import Path

    quant = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(quant / "ops"))
    import common as C  # noqa: WPS433

    C.init_qlib()
    from qlib.data import D

    df = D.features(
        [instrument],
        ["$open/$factor", "$close/$factor"],
        start_time=start,
        end_time=end,
    )
    opens: dict[str, float] = {}
    closes: dict[str, float] = {}
    if df is None or df.empty:
        return PriceSeries(open=opens, close=closes)
    try:
        sub = df.xs(instrument, level="instrument").sort_index()
    except KeyError:
        return PriceSeries(open=opens, close=closes)
    for ts, row in sub.iterrows():
        d = pd.Timestamp(ts).strftime("%Y-%m-%d")
        o, c = row.iloc[0], row.iloc[1]
        if o == o and o is not None:
            opens[d] = float(o)
        if c == c and c is not None:
            closes[d] = float(c)
    return PriceSeries(open=opens, close=closes)


def settle_prediction(pred: dict[str, Any], asof: str) -> dict[str, Any]:
    """Settle one ledger prediction dict; returns outcome patch."""
    if pred.get("level") == "L2" or pred.get("claim_type") == "target":
        from agent.settlement.l2_settle import settle_l2_target

        return settle_l2_target(pred=pred, asof=asof)

    from agent.core.events import detect_st_from_name, flags_from_closes

    start = (pd.Timestamp(pred["pred_date"]) - pd.Timedelta(days=5)).strftime("%Y-%m-%d")
    end = asof
    inst = pred["object"]
    stock = load_price_series_qlib(inst, start, end)
    bench_id = resolve_benchmark_id(pred["benchmark"])
    bench = load_price_series_qlib(bench_id, start, end)
    flags = flags_from_closes(inst, stock.close)
    # ST / 退市: name hint + optional claim/meta event days
    name = pred.get("object_name") or ""
    meta_st = (pred.get("claim") or {}).get("st_event_day") or (pred.get("error_metrics") or {}).get(
        "st_event_day"
    )
    meta_delist = (pred.get("claim") or {}).get("delist_event_day") or (
        pred.get("error_metrics") or {}
    ).get("delist_event_day")
    if meta_st:
        flags.st_event_day = str(meta_st)
    elif detect_st_from_name(name):
        # If already ST at emit, early-settle on first held close after entry
        # (caller may refine; here mark pred_date+ as event for Critic)
        flags.notes.append(f"标的名称含 ST/退（{name}）；若无精确事件日则按正常 horizon 结算并标注")
    if meta_delist:
        flags.delist_event_day = str(meta_delist)

    claim = pred["claim"]
    caliber = pred.get("settlement_caliber") or CALIBER
    if pred["claim_type"] == "direction":
        return settle_direction(
            claim_direction=claim["direction"],
            pred_date=pred["pred_date"],
            horizon=int(pred["horizon"]),
            stock=stock,
            bench=bench,
            asof=asof,
            caliber=caliber,
            flags=flags,
            instrument=inst,
        )
    if pred["claim_type"] == "interval":
        return settle_interval(
            low=float(claim["low"]),
            high=float(claim["high"]),
            pred_date=pred["pred_date"],
            horizon=int(pred["horizon"]),
            stock=stock,
            bench=bench,
            asof=asof,
            caliber=caliber,
            flags=flags,
            instrument=inst,
        )
    return {"status": "pending", "ready": False, "reason": f"unsupported_claim:{pred['claim_type']}"}
