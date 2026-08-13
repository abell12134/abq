"""Shared helpers to attach scorecard + release_gate for API responses."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from agent.core import store
from agent.core.caliber import CALIBER, SHADOW_MIN_DAYS, SHADOW_MIN_N, STRATEGY_VERSION
from agent.core.scorecard import calibration_buckets, scorecard_for

TZ = ZoneInfo("Asia/Shanghai")


def _release_gate(pred: dict[str, Any], mode: str, sc: dict[str, Any]) -> str:
    if pred.get("status") == "resolved":
        return "observe"
    if mode == "shadow":
        return "hold"
    if pred.get("level") == "L2":
        # L2 组合不进 L1 主荐股区，结算台/账本可见
        return "observe"
    try:
        from agent.trust.trust import list_trust

        for s in list_trust():
            if s.get("strategy_id") == "lgbm_planC" and s.get("state") == "paused":
                return "quarantine"
    except Exception:
        pass
    if not sc.get("sample_ok"):
        return "quarantine"
    return "released"


def enrich(pred: dict[str, Any], sc: dict[str, Any], mode: str) -> dict[str, Any]:
    out = dict(pred)
    out["scorecard"] = sc
    out["release_gate"] = _release_gate(pred, mode, sc)
    return out


def build_system_status(db_path: Path | None = None) -> dict[str, Any]:
    mode = store.get_meta("mode", path=db_path) or "shadow"
    n = store.count_resolved(STRATEGY_VERSION, "direction", path=db_path)
    if n >= SHADOW_MIN_N:
        mode = "graduated"
        store.set_meta("mode", mode, path=db_path)
    preds = store.list_predictions(path=db_path, include_synthetic=False, limit=2000)
    sc = scorecard_for(preds, claim_type="direction")
    # recompute gates
    released = hold = quarantine = 0
    pending_settle = 0
    for p in preds:
        g = _release_gate(p, mode, sc)
        if g == "released":
            released += 1
        elif g == "hold":
            hold += 1
        elif g == "quarantine":
            quarantine += 1
        if p.get("status") in ("pending", "shadow") and not p.get("outcome"):
            pending_settle += 1

    shadow_start = store.get_meta("shadow_start", path=db_path)
    remain = None
    if mode == "shadow" and shadow_start:
        # rough calendar proxy; UI shows trading-day intent
        try:
            delta = (datetime.now(TZ).date() - datetime.strptime(shadow_start, "%Y-%m-%d").date()).days
            remain = max(0, SHADOW_MIN_DAYS - delta)
        except ValueError:
            remain = SHADOW_MIN_DAYS

    day = (
        store.get_meta("last_track_day", path=db_path)
        or store.get_meta("last_emit_day", path=db_path)
        or datetime.now(TZ).strftime("%Y-%m-%d")
    )
    synthetic = not store.has_real_rows(path=db_path)
    return {
        "data_day": day,
        "settlement_caliber": CALIBER,
        "mode": mode,
        "shadow_days_remaining": remain,
        "released_count": released,
        "hold_count": hold,
        "quarantine_count": quarantine,
        "pending_settle_count": pending_settle,
        "synthetic_demo": synthetic,
        "disclaimer": (
            "系统只分析不交易；纸面指标≠可实现收益；无成绩单不荐股。"
            + (" 当前账本为空，API 回退合成演示。" if synthetic else f" 已结算方向样本 n={n}。")
        ),
        "resolved_direction_n": n,
    }


def list_enriched(db_path: Path | None = None) -> list[dict[str, Any]]:
    preds = store.list_predictions(path=db_path, include_synthetic=False, limit=2000)
    mode = build_system_status(db_path)["mode"]
    sc = scorecard_for(preds, claim_type="direction")
    # per claim_type scorecards
    sc_by_type = {
        "direction": sc,
        "interval": scorecard_for(preds, claim_type="interval"),
        "target": scorecard_for(preds, claim_type="target"),
    }
    out = []
    for p in preds:
        s = sc_by_type.get(p.get("claim_type") or "direction", sc)
        out.append(enrich(p, s, mode))
    try:
        from agent.trust.recommend import apply_blend_to_preds

        out = apply_blend_to_preds(out, path=db_path)
    except Exception:
        pass
    # released first by blend_score then confidence
    def _rank(p: dict[str, Any]) -> tuple:
        gate = p.get("release_gate") or ""
        g = 0 if gate == "released" else 1 if gate == "hold" else 2
        bs = p.get("blend_score")
        conf = float(p.get("confidence") or 0)
        score = float(bs) if bs is not None else conf
        return (g, -abs(score), -conf)

    out.sort(key=_rank)
    return out


def list_calibration(db_path: Path | None = None) -> list[dict[str, Any]]:
    preds = store.list_predictions(status="resolved", path=db_path, include_synthetic=False)
    return (
        calibration_buckets(preds, claim_type="direction")
        + calibration_buckets(preds, claim_type="interval")
    )
