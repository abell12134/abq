"""Track stage — resolve due predictions after EOD."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent.core import store
from agent.settlement.settle import settle_prediction
from agent.core.caliber import STRATEGY_VERSION, SHADOW_MIN_N
from agent.prediction.emit import system_mode
from agent.trust.trust import refresh_trust


def track_day(day: str, *, db_path: Path | None = None) -> dict[str, Any]:
    pending = store.list_pending(path=db_path)
    resolved_n = 0
    advanced = 0
    errors: list[str] = []

    for pred in pending:
        if pred.get("synthetic"):
            continue
        try:
            outcome = settle_prediction(pred, asof=day)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{pred['pred_id']}: {exc}")
            continue
        if not outcome.get("ready"):
            if outcome.get("entry_date") and not pred.get("entry_date"):
                pred["entry_date"] = outcome["entry_date"]
                pred["entry_price"] = outcome.get("entry_price")
                store.upsert_prediction(pred, path=db_path)
                advanced += 1
            continue

        pred["status"] = "resolved"
        pred["resolve_at"] = outcome["resolve_at"]
        pred["entry_date"] = outcome.get("entry_date")
        pred["entry_price"] = outcome.get("entry_price")
        # L1 vs L2 outcome shapes
        if outcome.get("adjudication"):
            pred["outcome"] = {
                "hit": outcome.get("hit"),
                "adjudication": outcome["adjudication"],
                "portfolio_return": outcome.get("portfolio_return"),
                "ann_return": outcome.get("ann_return"),
                "excess_return": outcome.get("excess_return"),
                "max_drawdown": outcome.get("max_drawdown"),
                "ann_vol": outcome.get("ann_vol"),
                "bench_ret": outcome.get("bench_ret"),
                "paper_metrics": True,
                "settlement_caliber": outcome.get("settlement_caliber"),
            }
            note = (
                f"{day}: L2 裁决 excess={outcome['adjudication']['excess']} "
                f"target={outcome['adjudication']['target']} "
                f"constraints={outcome['adjudication']['constraints']}"
            )
        else:
            pred["outcome"] = {
                "hit": outcome["hit"],
                "excess_return": outcome["excess_return"],
                "stock_ret": outcome["stock_ret"],
                "bench_ret": outcome["bench_ret"],
                "exit_price": outcome["exit_price"],
                "settlement_caliber": outcome["settlement_caliber"],
                "early_settle_reason": outcome.get("early_settle_reason"),
                "event_notes": outcome.get("event_notes"),
                "entry_limit_up": outcome.get("entry_limit_up"),
                "entry_limit_down": outcome.get("entry_limit_down"),
                "resolve_limit_up": outcome.get("resolve_limit_up"),
                "resolve_limit_down": outcome.get("resolve_limit_down"),
                "st_event_day": outcome.get("st_event_day"),
                "delist_event_day": outcome.get("delist_event_day"),
            }
            note = (
                f"{day}: 结算 {'HIT' if outcome['hit'] else 'MISS'} "
                f"excess={outcome['excess_return']:.2%}"
            )
            if outcome.get("early_settle_reason"):
                note += f" · 提前结算={outcome['early_settle_reason']}"
        notes = list(pred.get("critic_notes") or [])
        notes.append(note)
        for en in outcome.get("event_notes") or []:
            notes.append(en)
        pred["critic_notes"] = notes[-20:]
        store.upsert_prediction(pred, path=db_path)
        resolved_n += 1

    mode = system_mode(db_path)
    n = store.count_resolved(STRATEGY_VERSION, "direction", path=db_path)
    trust = refresh_trust(path=db_path)
    # Sync factor_lab challengers (non-fatal)
    try:
        from agent.trust.challenger import sync_from_factor_lab, evaluate_challengers

        sync_from_factor_lab(path=db_path)
        evaluate_challengers(path=db_path)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"challenger_sync: {exc}")

    store.set_meta("last_track_day", day, path=db_path)
    return {
        "ok": True,
        "day": day,
        "pending": len(pending),
        "resolved": resolved_n,
        "advanced": advanced,
        "mode": mode,
        "resolved_total": n,
        "graduate_at_n": SHADOW_MIN_N,
        "trust": trust,
        "errors": errors,
    }
