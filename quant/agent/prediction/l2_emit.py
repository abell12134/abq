"""Emit L2 paper portfolio claim from today's released / top L1 scores."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from agent.core import store
from agent.core.caliber import CALIBER, DEFAULT_BENCHMARK, FEATURE_VERSION, STRATEGY_VERSION
from agent.prediction.emit import system_mode

QUANT = Path(__file__).resolve().parents[2]
TZ = ZoneInfo("Asia/Shanghai")

L2_HORIZON = 10
L2_TOP = 10
DEFAULT_TARGET_ANN = 0.08
DEFAULT_MAX_DD = 0.12
DEFAULT_MAX_VOL = 0.25


def emit_l2_portfolio(
    day: str,
    *,
    top_k: int = L2_TOP,
    target_ann: float = DEFAULT_TARGET_ANN,
    db_path: Path | None = None,
) -> dict[str, Any]:
    path = QUANT / "data" / "signals" / f"{day}.csv"
    if not path.exists():
        path = QUANT / "data" / "signals" / "latest_pred.csv"
    if not path.exists():
        return {"ok": False, "error": "no signals", "emitted": 0}

    df = pd.read_csv(path).sort_values("score", ascending=False).head(top_k)
    if df.empty:
        return {"ok": False, "error": "empty signals", "emitted": 0}

    w = 1.0 / len(df)
    constituents = [
        {"instrument": str(r.instrument).upper(), "weight": w, "score": float(r.score)}
        for r in df.itertuples()
    ]
    mode = system_mode(db_path)
    status = "shadow" if mode == "shadow" else "pending"
    pid = f"pred_{day.replace('-', '')}_L2_PORT_STEADY"
    now = datetime.now(TZ).isoformat()
    pred = {
        "pred_id": pid,
        "level": "L2",
        "object": "PORT_STEADY",
        "object_name": f"稳健纸面组合 Top{top_k}",
        "claim_type": "target",
        "claim": {
            "target_ann_return": target_ann,
            "max_drawdown": DEFAULT_MAX_DD,
            "max_vol": DEFAULT_MAX_VOL,
            "benchmark": DEFAULT_BENCHMARK,
            "constituents": constituents,
            "note": "纸面指标，不含冲击/滑点/涨跌停未成交",
        },
        "horizon": L2_HORIZON,
        "benchmark": DEFAULT_BENCHMARK,
        "settlement_caliber": CALIBER,
        "confidence": 0.5,
        "raw_confidence": 0.5,
        "strategy_version": f"{STRATEGY_VERSION}.l2_ew",
        "feature_snapshot": {
            "feature_version": FEATURE_VERSION,
            "pit_timestamp": f"{day}T15:00:00+08:00",
            "content_hash": pid,
            "snapshot_ref": f"signals://{day}.csv#top{top_k}",
        },
        "created_at": now,
        "pred_date": day,
        "resolve_at": None,
        "status": status,
        "outcome": None,
        "failure_conditions": [
            "三项独立裁决：超额 / 目标年化 / 约束；不可由 L1 命中率推断",
            "纸面回撤与波动，不含交易成本",
        ],
        "critic_notes": ["L2 纸面组合 · 等权 TopK"],
        "explanation": (
            f"目标年化 {target_ann:.0%}；约束回撤≤{DEFAULT_MAX_DD:.0%}、"
            f"波动≤{DEFAULT_MAX_VOL:.0%}。历史可达性见成绩单，非承诺收益。"
        ),
        "synthetic": False,
    }
    existing = store.get_prediction(pid, path=db_path)
    if existing and existing.get("status") == "resolved":
        return {"ok": True, "emitted": 0, "skipped": "already_resolved", "pred_id": pid}
    store.upsert_prediction(pred, path=db_path)
    return {"ok": True, "emitted": 1, "pred_id": pid, "mode": mode, "n_constituents": len(constituents)}
