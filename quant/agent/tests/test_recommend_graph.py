"""Tests for recommend blend + langgraph smoke."""

from __future__ import annotations

import sys
from pathlib import Path

QUANT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(QUANT))

from agent.core import store  # noqa: E402
from agent.core.caliber import STRATEGY_VERSION  # noqa: E402
from agent.trust.recommend import blend_day  # noqa: E402


def test_blend_weights(tmp_path: Path | None = None):
    import tempfile
    from datetime import datetime
    from zoneinfo import ZoneInfo

    db = Path(tempfile.mkdtemp()) / "t.sqlite3"
    store.init_db(db)
    now = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
    day = "2026-08-11"
    store.upsert_strategy(
        {
            "strategy_id": "lgbm_planC",
            "name": "champ",
            "version": "v",
            "state": "champion",
            "trust_weight": 1.0,
            "claim_type": "direction",
            "rolling_n": 40,
            "rolling_hit_rate": 0.6,
            "wilson_low": 0.5,
            "wilson_high": 0.7,
            "pause_reason": None,
            "bad_windows": 0,
            "updated_at": now,
        },
        path=db,
    )
    store.upsert_strategy(
        {
            "strategy_id": "factorlab.alpha_x",
            "name": "chal",
            "version": "v",
            "state": "champion",
            "trust_weight": 1.0,
            "claim_type": "direction",
            "rolling_n": 40,
            "rolling_hit_rate": 0.55,
            "wilson_low": 0.45,
            "wilson_high": 0.65,
            "pause_reason": None,
            "bad_windows": 0,
            "updated_at": now,
        },
        path=db,
    )
    for sid, score, sv in (
        ("c", 2.0, f"{STRATEGY_VERSION}.live"),
        ("f", 0.0, "factorlab.alpha_x.shadow"),
    ):
        store.upsert_prediction(
            {
                "pred_id": f"pred_{sid}",
                "level": "L1",
                "object": "SZ000001",
                "object_name": "",
                "claim_type": "direction",
                "claim": {"direction": "up", "score": score},
                "horizon": 10,
                "benchmark": "CSI500",
                "settlement_caliber": "caliber.v1.1.events",
                "confidence": 0.6,
                "raw_confidence": 0.6,
                "strategy_version": sv,
                "feature_snapshot": {
                    "feature_version": "t",
                    "pit_timestamp": f"{day}T15:00:00+08:00",
                    "content_hash": "x",
                    "snapshot_ref": "t",
                },
                "created_at": now,
                "pred_date": day,
                "resolve_at": None,
                "status": "pending",
                "outcome": None,
                "error_metrics": None,
                "failure_conditions": [],
                "critic_notes": [],
                "explanation": None,
                "entry_date": None,
                "entry_price": None,
                "synthetic": False,
            },
            path=db,
        )
    out = blend_day(day, path=db)
    inst = out["instruments"]["SZ000001"]
    # equal weights → mean of 2.0 and 0.0
    assert abs(inst["blend_score"] - 1.0) < 1e-9
    assert inst["n_sources"] == 2


def test_langgraph_plan_only():
    from agent.orchestration.graph import run_graph

    out = run_graph(message="看看今日放行", use_llm=False)
    assert out.get("reply")
    assert out.get("meta", {}).get("graph") == "langgraph"


if __name__ == "__main__":
    test_blend_weights()
    test_langgraph_plan_only()
    print("OK recommend/graph tests")
