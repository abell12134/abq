"""日度发出行业预测：特征 → 模型打分 → Top3 up/avoid × 双期限。"""

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from . import features as F
from . import model as M
from . import store
from .schema import (
    BENCHMARK,
    BENCHMARK_NAME,
    CALIBER,
    DISCLAIMER,
    FEATURE_COLS,
    HORIZON_10,
    HORIZON_20,
    HORIZONS,
    LOOKBACK_DAYS,
    MODEL_VERSION,
    TILT_NOTE,
    TOP_K,
    UNIVERSE,
    claim_type_for,
    evidence_lines,
    pick_claims,
    ForecastFile,
)

log = logging.getLogger(__name__)


def _horizon_shadow(meta: dict[str, Any] | None, horizon: int) -> tuple[bool, str]:
    if not meta:
        return True, "无模型成绩单"
    ev = (meta.get("eval") or {}).get(f"h{horizon}") or {}
    return bool(ev.get("shadow", True)), str(ev.get("shadow_reason") or "该期限未过门")


def _attach_tilt(by_industry: dict[str, Any], day: str) -> None:
    """东财概念/快讯按行业归并，只写证据，不改 p_beat。"""
    try:
        from overlays.market_board import pool as pool_mod
        from overlays.market_board import store as board_store
    except Exception:  # noqa: BLE001
        return
    snap = board_store.load_daily(day)
    if not snap:
        return
    pool = pool_mod.load_pool(day)
    code_ind = {p["instrument"][2:]: p["industry"] for p in pool}
    concepts = ((snap.get("themes") or {}).get("concepts")) or []
    hits: dict[str, list[str]] = {}
    for c in concepts[:20]:
        name = str(c.get("name") or "")
        if not name:
            continue
        for code in c.get("pool_limit_list") or []:
            ind = code_ind.get(str(code))
            if ind and ind in by_industry:
                hits.setdefault(ind, [])
                if name not in hits[ind]:
                    hits[ind].append(name)
    news_n: dict[str, int] = {}
    for item in (snap.get("news") or {}).get("pool_feed") or []:
        inst = str(item.get("instrument") or "")
        ind = code_ind.get(inst[2:] if len(inst) > 6 else inst)
        if not ind:
            ind = item.get("industry")
        if ind:
            news_n[ind] = news_n.get(ind, 0) + 1
    for ind, rec in by_industry.items():
        rec["related_concepts"] = hits.get(ind, [])[:4]
        rec["news_hits"] = int(news_n.get(ind, 0))
        rec["tilt_note"] = TILT_NOTE


def _score_day(day_df: pd.DataFrame, models: dict[str, Any] | None,
               horizon: int) -> pd.DataFrame:
    ready = day_df.dropna(subset=FEATURE_COLS)
    if ready.empty:
        return ready
    if models:
        return M.score_frame(ready, models, horizon)
    return M.heuristic_score(ready, horizon)


def emit(day: str, panel: pd.DataFrame | None = None, *,
         retrain: bool = False, skip_llm: bool = False,
         force_llm: str | None = None) -> dict[str, Any]:
    """对 day 发出预测文件。panel 可复用，避免重复拉 qlib。"""
    if panel is None:
        panel = F.build_panel(day, lookback=LOOKBACK_DAYS)
    models = None if retrain else M.load_models()
    meta: dict[str, Any] | None = None
    errors: list[str] = []
    if models is None or retrain:
        try:
            pack = M.train(panel)
            models = M.load_models()
            meta = pack.get("meta")
        except Exception as e:  # noqa: BLE001
            log.warning("训练失败，改用启发式: %s", e)
            errors.append(f"训练失败: {e}")
            models = None
            meta = None
    else:
        meta = models.get("meta")

    day_df = F.feature_matrix(panel[panel["day"] == day], dropna=False)
    by_industry: dict[str, Any] = {}
    claims: list[dict[str, Any]] = []

    for h in HORIZONS:
        scored = _score_day(day_df, models, h)
        rows = []
        for rec in scored.to_dict("records"):
            rows.append({
                "industry": rec["industry"],
                "n_stocks": int(rec.get("n_stocks") or 0),
                "p_beat": float(rec["p_beat"]),
                "expected_excess": float(rec.get("expected_excess") or 0),
                "horizon_days": h,
                **{k: rec.get(k) for k in (
                    "ret_5d", "ret_10d", "ret_20d", "rs_10d", "accel",
                    "pct_up", "limit_up_share", "crowded")},
            })
        picked = pick_claims(rows)
        sh, why = _horizon_shadow(meta, h)
        if models is None:
            sh, why = True, "启发式兜底，未用截面模型"
        for r in picked:
            ev = evidence_lines(r)
            claims.append({
                "industry": r["industry"],
                "horizon_days": h,
                "claim_type": claim_type_for(h),
                "action": r["action"],
                "rank": r["rank"],
                "p_beat": round(float(r["p_beat"]), 4),
                "expected_excess": round(float(r["expected_excess"]), 4),
                "n_stocks": r["n_stocks"],
                "evidence": ev,
                "drivers": F.row_to_drivers(r),
                "shadow": sh,
                "status": "pending",
                "benchmark": BENCHMARK,
                "benchmark_name": BENCHMARK_NAME,
            })
        # 全行业分数备查
        action_map = {x["industry"]: x for x in picked}
        for rec in scored.to_dict("records"):
            ind = rec["industry"]
            slot = by_industry.setdefault(ind, {"industry": ind, "n_stocks": int(rec.get("n_stocks") or 0)})
            picked_row = action_map.get(ind)
            slot[f"h{h}"] = {
                "p_beat": round(float(rec["p_beat"]), 4),
                "expected_excess": round(float(rec.get("expected_excess") or 0), 4),
                "action": picked_row["action"] if picked_row else "neutral",
                "rank": picked_row["rank"] if picked_row else None,
                "shadow": sh,
                "evidence": evidence_lines(rec) if picked_row else evidence_lines(rec, 2),
            }

    _attach_tilt(by_industry, day)
    for c in claims:
        extra = by_industry.get(c["industry"]) or {}
        c["related_concepts"] = extra.get("related_concepts") or []
        c["news_hits"] = extra.get("news_hits") or 0

    scorecard = (meta or {}).get("eval") or {}
    file_shadow = all(_horizon_shadow(meta, h)[0] for h in HORIZONS) if meta else True
    reason = (meta or {}).get("shadow_reason") or (
        "无有效模型成绩单" if not meta else "")
    if models is None:
        file_shadow, reason = True, "启发式兜底，未用截面模型"

    payload = ForecastFile(
        day=day,
        shadow=file_shadow,
        shadow_reason=reason,
        claims=claims,
        by_industry=by_industry,
        scorecard=scorecard,
        model_version=MODEL_VERSION,
        caliber=CALIBER,
        errors=errors,
        status="ok" if not errors else "partial",
    ).to_dict()
    payload["universe"] = UNIVERSE
    payload["top_k"] = TOP_K
    payload["disclaimer"] = DISCLAIMER
    from . import brief as B
    B.attach(payload, day, force_llm=force_llm, skip=skip_llm)
    store.save_prediction(day, payload)
    if meta:
        store.save_json(store.eval_path("scorecard.json"), {
            "as_of": day, "source": "OOS", **meta,
        })
    return payload
