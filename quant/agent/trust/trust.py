"""L3 strategy trust — rolling Wilson CI, pause / weight (deterministic)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from agent.core import store
from agent.core.caliber import STRATEGY_VERSION
from agent.core.scorecard import scorecard_for, wilson_interval

TZ = ZoneInfo("Asia/Shanghai")

# Plan §8.1: below random (0.5) for K consecutive evaluation windows → downweight/pause
BAD_WINDOW_LIMIT = 2
RANDOM_BASELINE = 0.5
MIN_N_EVAL = 30
ROLLING_CAP = 60  # most recent resolved


def _rolling_resolved(path: Path | None = None) -> list[dict[str, Any]]:
    rows = [
        p
        for p in store.list_predictions(
            status="resolved", path=path, include_synthetic=False, limit=2000
        )
        if p.get("claim_type") == "direction"
        and isinstance(p.get("outcome"), dict)
        and str(p.get("strategy_version") or "").startswith(STRATEGY_VERSION)
    ]
    # list is pred_date DESC; take most recent ROLLING_CAP
    return rows[:ROLLING_CAP]


def refresh_trust(path: Path | None = None) -> dict[str, Any]:
    """Recompute champion trust from ledger; persist strategies table."""
    rows = _rolling_resolved(path)
    sc = scorecard_for(rows, claim_type="direction", min_n=MIN_N_EVAL)
    now = datetime.now(TZ).isoformat()
    prev = store.get_strategy("lgbm_planC", path=path) or {}
    bad = int(prev.get("bad_windows") or 0)

    state = "champion"
    weight = 1.0
    pause_reason = None

    if not sc["sample_ok"]:
        state = "shadow"
        weight = 0.0
        pause_reason = "样本不足，不评估不降权"
        bad = 0
    else:
        lo = sc.get("wilson_low")
        if lo is not None and lo < RANDOM_BASELINE:
            bad += 1
            weight = max(0.0, 1.0 - 0.5 * bad)
            if bad >= BAD_WINDOW_LIMIT:
                state = "paused"
                weight = 0.0
                pause_reason = (
                    f"滚动 Wilson 下界 {lo:.1%} 连续 {bad} 窗低于随机基准 {RANDOM_BASELINE:.0%}"
                )
            else:
                pause_reason = (
                    f"滚动 Wilson 下界 {lo:.1%} 低于随机基准（警告窗 {bad}/{BAD_WINDOW_LIMIT}）"
                )
        else:
            bad = 0
            state = "champion"
            weight = 1.0
            pause_reason = None

    # severe: 2σ below 0.5 approx using wilson — if hi < 0.5 also pause
    hi = sc.get("wilson_high")
    if sc["sample_ok"] and hi is not None and hi < RANDOM_BASELINE:
        state = "paused"
        weight = 0.0
        pause_reason = f"滚动命中率上界 {hi:.1%} 仍低于随机基准 —— 严重失效，直接暂停"

    store.upsert_strategy(
        {
            "strategy_id": "lgbm_planC",
            "name": "LGBM Plan C 方向",
            "version": f"{STRATEGY_VERSION}.live",
            "state": state,
            "trust_weight": weight,
            "claim_type": "direction",
            "rolling_n": sc["n"],
            "rolling_hit_rate": sc.get("hit_rate"),
            "wilson_low": sc.get("wilson_low"),
            "wilson_high": sc.get("wilson_high"),
            "pause_reason": pause_reason,
            "bad_windows": bad,
            "updated_at": now,
        },
        path=path,
    )
    return store.get_strategy("lgbm_planC", path=path) or {}


def list_trust(path: Path | None = None) -> list[dict[str, Any]]:
    rows = store.list_strategies(path=path)
    if not rows:
        refresh_trust(path=path)
        rows = store.list_strategies(path=path)
    return rows
