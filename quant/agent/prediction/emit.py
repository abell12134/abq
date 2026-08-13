"""Emit L1 direction predictions from daily LGBM signal CSV."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from agent.core import store
from agent.core.caliber import (
    CALIBER,
    DEFAULT_BENCHMARK,
    FEATURE_VERSION,
    HORIZON_DEFAULT,
    SHADOW_MIN_N,
    STRATEGY_VERSION,
    TOP_K_EMIT,
)
from agent.core.scorecard import scorecard_for

QUANT = Path(__file__).resolve().parents[2]
TZ = ZoneInfo("Asia/Shanghai")


def _signals_path(day: str) -> Path:
    return QUANT / "data" / "signals" / f"{day}.csv"


def _pred_id(day: str, instrument: str, direction: str) -> str:
    return f"pred_{day.replace('-', '')}_L1_{instrument}_{direction}"


def _content_hash(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def system_mode(path: Path | None = None) -> str:
    """shadow until enough non-synthetic resolved direction samples."""
    n = store.count_resolved(STRATEGY_VERSION, "direction", path=path)
    mode = "graduated" if n >= SHADOW_MIN_N else "shadow"
    prev = store.get_meta("mode", path=path)
    store.set_meta("mode", mode, path=path)
    if store.get_meta("shadow_start", path=path) is None:
        store.set_meta("shadow_start", datetime.now(TZ).strftime("%Y-%m-%d"), path=path)
    # promote leftover cold-start labels once graduated
    if mode == "graduated" and prev != "graduated":
        for p in store.list_predictions(status="shadow", path=path, include_synthetic=False):
            if not p.get("outcome"):
                p["status"] = "pending"
                p.setdefault("critic_notes", []).append("系统已毕业：shadow → pending")
                store.upsert_prediction(p, path=path)
    return mode


def score_to_raw_confidence(score: float, scores: pd.Series) -> float:
    """Map score to (0.5, 1) via rank percentile within the batch."""
    if scores.empty:
        return 0.55
    rank = float((scores <= score).mean())  # 0..1
    # stretch away from 0.5
    return round(0.5 + 0.45 * abs(rank - 0.5) * 2, 4)


def emit_from_signals(
    day: str,
    *,
    top_k: int = TOP_K_EMIT,
    horizon: int = HORIZON_DEFAULT,
    benchmark: str = DEFAULT_BENCHMARK,
    db_path: Path | None = None,
    allow_latest_fallback: bool = False,
) -> dict[str, Any]:
    from agent.prediction.critic_assert import assert_prediction_record, gate_emit_or_raise

    path = _signals_path(day)
    used_fallback = False
    if not path.exists():
        alt = QUANT / "data" / "signals" / "latest_pred.csv"
        if not alt.exists():
            return {"ok": False, "error": f"no signals for {day}", "emitted": 0}
        if not allow_latest_fallback:
            return {
                "ok": False,
                "error": f"缺少 {path.name}；拒绝 latest_pred 回退（lookahead 门）",
                "emitted": 0,
            }
        path = alt
        used_fallback = True
        df = pd.read_csv(alt)
    else:
        df = pd.read_csv(path)

    try:
        gate = gate_emit_or_raise(
            day,
            path,
            allow_latest_fallback=used_fallback or allow_latest_fallback,
        )
    except AssertionError as exc:
        return {"ok": False, "error": f"lookahead_assert: {exc}", "emitted": 0}

    if "instrument" not in df.columns or "score" not in df.columns:
        return {"ok": False, "error": "signals missing instrument/score", "emitted": 0}

    df = df.sort_values("score", ascending=False).reset_index(drop=True)
    scores = df["score"].astype(float)
    mode = system_mode(db_path)
    status = "shadow" if mode == "shadow" else "pending"

    resolved = [
        p
        for p in store.list_predictions(status="resolved", path=db_path, include_synthetic=False)
        if str(p.get("strategy_version") or "").startswith(STRATEGY_VERSION)
    ]
    sc = scorecard_for(resolved, claim_type="direction")

    longs = df.head(top_k)
    shorts = df.tail(top_k)
    emitted = 0
    blocked = 0
    now = datetime.now(TZ).isoformat()
    critic_warns = list(gate.warnings)

    batches = [
        (longs, "up"),
        (shorts, "down"),
    ]
    for batch, direction in batches:
        for _, row in batch.iterrows():
            inst = str(row["instrument"]).upper()
            score = float(row["score"])
            raw = score_to_raw_confidence(score, scores)
            if sc["sample_ok"] and sc["hit_rate"] is not None:
                conf = round(0.7 * raw + 0.3 * float(sc["hit_rate"]), 4)
            else:
                conf = raw
            pid = _pred_id(day, inst, direction)
            feat = {
                "feature_version": FEATURE_VERSION,
                "pit_timestamp": f"{day}T15:00:00+08:00",
                "content_hash": _content_hash(
                    {"day": day, "instrument": inst, "score": score, "direction": direction}
                ),
                "snapshot_ref": f"signals://{day}.csv#{inst}",
            }
            try:
                from agent.prediction.feature_archive import write_feature_snapshot

                archived = write_feature_snapshot(
                    pred_id=pid,
                    day=day,
                    instrument=inst,
                    features={
                        "score": score,
                        "direction": direction,
                        "raw_confidence": raw,
                        "signal_file": path.name,
                    },
                )
                feat["snapshot_ref"] = archived["snapshot_ref"]
                feat["content_hash"] = archived["content_hash"]
            except Exception as exc:  # noqa: BLE001
                feat["snapshot_ref"] = f"signals://{day}.csv#{inst}"
                # keep emit alive if parquet write fails
                _ = exc
            pred = {
                "pred_id": pid,
                "level": "L1",
                "object": inst,
                "object_name": "",
                "claim_type": "direction",
                "claim": {"direction": direction, "vs": benchmark, "score": score},
                "horizon": horizon,
                "benchmark": benchmark,
                "settlement_caliber": CALIBER,
                "confidence": conf,
                "raw_confidence": raw,
                "strategy_version": f"{STRATEGY_VERSION}.live",
                "feature_snapshot": feat,
                "created_at": now,
                "pred_date": day,
                "resolve_at": None,
                "status": status,
                "outcome": None,
                "error_metrics": None,
                "failure_conditions": [
                    "滚动 Wilson 下界跌破 0.50 且持续 2 窗 → 降权",
                    "预测期内 ST/退市 → 事件日提前结算",
                    "结算日涨跌停仅标注，仍按收盘价命中",
                ],
                "critic_notes": [
                    "代码断言：特征来自 pred_date 当日信号 CSV（PIT）",
                    f"系统模式={mode}",
                    f"特征归档={feat.get('snapshot_ref')}",
                ],
                "explanation": None,
                "entry_date": None,
                "entry_price": None,
                "synthetic": False,
            }
            chk = assert_prediction_record(pred)
            if not chk.ok:
                blocked += 1
                continue
            existing = store.get_prediction(pid, path=db_path)
            if existing and existing.get("status") == "resolved":
                continue
            store.upsert_prediction(pred, path=db_path)
            emitted += 1

    store.set_meta("last_emit_day", day, path=db_path)
    from agent.prediction.l2_emit import emit_l2_portfolio
    from agent.prediction.interval_emit import emit_interval_band

    l2 = emit_l2_portfolio(day, db_path=db_path)
    interval = emit_interval_band(day, db_path=db_path)
    return {
        "ok": True,
        "emitted": emitted,
        "blocked": blocked,
        "mode": mode,
        "day": day,
        "scorecard_n": sc["n"],
        "l2": l2,
        "interval": interval,
        "critic_warnings": critic_warns,
    }
