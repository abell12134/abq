"""swing_hunter 跟踪器：每日收盘后更新全部活跃预测（规则驱动，零 LLM 成本）。

状态机：
  triggered ──T+1 有开盘价──→ holding ──收盘 ≥+10%──→ hit
                                    ├─收盘 ≤−5%(先到)→ stopped
                                    └─满 10 交易日────→ expired
  （invalid 预留给后续 LLM delta「催化证伪」判定，原型不自动触发）

判定口径（与 schema 一致，用户决策：收盘价口径）：
  entry = T+1 开盘价（后复权）；hit/stop 均按收盘价；MFE/MAE 为期内收盘极值辅助。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pandas as pd

from .schema import (
    ACTIVE_STATES,
    HIT_PCT,
    HIT_PCT_TIER2,
    HIT_PCT_TIER3,
    HORIZON_DAYS,
    QUANT,
    STOP_PCT,
    TrackRecord,
)
from . import pattern_mine
from . import store

sys.path.insert(0, str(QUANT))
sys.path.insert(0, str(QUANT / "ops"))


def _load_series(instrument: str, start: str, end: str) -> pd.DataFrame:
    """[start,end] 的后复权开收盘序列；停牌日为 NaN，调用方跳过。"""
    import common as C  # noqa: WPS433
    C.init_qlib()
    from qlib.data import D
    df = D.features(
        [instrument],
        ["$open/$factor", "$close/$factor", "$high/$factor", "$low/$factor"],
        start_time=start, end_time=end,
    )
    if df is None or df.empty:
        return pd.DataFrame(columns=["open", "close", "high", "low"])
    try:
        sub = df.xs(instrument, level="instrument").sort_index()
    except KeyError:
        return pd.DataFrame(columns=["open", "close", "high", "low"])
    sub.columns = ["open", "close", "high", "low"]
    return sub


def _hit_tier(ret: float) -> int:
    if ret >= HIT_PCT_TIER3:
        return 3
    if ret >= HIT_PCT_TIER2:
        return 2
    if ret >= HIT_PCT:
        return 1
    return 0


def update_record(rec: TrackRecord, today: str,
                  series: pd.DataFrame | None = None) -> TrackRecord:
    """推进单条记录到 today（含）。幂等：重复跑同日结果一致。"""
    if rec.state not in ACTIVE_STATES:
        return rec
    if series is None:
        start = (pd.Timestamp(rec.pred_date) - pd.Timedelta(days=5)).strftime("%Y-%m-%d")
        series = _load_series(rec.instrument, start, today)
    if series.empty:
        rec.notes.append(f"{today}: 无行情数据，跟踪暂停一日")
        rec.notes = rec.notes[-20:]
        return rec

    # --- triggered：等待 T+1 开盘入场 ---
    if rec.state == "triggered" and rec.entry_price is None:
        after = series[series.index > pd.Timestamp(rec.pred_date)]
        valid_open = after["open"].dropna()
        if valid_open.empty:
            return rec  # T+1 尚未到来或停牌
        rec.entry_date = valid_open.index[0].strftime("%Y-%m-%d")
        rec.entry_price = round(float(valid_open.iloc[0]), 4)
        rec.state = "holding"

    if rec.entry_price is None or rec.entry_date is None:
        return rec

    # --- holding：逐日收盘判定（从入场日到 today） ---
    entry = rec.entry_price
    span = series[(series.index >= pd.Timestamp(rec.entry_date))
                  & (series.index <= pd.Timestamp(today))]
    seen_dates = {d.get("date") for d in rec.daily}
    rec.state = "holding"
    for dt_idx, row in span.iterrows():
        date_s = dt_idx.strftime("%Y-%m-%d")
        if date_s in seen_dates:
            continue
        close = row.get("close")
        if pd.isna(close) or close is None:
            continue  # 停牌日不计入持有天数
        ret = float(close) / entry - 1.0
        rec.daily.append({"date": date_s, "close": round(float(close), 4),
                          "ret": round(ret, 4)})
        seen_dates.add(date_s)

        rec.mfe = ret if rec.mfe is None else max(rec.mfe, ret)
        rec.mae = ret if rec.mae is None else min(rec.mae, ret)
        tier = _hit_tier(ret)
        if tier > rec.hit_tier:
            rec.hit_tier = tier
            rec.notes.append(f"{date_s}: 收盘达 +{ret*100:.1f}%，命中 T{tier}")

        if ret >= HIT_PCT:
            rec.state, rec.result = "hit", "hit"
            rec.result_date, rec.result_return = date_s, round(ret, 4)
            break
        if ret <= -STOP_PCT:
            rec.state, rec.result = "stopped", "stopped"
            rec.result_date, rec.result_return = date_s, round(ret, 4)
            rec.notes.append(f"{date_s}: 收盘跌穿 -5% 止损线（T+1 下隔夜风险已计价）")
            break

    rec.days_held = len(rec.daily)
    if rec.state == "holding" and rec.days_held >= HORIZON_DAYS:
        last = rec.daily[-1]
        rec.state, rec.result = "expired", "expired"
        rec.result_date = last["date"]
        rec.result_return = last["ret"]
        rec.notes.append(f"{last['date']}: 满 {HORIZON_DAYS} 日未达标，到期结算 "
                         f"{last['ret']*100:+.1f}%")
    rec.notes = rec.notes[-20:]
    return rec


def run_tracking(today: str) -> dict[str, Any]:
    """更新全部活跃记录并刷新 catalog。返回变更摘要。"""
    active = store.all_active_records()
    summary = {"today": today, "tracked": 0, "entered": 0, "hit": 0,
               "stopped": 0, "expired": 0, "unchanged": 0, "details": []}
    for rec in active:
        before = (rec.state, rec.days_held, rec.hit_tier)
        rec = update_record(rec, today)
        store.upsert_record(rec)
        summary["tracked"] += 1
        after = (rec.state, rec.days_held, rec.hit_tier)
        if rec.state in {"hit", "stopped", "expired"} and before[0] != rec.state:
            summary[rec.result or "expired"] += 1
            summary["details"].append(
                f"{rec.instrument} {rec.name} → {rec.result} "
                f"({rec.result_return if rec.result_return is not None else 0:+.1%})")
            if rec.result == "hit":
                pred_dict = _prediction_for_record(rec)
                pattern_mine.mine_from_hit(rec, pred_dict)
        elif before != after:
            if before[0] == "triggered" and rec.state == "holding":
                summary["entered"] += 1
                summary["details"].append(
                    f"{rec.instrument} {rec.name} 入场 @{rec.entry_price}")
            else:
                summary["unchanged"] += 1
        else:
            summary["unchanged"] += 1
    store.update_catalog(today)
    return summary


def _prediction_for_record(rec: TrackRecord) -> dict[str, Any] | None:
    """从当日预测文件取该票 prediction dict（供模式挖掘）。"""
    from .schema import read_predictions
    pf = read_predictions(rec.pred_date)
    if not pf:
        return None
    for p in pf.predictions:
        if p.instrument == rec.instrument:
            return p.to_dict()
    return None
