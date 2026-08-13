"""Sync factor_lab → challenger registry; evaluate promotion gates."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from agent.core import store
from agent.trust.gates import apply_holm, eval_hit_rate_challenger
from agent.trust.trust import refresh_trust

QUANT = Path(__file__).resolve().parents[2]
TZ = ZoneInfo("Asia/Shanghai")

STATUS_MAP = {
    "paper_tracking": "challenger",
    "passed_auto": "challenger",
    "live": "champion",
    "rejected": "paused",
    "candidate": "challenger",
}


def _json_safe(obj: Any) -> Any:
    """Replace NaN/Inf so FastAPI json.dumps won't blow up."""
    if isinstance(obj, float):
        if obj != obj or obj in (float("inf"), float("-inf")):
            return None
        return obj
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(v) for v in obj]
    return obj


def _load_factor_lib() -> dict[str, Any]:
    sys.path.insert(0, str(QUANT / "factor_lab"))
    import factor_lib as FL  # noqa: WPS433

    return FL.load_lib()


def sync_from_factor_lab(path: Path | None = None) -> dict[str, Any]:
    """Register paper_tracking / passed_auto / live / rejected into strategies table."""
    lib = _load_factor_lib()
    discovered = lib.get("discovered") or {}
    now = datetime.now(TZ).isoformat()
    synced = []
    for name, fac in discovered.items():
        status = fac.get("status") or "candidate"
        if status not in STATUS_MAP:
            continue
        m = fac.get("metrics") or {}
        sid = f"factorlab.{name}"
        state = STATUS_MAP[status]
        # live factors are not auto-champion for L1 weights — only lgbm_planC is trading champion;
        # mark as challenger(tracking) note unless we explicitly promote via agent gate
        if status == "live":
            state = "challenger"  # factor live ≠ prediction champion; await hit-rate gate
        if status == "rejected":
            state = "paused"
        store.upsert_strategy(
            {
                "strategy_id": sid,
                "name": f"[factor] {name}",
                "version": f"factorlab.{fac.get('updated_at') or 'na'}",
                "state": state,
                "trust_weight": 0.0,
                "claim_type": "direction",
                "rolling_n": 0,
                "rolling_hit_rate": None,
                "wilson_low": None,
                "wilson_high": None,
                "pause_reason": (
                    f"factor_lab status={status}; "
                    f"oos_ic={m.get('oos_rank_ic')}; icir={m.get('oos_icir') or m.get('icir')}"
                ),
                "bad_windows": 0,
                "updated_at": now,
            },
            path=path,
        )
        synced.append({"strategy_id": sid, "factor_status": status, "state": state, "metrics": m})
    refresh_trust(path=path)
    return _json_safe({"ok": True, "synced": len(synced), "items": synced})


def _settled_for_strategy(strategy_id: str, path: Path | None = None) -> tuple[int, int]:
    """Count hit/n for predictions whose strategy_version contains strategy_id."""
    rows = store.list_predictions(status="resolved", path=path, include_synthetic=False, limit=2000)
    hits = 0
    n = 0
    key = strategy_id.replace("factorlab.", "")
    for p in rows:
        sv = p.get("strategy_version") or ""
        # match factorlab.{name} or factorlab.{name}.shadow
        if sv == strategy_id or sv.startswith(strategy_id + ".") or (
            key and (sv == f"factorlab.{key}" or sv.startswith(f"factorlab.{key}."))
        ):
            if p.get("claim_type") != "direction":
                continue
            oc = p.get("outcome") or {}
            if "hit" not in oc:
                continue
            n += 1
            if oc.get("hit"):
                hits += 1
    return hits, n


def evaluate_challengers(path: Path | None = None) -> dict[str, Any]:
    """Binomial + Holm gate vs random; must not lose to champion hit rate."""
    refresh_trust(path=path)
    champ = store.get_strategy("lgbm_planC", path=path) or {}
    champ_rate = champ.get("rolling_hit_rate")
    lib = _load_factor_lib()
    discovered = lib.get("discovered") or {}

    challengers = [
        s
        for s in store.list_strategies(path=path)
        if s.get("state") == "challenger" and str(s.get("strategy_id", "")).startswith("factorlab.")
    ]
    evals = []
    for s in challengers:
        sid = s["strategy_id"]
        name = sid.replace("factorlab.", "", 1)
        fac = discovered.get(name) or {}
        m = fac.get("metrics") or {}
        hits, n = _settled_for_strategy(sid, path=path)
        # If no agent settlements yet, use n=0 (gate fails on sample) but still surface IC
        ev = eval_hit_rate_challenger(
            strategy_id=sid,
            hits=hits,
            n=n,
            champion_hit_rate=champ_rate,
            oos_rank_ic=m.get("oos_rank_ic"),
        )
        evals.append(ev)

    gated = apply_holm(evals)
    # persist rolling stats onto strategy rows
    now = datetime.now(TZ).isoformat()
    by_id = {g["strategy_id"]: g for g in gated}
    for s in challengers:
        g = by_id.get(s["strategy_id"])
        if not g:
            continue
        store.upsert_strategy(
            {
                **s,
                "rolling_n": g["n"],
                "rolling_hit_rate": g["hit_rate"],
                "wilson_low": None,
                "wilson_high": None,
                "pause_reason": g["reason"]
                + (f"；oos_ic={g.get('oos_rank_ic')}" if g.get("oos_rank_ic") is not None else ""),
                "updated_at": now,
            },
            path=path,
        )
    store.set_meta("last_challenger_eval", now, path=path)
    return _json_safe({
        "ok": True,
        "champion_hit_rate": champ_rate,
        "challengers": gated,
        "eligible_to_promote": [g["strategy_id"] for g in gated if g["pass_gate"]],
    })


def promote_challenger(strategy_id: str, *, path: Path | None = None, force: bool = False) -> dict[str, Any]:
    """Promote factorlab challenger into agent trust weight slot.

    Does NOT demote lgbm_planC (still the signal emitter). Other factorlab
    champions are returned to challenger.
    """
    report = evaluate_challengers(path=path)
    eligible = set(report["eligible_to_promote"])
    if not force and strategy_id not in eligible:
        return {
            "ok": False,
            "error": "未通过多重检验门",
            "report": report,
        }
    row = store.get_strategy(strategy_id, path=path)
    if not row:
        return {"ok": False, "error": "strategy not found"}
    if not str(strategy_id).startswith("factorlab."):
        return {"ok": False, "error": "仅支持晋升 factorlab.* challenger"}
    now = datetime.now(TZ).isoformat()
    for s in store.list_strategies(path=path):
        sid = s.get("strategy_id") or ""
        if sid.startswith("factorlab.") and sid != strategy_id and s.get("state") == "champion":
            store.upsert_strategy(
                {
                    **s,
                    "state": "challenger",
                    "trust_weight": 0.0,
                    "pause_reason": f"被 {strategy_id} 替换 @ {now}",
                    "updated_at": now,
                },
                path=path,
            )
    store.upsert_strategy(
        {
            **row,
            "state": "champion",
            "trust_weight": 1.0,
            "pause_reason": f"晋升自 challenger @ {now}（信号源仍为 lgbm_planC）",
            "updated_at": now,
        },
        path=path,
    )
    return {"ok": True, "promoted": strategy_id, "report": report}


def list_research_queue(path: Path | None = None) -> dict[str, Any]:
    """Payload for Research UI."""
    try:
        sync = sync_from_factor_lab(path=path)
    except Exception as exc:  # noqa: BLE001
        sync = {"ok": False, "error": str(exc), "synced": 0, "items": []}
    try:
        gate = evaluate_challengers(path=path)
    except Exception as exc:  # noqa: BLE001
        gate = {"ok": False, "error": str(exc), "challengers": [], "eligible_to_promote": []}
    return _json_safe({
        "sync": sync,
        "gate": gate,
        "strategies": store.list_strategies(path=path),
    })
