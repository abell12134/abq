"""到期结算：窗口走完后用同一套池内等权 − 中证500 判定 hit/miss。"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

import pandas as pd

from . import store
from .schema import BENCHMARK, HORIZONS, window_days

log = logging.getLogger(__name__)
QUANT = Path(__file__).resolve().parents[2]


def _cal() -> list[str]:
    sys.path.insert(0, str(QUANT / "ops"))
    import common as C
    return [str(pd.Timestamp(d))[:10] for d in C.calendar()]


def _excess_from_panel(panel: pd.DataFrame, days: list[str], industry: str) -> float | None:
    sub = panel[(panel["day"].isin(days)) & (panel["industry"] == industry)]
    if sub["day"].nunique() < len(days):
        return None
    eq = float(sub.groupby("day")["eq_ret"].first().sum())
    bench = panel[panel["day"].isin(days)].drop_duplicates("day").set_index("day")["bench_ret"]
    if bench.reindex(days).isna().any():
        return None
    return eq - float(bench.reindex(days).sum())


def settle_file(pred: dict[str, Any], panel: pd.DataFrame, cal: list[str]) -> dict[str, Any]:
    claims = pred.get("claims") or []
    changed = False
    for c in claims:
        if c.get("status") in {"hit", "miss"}:
            continue
        h = int(c.get("horizon_days") or 0)
        days = window_days(cal, pred["day"], h)
        if len(days) < h:
            c["status"] = "pending"
            continue
        ex = _excess_from_panel(panel, days, c["industry"])
        if ex is None:
            continue
        action = c.get("action")
        beat = ex > 0
        if action == "up":
            hit = beat
        elif action == "avoid":
            hit = not beat
        else:
            continue
        c["realized_excess"] = round(float(ex), 4)
        c["result"] = "hit" if hit else "miss"
        c["status"] = c["result"]
        c["window_end"] = days[-1]
        changed = True
        store.append_settled({
            "pred_day": pred["day"],
            "industry": c["industry"],
            "horizon_days": h,
            "action": action,
            "hit": hit,
            "realized_excess": c["realized_excess"],
            "p_beat": c.get("p_beat"),
            "shadow": c.get("shadow"),
            "benchmark": BENCHMARK,
        })
    if changed:
        pred["claims"] = claims
        store.save_prediction(pred["day"], pred)
    return pred


def settle_due(as_of: str | None = None) -> dict[str, Any]:
    """扫描预测文件，窗口已结束的全部结算（共享一块面板）。"""
    from .features import build_panel

    cal = _cal()
    as_of = as_of or cal[-1]
    due: list[dict[str, Any]] = []
    min_day = as_of
    for day in store.list_pred_days(120):
        pred = store.load_prediction(day)
        if not pred:
            continue
        ready = False
        for h in HORIZONS:
            w = window_days(cal, day, h)
            if len(w) >= h and w[-1] <= as_of:
                ready = True
                break
        if not ready:
            continue
        due.append(pred)
        if day < min_day:
            min_day = day
    if not due:
        live = _live_scorecard()
        return {"ok": True, "files": 0, "updated_claims": 0, "live": live}

    if as_of not in cal or min_day not in cal:
        return {"ok": False, "files": 0, "updated_claims": 0, "error": "日期不在日历"}
    lookback = cal.index(as_of) - cal.index(min_day) + 8
    try:
        panel = build_panel(as_of, lookback=max(lookback, 30))
    except Exception as e:  # noqa: BLE001
        log.warning("结算面板失败: %s", e)
        return {"ok": False, "files": 0, "updated_claims": 0, "error": str(e)}

    n_new = 0
    for pred in due:
        before = [c.get("status") for c in (pred.get("claims") or [])]
        pred = settle_file(pred, panel, cal)
        after = [c.get("status") for c in (pred.get("claims") or [])]
        n_new += sum(1 for a, b in zip(before, after) if a != b)
    live = _live_scorecard()
    if live:
        store.save_json(store.eval_path("live.json"), live)
    return {"ok": True, "files": len(due), "updated_claims": n_new, "live": live}


def _live_scorecard() -> dict[str, Any] | None:
    path = store.ROOT / "eval" / "settled.jsonl"
    if not path.exists():
        return None
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:  # noqa: BLE001
            continue
    if not rows:
        return None
    out: dict[str, Any] = {"n": len(rows), "by_horizon": {}}
    for h in HORIZONS:
        sub = [r for r in rows if int(r.get("horizon_days") or 0) == h]
        if not sub:
            continue
        hits = [r for r in sub if r.get("hit")]
        out["by_horizon"][str(h)] = {
            "n": len(sub),
            "hit": round(len(hits) / len(sub), 4),
            "mean_excess": round(
                sum(float(r.get("realized_excess") or 0) for r in sub) / len(sub), 4),
        }
    return out
