"""Scorecards + calibration buckets from resolved ledger rows."""

from __future__ import annotations

import math
from typing import Any


def wilson_interval(hits: int, n: int, z: float = 1.96) -> tuple[float | None, float | None]:
    if n <= 0:
        return None, None
    p = hits / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre - margin) / denom, (centre + margin) / denom


def scorecard_for(
    resolved: list[dict[str, Any]],
    *,
    claim_type: str = "direction",
    min_n: int = 30,
    caliber: str | None = None,
) -> dict[str, Any]:
    from agent.core.caliber import CALIBER

    active = caliber or CALIBER
    all_rows = [
        r
        for r in resolved
        if r.get("claim_type") == claim_type
        and r.get("status") == "resolved"
        and isinstance(r.get("outcome"), dict)
        and "hit" in r["outcome"]
    ]
    rows = [
        r
        for r in all_rows
        if (r["outcome"].get("settlement_caliber") or active) == active
    ]
    if not rows:
        rows = all_rows  # transition: no active-caliber rows yet

    n = len(rows)
    hits = sum(1 for r in rows if r["outcome"].get("hit"))
    rate = hits / n if n else None
    lo, hi = wilson_interval(hits, n) if n else (None, None)
    ok = n >= min_n
    if claim_type == "interval":
        return {
            "claim_type": claim_type,
            "n": n,
            "hit_rate": None,
            "pic": round(rate, 4) if rate is not None else None,
            "wilson_low": round(lo, 4) if lo is not None else None,
            "wilson_high": round(hi, 4) if hi is not None else None,
            "sample_ok": ok,
            "label": "追踪成绩" if ok else "样本不足，仅供参考",
        }
    return {
        "claim_type": claim_type,
        "n": n,
        "hit_rate": round(rate, 4) if rate is not None else None,
        "pic": None,
        "wilson_low": round(lo, 4) if lo is not None else None,
        "wilson_high": round(hi, 4) if hi is not None else None,
        "sample_ok": ok,
        "label": "追踪成绩" if ok else "样本不足，仅供参考",
    }


def calibration_buckets(
    resolved: list[dict[str, Any]],
    *,
    claim_type: str = "direction",
    edges: list[float] | None = None,
) -> list[dict[str, Any]]:
    edges = edges or [0.5, 0.55, 0.6, 0.7, 0.85, 1.01]
    rows = [
        r
        for r in resolved
        if r.get("claim_type") == claim_type
        and r.get("status") == "resolved"
        and r.get("confidence") is not None
        and isinstance(r.get("outcome"), dict)
    ]
    out = []
    for i in range(len(edges) - 1):
        lo, hi = edges[i], edges[i + 1]
        bucket = [
            r
            for r in rows
            if lo <= float(r["confidence"]) < hi
        ]
        n = len(bucket)
        if n == 0:
            continue
        emp = sum(1 for r in bucket if r["outcome"].get("hit")) / n
        mean_c = sum(float(r["confidence"]) for r in bucket) / n
        out.append(
            {
                "claim_type": claim_type,
                "bin_lo": lo,
                "bin_hi": hi,
                "mean_confidence": round(mean_c, 4),
                "empirical_rate": round(emp, 4),
                "n": n,
            }
        )
    return out
