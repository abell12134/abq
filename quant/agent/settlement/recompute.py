"""Caliber upgrade — recompute historical outcomes; keep prior calibers side-by-side."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from agent.core import store
from agent.core.caliber import CALIBER
from agent.settlement.settle import settle_prediction

TZ = ZoneInfo("Asia/Shanghai")


def _asof_for(pred: dict[str, Any]) -> str:
    if pred.get("resolve_at"):
        return str(pred["resolve_at"])
    oc = pred.get("outcome") or {}
    if oc.get("resolve_at"):
        return str(oc["resolve_at"])
    return datetime.now(TZ).strftime("%Y-%m-%d")


def recompute_prediction(
    pred: dict[str, Any],
    *,
    target_caliber: str = CALIBER,
    db_path: Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Re-settle one resolved/pending-ready pred under target_caliber."""
    if pred.get("synthetic"):
        return {"ok": False, "pred_id": pred.get("pred_id"), "error": "synthetic"}
    if pred.get("status") not in ("resolved", "pending", "shadow"):
        return {"ok": False, "pred_id": pred.get("pred_id"), "error": "bad_status"}

    # Force settle path to use target caliber
    work = dict(pred)
    work["settlement_caliber"] = target_caliber
    asof = _asof_for(pred)
    try:
        outcome = settle_prediction(work, asof=asof)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "pred_id": pred["pred_id"], "error": str(exc)}

    if not outcome.get("ready"):
        return {
            "ok": False,
            "pred_id": pred["pred_id"],
            "error": outcome.get("reason") or "not_ready",
            "outcome": outcome,
        }

    em = dict(pred.get("error_metrics") or {})
    by = dict(em.get("outcomes_by_caliber") or {})
    # preserve previous active outcome under its caliber
    old = pred.get("outcome")
    if isinstance(old, dict) and old.get("settlement_caliber"):
        by.setdefault(str(old["settlement_caliber"]), old)
    by[target_caliber] = {
        k: outcome[k]
        for k in (
            "hit",
            "excess_return",
            "stock_ret",
            "bench_ret",
            "exit_price",
            "entry_date",
            "entry_price",
            "resolve_at",
            "settlement_caliber",
            "early_settle_reason",
            "event_notes",
            "entry_limit_up",
            "entry_limit_down",
            "resolve_limit_up",
            "resolve_limit_down",
            "st_event_day",
            "delist_event_day",
            "pic",
            "width",
            "interval",
            "adjudication",
            "portfolio_return",
            "ann_return",
            "max_drawdown",
            "ann_vol",
            "paper_metrics",
        )
        if k in outcome
    }
    em["outcomes_by_caliber"] = by
    em["active_caliber"] = target_caliber
    em["recomputed_at"] = datetime.now(TZ).isoformat()

    new_outcome = {
        "hit": outcome.get("hit"),
        "excess_return": outcome.get("excess_return"),
        "stock_ret": outcome.get("stock_ret"),
        "bench_ret": outcome.get("bench_ret"),
        "exit_price": outcome.get("exit_price"),
        "settlement_caliber": target_caliber,
        "early_settle_reason": outcome.get("early_settle_reason"),
        "event_notes": outcome.get("event_notes"),
        "entry_limit_up": outcome.get("entry_limit_up"),
        "entry_limit_down": outcome.get("entry_limit_down"),
        "resolve_limit_up": outcome.get("resolve_limit_up"),
        "resolve_limit_down": outcome.get("resolve_limit_down"),
        "st_event_day": outcome.get("st_event_day"),
        "delist_event_day": outcome.get("delist_event_day"),
    }
    if outcome.get("adjudication"):
        new_outcome.update(
            {
                "adjudication": outcome["adjudication"],
                "portfolio_return": outcome.get("portfolio_return"),
                "ann_return": outcome.get("ann_return"),
                "max_drawdown": outcome.get("max_drawdown"),
                "ann_vol": outcome.get("ann_vol"),
                "paper_metrics": True,
            }
        )
    if "pic" in outcome:
        new_outcome["pic"] = outcome["pic"]
        new_outcome["width"] = outcome.get("width")
        new_outcome["interval"] = outcome.get("interval")

    changed = bool(old) and (
        old.get("hit") != new_outcome.get("hit")
        or round(float(old.get("excess_return") or 0), 6)
        != round(float(new_outcome.get("excess_return") or 0), 6)
    )

    if not dry_run:
        pred["outcome"] = new_outcome
        pred["error_metrics"] = em
        pred["resolve_at"] = outcome.get("resolve_at") or pred.get("resolve_at")
        pred["entry_date"] = outcome.get("entry_date") or pred.get("entry_date")
        pred["entry_price"] = outcome.get("entry_price") or pred.get("entry_price")
        if outcome.get("ready") and pred.get("status") in ("pending", "shadow"):
            pred["status"] = "resolved"
        notes = list(pred.get("critic_notes") or [])
        notes.append(
            f"口径重算 → {target_caliber}"
            + ("（结果变化）" if changed else "（结果未变）")
        )
        if outcome.get("event_notes"):
            notes.extend(outcome["event_notes"])
        pred["critic_notes"] = notes[-30:]
        # keep emit-time caliber on row; active is in outcome
        store.upsert_prediction(pred, path=db_path)

    return {
        "ok": True,
        "pred_id": pred["pred_id"],
        "changed": changed,
        "target_caliber": target_caliber,
        "hit": new_outcome.get("hit"),
    }


def recompute_all(
    *,
    target_caliber: str = CALIBER,
    db_path: Path | None = None,
    dry_run: bool = False,
    limit: int = 5000,
) -> dict[str, Any]:
    rows = store.list_predictions(
        status="resolved", path=db_path, include_synthetic=False, limit=limit
    )
    ok = changed = fail = 0
    details = []
    for pred in rows:
        r = recompute_prediction(
            pred, target_caliber=target_caliber, db_path=db_path, dry_run=dry_run
        )
        details.append(r)
        if r.get("ok"):
            ok += 1
            if r.get("changed"):
                changed += 1
        else:
            fail += 1
    store.set_meta("last_recompute_caliber", target_caliber, path=db_path)
    store.set_meta(
        "last_recompute_at", datetime.now(TZ).isoformat(), path=db_path
    )
    return {
        "ok": True,
        "target_caliber": target_caliber,
        "dry_run": dry_run,
        "total": len(rows),
        "ok_n": ok,
        "changed_n": changed,
        "fail_n": fail,
        "details": details[:50],
    }
