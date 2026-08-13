"""Blend champion + promoted challenger weights into the main release pool.

Plan §8: unpromoted challengers stay weight 0. Promoted factorlab.* champions
contribute trust_weight into a per-instrument blend score used for ranking.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent.core import store
from agent.core.caliber import STRATEGY_VERSION


def weighted_strategies(path: Path | None = None) -> list[dict[str, Any]]:
    rows = store.list_strategies(path=path)
    out = []
    for s in rows:
        w = float(s.get("trust_weight") or 0)
        if w <= 0:
            continue
        if s.get("state") not in ("champion",):
            continue
        out.append(s)
    if not out:
        # fallback: champion lgbm even if weight 0 during shadow
        champ = store.get_strategy("lgbm_planC", path=path)
        if champ:
            out.append({**champ, "trust_weight": max(float(champ.get("trust_weight") or 0), 1e-6)})
    return out


def _score_from_pred(p: dict[str, Any]) -> float | None:
    claim = p.get("claim") or {}
    if "score" in claim:
        try:
            return float(claim["score"])
        except (TypeError, ValueError):
            return None
    # map confidence × direction
    conf = p.get("confidence")
    if conf is None:
        return None
    direction = claim.get("direction")
    s = float(conf)
    return s if direction != "down" else -s


def blend_day(
    day: str,
    *,
    path: Path | None = None,
) -> dict[str, Any]:
    """Return instrument → blended score + contributors for pred_date=day."""
    strats = weighted_strategies(path=path)
    weights = {s["strategy_id"]: float(s["trust_weight"]) for s in strats}
    # map strategy_id → prediction strategy_version prefixes
    prefixes: dict[str, list[str]] = {
        "lgbm_planC": [STRATEGY_VERSION],
    }
    for s in strats:
        sid = s["strategy_id"]
        if sid.startswith("factorlab."):
            prefixes[sid] = [sid, f"{sid}.shadow"]

    preds = [
        p
        for p in store.list_predictions(path=path, include_synthetic=False, limit=5000)
        if p.get("pred_date") == day
        and p.get("claim_type") == "direction"
        and p.get("level") == "L1"
        and p.get("status") in ("pending", "shadow", "resolved")
    ]

    # instrument → strategy_id → score
    grid: dict[str, dict[str, float]] = {}
    for p in preds:
        sv = str(p.get("strategy_version") or "")
        owner = None
        for sid, prefs in prefixes.items():
            if any(sv == pr or sv.startswith(pr + ".") for pr in prefs):
                owner = sid
                break
        if owner is None or owner not in weights:
            continue
        sc = _score_from_pred(p)
        if sc is None:
            continue
        grid.setdefault(p["object"], {})[owner] = sc

    blended: dict[str, Any] = {}
    for inst, parts in grid.items():
        num = den = 0.0
        contributors = []
        for sid, sc in parts.items():
            w = weights.get(sid, 0.0)
            if w <= 0:
                continue
            num += w * sc
            den += w
            contributors.append({"strategy_id": sid, "weight": w, "score": sc})
        if den <= 0:
            continue
        blended[inst] = {
            "blend_score": num / den,
            "contributors": contributors,
            "n_sources": len(contributors),
        }
    return {"day": day, "weights": weights, "instruments": blended}


def apply_blend_to_preds(
    preds: list[dict[str, Any]],
    *,
    path: Path | None = None,
) -> list[dict[str, Any]]:
    """Attach blend_score to champion L1 direction rows; sort-stable hint."""
    by_day: dict[str, dict[str, Any]] = {}
    out = []
    for p in preds:
        day = p.get("pred_date") or ""
        if day not in by_day:
            by_day[day] = blend_day(day, path=path)["instruments"]
        inst = p.get("object")
        info = by_day[day].get(inst) if inst else None
        q = dict(p)
        if info and str(p.get("strategy_version") or "").startswith(STRATEGY_VERSION):
            q["blend_score"] = info["blend_score"]
            q["blend_contributors"] = info["contributors"]
        elif info:
            q["blend_score"] = info["blend_score"]
            q["blend_contributors"] = info["contributors"]
        out.append(q)
    return out
