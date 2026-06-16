"""阶段3 因子库：因子的注册、状态机与持久化（factors.yaml）。

每个因子是一条 Qlib 表达式 + 元数据（经济逻辑、各项指标、准入状态）。
状态机（对应 §3.3 五道准入关卡的产物）：
    candidate      LLM 刚提出，未评估
    rejected       未过自动关卡（初筛/去重/样本外），记 reject_reason
    passed_auto    过了初筛+去重+样本外，等待人工评审（关卡4）
    paper_tracking 人工评审通过，进入纸面跟踪期（关卡5），暂不参与实盘模型
    live           纸面跟踪达标，正式参与实盘模型

`seed` 段是代表现有 Alpha158 因子族的种子因子，仅用于"去重"基准
（新因子与它们相关性 >0.7 视为重复），不参与状态流转。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import numpy as np
import yaml


def _coerce(v):
    """把 numpy 标量转成原生 Python 类型，便于 yaml 序列化。"""
    if isinstance(v, np.floating):
        return float(v)
    if isinstance(v, np.integer):
        return int(v)
    return v

HERE = Path(__file__).resolve().parent
LIB_PATH = HERE / "factors.yaml"

STATUSES = ["candidate", "rejected", "passed_auto", "paper_tracking", "live"]

# 代表现有因子库（Alpha158 各因子族）的种子因子——去重基准
SEED_FACTORS = {
    "seed_mom20": {"expr": "$close/Ref($close,20)-1", "category": "momentum"},
    "seed_rev5": {"expr": "Ref($close,5)/$close-1", "category": "reversal"},
    "seed_vol20": {"expr": "Std($close/Ref($close,1)-1,20)", "category": "volatility"},
    "seed_volratio": {"expr": "Mean($volume,5)/(Mean($volume,20)+1e-12)",
                      "category": "volume"},
    "seed_maratio": {"expr": "$close/Mean($close,20)", "category": "trend"},
    "seed_pxpos": {"expr": "($close-Min($low,20))/(Max($high,20)-Min($low,20)+1e-12)",
                   "category": "position"},
    "seed_amihud": {"expr": "Mean(Abs($close/Ref($close,1)-1)/($volume*$close+1),10)",
                    "category": "liquidity"},
    "seed_rsi": {"expr": "Mean(Greater($close-Ref($close,1),0),14)/"
                         "(Mean(Abs($close-Ref($close,1)),14)+1e-12)",
                 "category": "momentum"},
}


def _empty_lib() -> dict:
    return {"seed": SEED_FACTORS, "discovered": {}}


def load_lib() -> dict:
    if not LIB_PATH.exists():
        lib = _empty_lib()
        save_lib(lib)
        return lib
    lib = yaml.safe_load(LIB_PATH.read_text()) or _empty_lib()
    lib.setdefault("seed", SEED_FACTORS)
    lib.setdefault("discovered", {})
    return lib


def save_lib(lib: dict) -> None:
    LIB_PATH.write_text(yaml.safe_dump(lib, allow_unicode=True, sort_keys=False))


def existing_exprs(lib: dict, include_rejected: bool = True) -> dict[str, str]:
    """用于去重的"现有因子"表达式集合：种子 + 已发现（可含已拒绝，避免重复提）。"""
    out = {n: f["expr"] for n, f in lib["seed"].items()}
    for n, f in lib["discovered"].items():
        if include_rejected or f.get("status") != "rejected":
            out[n] = f["expr"]
    return out


def known_names(lib: dict) -> list[str]:
    return list(lib["seed"]) + list(lib["discovered"])


def upsert(lib: dict, name: str, expr: str, hypothesis: str, category: str,
           status: str, metrics: dict | None = None, reject_reason: str = "",
           created_iter: int = 0) -> None:
    lib["discovered"][name] = {
        "expr": expr,
        "hypothesis": hypothesis,
        "category": category,
        "status": status,
        "metrics": {k: _coerce(v) for k, v in (metrics or {}).items()},
        "reject_reason": reject_reason,
        "created_iter": created_iter,
        "updated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


def by_status(lib: dict, status: str) -> dict:
    return {n: f for n, f in lib["discovered"].items() if f.get("status") == status}
