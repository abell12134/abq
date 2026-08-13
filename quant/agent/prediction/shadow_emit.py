"""Challenger shadow predictions — score factor_lab exprs → L1 direction claims.

Does not change live orders. strategy_version = factorlab.{name}.shadow
so gates.py can accumulate hit rates per challenger.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from agent.core import store
from agent.core.caliber import CALIBER, DEFAULT_BENCHMARK, FEATURE_VERSION, HORIZON_DEFAULT
from agent.prediction.emit import system_mode

QUANT = Path(__file__).resolve().parents[2]
TZ = ZoneInfo("Asia/Shanghai")
SHADOW_TOP_K = 20
LOOKBACK_CAL_DAYS = 120


def _factor_exprs(statuses: tuple[str, ...] = ("paper_tracking", "passed_auto")) -> dict[str, dict[str, Any]]:
    sys.path.insert(0, str(QUANT / "factor_lab"))
    import factor_lib as FL  # noqa: WPS433

    lib = FL.load_lib()
    out = {}
    for name, fac in (lib.get("discovered") or {}).items():
        if fac.get("status") in statuses and fac.get("expr"):
            out[name] = fac
    return out


def _cross_section_scores(expr: str, day: str, market: str = "csi500") -> pd.Series:
    """Factor value cross-section on `day` (PIT: features up to day inclusive)."""
    sys.path.insert(0, str(QUANT / "ops"))
    import common as C  # noqa: WPS433

    C.init_qlib()
    from qlib.data import D

    start = (pd.Timestamp(day) - pd.Timedelta(days=LOOKBACK_CAL_DAYS)).strftime("%Y-%m-%d")
    inst = D.instruments(market=market)
    df = D.features(inst, [expr], start_time=start, end_time=day)
    if df is None or df.empty:
        return pd.Series(dtype=float)
    df = df.replace([np.inf, -np.inf], np.nan)
    # last available datetime <= day
    try:
        # MultiIndex instrument, datetime
        last = df.index.get_level_values("datetime").max()
        sub = df.xs(last, level="datetime").iloc[:, 0]
    except Exception:
        return pd.Series(dtype=float)
    return sub.dropna().astype(float)


def emit_shadow_for_factor(
    name: str,
    fac: dict[str, Any],
    day: str,
    *,
    top_k: int = SHADOW_TOP_K,
    db_path: Path | None = None,
) -> dict[str, Any]:
    expr = fac.get("expr")
    if not expr:
        return {"ok": False, "factor": name, "error": "no expr"}
    try:
        scores = _cross_section_scores(expr, day)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "factor": name, "error": str(exc)}
    if scores.empty or len(scores) < top_k * 2:
        return {"ok": False, "factor": name, "error": f"insufficient cross-section n={len(scores)}"}

    ranked = scores.sort_values(ascending=False)
    longs = ranked.head(top_k)
    shorts = ranked.tail(top_k)
    mode = system_mode(db_path)
    status = "shadow" if mode == "shadow" else "pending"
    now = datetime.now(TZ).isoformat()
    sid = f"factorlab.{name}"
    emitted = 0

    def _one(inst: str, direction: str, score: float) -> None:
        nonlocal emitted
        pid = f"pred_{day.replace('-', '')}_SH_{name}_{inst}_{direction}"
        existing = store.get_prediction(pid, path=db_path)
        if existing and existing.get("status") == "resolved":
            return
        # confidence from rank pct
        pct = float((scores <= score).mean())
        raw = round(0.5 + 0.45 * abs(pct - 0.5) * 2, 4)
        store.upsert_prediction(
            {
                "pred_id": pid,
                "level": "L1",
                "object": str(inst).upper(),
                "object_name": "",
                "claim_type": "direction",
                "claim": {
                    "direction": direction,
                    "vs": DEFAULT_BENCHMARK,
                    "score": float(score),
                    "factor": name,
                },
                "horizon": HORIZON_DEFAULT,
                "benchmark": DEFAULT_BENCHMARK,
                "settlement_caliber": CALIBER,
                "confidence": raw,
                "raw_confidence": raw,
                "strategy_version": f"{sid}.shadow",
                "feature_snapshot": {
                    "feature_version": f"factorlab.{name}",
                    "pit_timestamp": f"{day}T15:00:00+08:00",
                    "content_hash": pid[-16:],
                    "snapshot_ref": f"factorlab://{name}@{day}",
                },
                "created_at": now,
                "pred_date": day,
                "resolve_at": None,
                "status": status,
                "outcome": None,
                "failure_conditions": [
                    "challenger 影子预测，不进主推荐权重",
                    "晋升须过二项+Holm 门且样本外追踪达标",
                ],
                "critic_notes": [f"shadow emit from expr; factor_lab status={fac.get('status')}"],
                "synthetic": False,
            },
            path=db_path,
        )
        emitted += 1

    for inst, score in longs.items():
        _one(inst, "up", float(score))
    for inst, score in shorts.items():
        _one(inst, "down", float(score))

    return {
        "ok": True,
        "factor": name,
        "emitted": emitted,
        "strategy_id": sid,
        "cross_section_n": int(len(scores)),
    }


def emit_all_shadows(day: str, *, db_path: Path | None = None) -> dict[str, Any]:
    from agent.prediction.critic_assert import _latest_trading_day

    latest = _latest_trading_day()
    if latest and day > latest:
        return {
            "ok": False,
            "error": f"lookahead: pred_date {day} 晚于 Qlib 最新交易日 {latest}",
            "emitted": 0,
        }

    factors = _factor_exprs()
    results = []
    for name, fac in factors.items():
        results.append(emit_shadow_for_factor(name, fac, day, db_path=db_path))
    ok_n = sum(1 for r in results if r.get("ok"))
    emitted = sum(int(r.get("emitted") or 0) for r in results)
    store.set_meta("last_shadow_emit_day", day, path=db_path)
    return {
        "ok": True,
        "day": day,
        "factors": len(factors),
        "ok_factors": ok_n,
        "emitted": emitted,
        "details": results,
    }
