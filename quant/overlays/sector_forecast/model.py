"""10d/20d 行业超额模型：截面 LightGBM + 追涨幅榜基线 OOS。"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from . import store
from .schema import (
    FEATURE_COLS,
    HORIZON_10,
    HORIZON_20,
    MIN_OOS_DAYS,
    MODEL_VERSION,
    OOS_DAYS,
    TOP_K,
    HorizonEval,
    shadow_gate,
)


def _lgb():
    import lightgbm as lgb
    return lgb


def _clf_params() -> dict[str, Any]:
    return dict(
        n_estimators=80,
        num_leaves=8,
        max_depth=3,
        learning_rate=0.05,
        min_child_samples=40,
        subsample=0.8,
        colsample_bytree=0.8,
        random_state=42,
        verbosity=-1,
    )


def _reg_params() -> dict[str, Any]:
    p = _clf_params()
    return p


def _split_oos(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    days = sorted(df["day"].unique())
    if len(days) <= OOS_DAYS + 20:
        cut_i = max(1, int(len(days) * 0.75))
        cut = days[cut_i]
    else:
        cut = days[-OOS_DAYS]
    return df[df["day"] < cut], df[df["day"] >= cut]


def _daily_topk_stats(df: pd.DataFrame, score_col: str, excess_col: str,
                      k: int = TOP_K) -> tuple[float | None, float | None, int]:
    hits, exs = [], []
    for _, g in df.groupby("day"):
        if len(g) < k:
            continue
        top = g.nlargest(k, score_col)
        hits.append(float((top[excess_col] > 0).mean()))
        exs.append(float(top[excess_col].mean()))
    if not hits:
        return None, None, 0
    return float(np.mean(hits)), float(np.mean(exs)), len(hits)


def _fit_horizon(train: pd.DataFrame, oos: pd.DataFrame, horizon: int) -> dict[str, Any]:
    ycol = f"y_{horizon}"
    ecol = f"excess_{horizon}"
    tr = train.dropna(subset=FEATURE_COLS + [ycol, ecol])
    te = oos.dropna(subset=FEATURE_COLS + [ycol, ecol])
    lgb = _lgb()
    clf = lgb.LGBMClassifier(**_clf_params())
    reg = lgb.LGBMRegressor(**_reg_params())
    Xtr = tr[FEATURE_COLS]
    clf.fit(Xtr, tr[ycol].astype(int))
    reg.fit(Xtr, tr[ecol].astype(float))
    eval_row = HorizonEval(horizon=horizon)
    if not te.empty:
        te = te.copy()
        te["p_beat"] = clf.predict_proba(te[FEATURE_COLS])[:, 1]
        te["expected_excess"] = reg.predict(te[FEATURE_COLS])
        m_hit, m_ex, n = _daily_topk_stats(te, "p_beat", ecol)
        c_hit, c_ex, _ = _daily_topk_stats(te, "eq_ret", ecol)
        eval_row.oos_days = n
        eval_row.model_hit = m_hit
        eval_row.model_excess = m_ex
        eval_row.chase_hit = c_hit
        eval_row.chase_excess = c_ex
        eval_row.shadow, eval_row.shadow_reason = shadow_gate(
            model_hit=m_hit, chase_hit=c_hit, oos_days=n)
    else:
        eval_row.shadow, eval_row.shadow_reason = True, "OOS 为空"
    return {
        "clf": clf,
        "reg": reg,
        "eval": eval_row,
    }


def train(panel: pd.DataFrame) -> dict[str, Any]:
    """训练双期限模型并写盘。panel 需含特征与 excess/y 列。"""
    labeled = panel.dropna(subset=FEATURE_COLS)
    pack: dict[str, Any] = {"version": MODEL_VERSION, "features": list(FEATURE_COLS)}
    evals: dict[str, Any] = {}
    for h in (HORIZON_10, HORIZON_20):
        ycol = f"y_{h}"
        sub = labeled.dropna(subset=[ycol, f"excess_{h}"])
        tr, te = _split_oos(sub)
        fitted = _fit_horizon(tr, te, h)
        pack[f"h{h}"] = fitted
        evals[f"h{h}"] = fitted["eval"].to_dict()
        _save_booster(fitted["clf"], f"clf_{h}.txt")
        _save_booster(fitted["reg"], f"reg_{h}.txt")
    shadow = bool(evals.get("h10", {}).get("shadow", True) and evals.get("h20", {}).get("shadow", True))
    # 任一期限过门则主区可用该期限；总 shadow 仅当两期限都失败
    reasons = [evals[k].get("shadow_reason", "") for k in ("h10", "h20") if evals.get(k)]
    meta = {
        "version": MODEL_VERSION,
        "features": list(FEATURE_COLS),
        "eval": evals,
        "shadow": shadow,
        "shadow_reason": "；".join(x for x in reasons if x),
        "min_oos_days": MIN_OOS_DAYS,
        "train_days": int(labeled["day"].nunique()) if not labeled.empty else 0,
        "n_rows": int(len(labeled)),
    }
    store.save_json(store.model_dir() / "meta.json", meta)
    store.save_json(store.eval_path("oos.json"), {"caliber": "OOS 时间切分", **meta})
    pack["meta"] = meta
    return pack


def _save_booster(est, name: str) -> None:
    path = store.model_dir() / name
    est.booster_.save_model(str(path))


def load_models() -> dict[str, Any] | None:
    meta = store.load_json(store.model_dir() / "meta.json")
    if not meta:
        return None
    lgb = _lgb()
    out: dict[str, Any] = {"meta": meta}
    for h in (HORIZON_10, HORIZON_20):
        cp = store.model_dir() / f"clf_{h}.txt"
        rp = store.model_dir() / f"reg_{h}.txt"
        if not cp.exists() or not rp.exists():
            return None
        out[f"clf_{h}"] = lgb.Booster(model_file=str(cp))
        out[f"reg_{h}"] = lgb.Booster(model_file=str(rp))
    return out


def score_frame(df: pd.DataFrame, models: dict[str, Any], horizon: int) -> pd.DataFrame:
    out = df.copy()
    X = out.loc[:, FEATURE_COLS].astype(float).to_numpy()
    out["p_beat"] = models[f"clf_{horizon}"].predict(X)
    out["expected_excess"] = models[f"reg_{horizon}"].predict(X)
    out["horizon_days"] = horizon
    return out


def heuristic_score(df: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """无模型时的可运行兜底：加速 − 拥挤 + 相对强度，经 sigmoid。"""
    out = df.copy()
    accel = out["accel"].fillna(0)
    crowd = out["crowded"].fillna(0)
    rs = out["rs_10d"].fillna(0)
    z = 8 * accel - 6 * crowd + 4 * rs
    out["p_beat"] = 1 / (1 + np.exp(-z.clip(-8, 8)))
    out["expected_excess"] = 0.25 * accel + 0.15 * rs - 0.1 * crowd
    out["horizon_days"] = horizon
    return out
