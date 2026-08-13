"""Emit L1 interval claims from score dispersion (top band)."""

from __future__ import annotations

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
    STRATEGY_VERSION,
)
from agent.prediction.emit import score_to_raw_confidence, system_mode

QUANT = Path(__file__).resolve().parents[2]
TZ = ZoneInfo("Asia/Shanghai")


def emit_interval_band(
    day: str,
    *,
    top_k: int = 10,
    low: float = -0.02,
    high: float = 0.05,
    horizon: int = HORIZON_DEFAULT,
    benchmark: str = DEFAULT_BENCHMARK,
    db_path: Path | None = None,
) -> dict[str, Any]:
    path = QUANT / "data" / "signals" / f"{day}.csv"
    if not path.exists():
        path = QUANT / "data" / "signals" / "latest_pred.csv"
    if not path.exists():
        return {"ok": False, "error": "no signals", "emitted": 0}

    df = pd.read_csv(path).sort_values("score", ascending=False).head(top_k)
    scores = pd.read_csv(path)["score"].astype(float) if path.exists() else df["score"]
    mode = system_mode(db_path)
    status = "shadow" if mode == "shadow" else "pending"
    now = datetime.now(TZ).isoformat()
    emitted = 0
    for _, row in df.iterrows():
        inst = str(row["instrument"]).upper()
        score = float(row["score"])
        raw = score_to_raw_confidence(score, scores)
        pid = f"pred_{day.replace('-', '')}_L1_{inst}_interval"
        existing = store.get_prediction(pid, path=db_path)
        if existing and existing.get("status") == "resolved":
            continue
        pred = {
            "pred_id": pid,
            "level": "L1",
            "object": inst,
            "object_name": "",
            "claim_type": "interval",
            "claim": {"low": low, "high": high, "vs": benchmark, "score": score},
            "horizon": horizon,
            "benchmark": benchmark,
            "settlement_caliber": CALIBER,
            "confidence": raw,
            "raw_confidence": raw,
            "strategy_version": f"{STRATEGY_VERSION}.interval",
            "feature_snapshot": {
                "feature_version": FEATURE_VERSION,
                "pit_timestamp": f"{day}T15:00:00+08:00",
                "content_hash": pid,
                "snapshot_ref": f"signals://{day}.csv#{inst}",
            },
            "created_at": now,
            "pred_date": day,
            "resolve_at": None,
            "status": status,
            "outcome": None,
            "failure_conditions": [
                "区间 PIC 与名义水平偏离 → 重估带宽，不做命中率校准",
            ],
            "critic_notes": ["interval claim · 与方向成绩单分列"],
            "synthetic": False,
        }
        store.upsert_prediction(pred, path=db_path)
        emitted += 1
    return {"ok": True, "emitted": emitted, "day": day, "band": [low, high]}
