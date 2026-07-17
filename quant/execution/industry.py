"""行业偏离约束：组合 vs 基准（中证500）一级行业权重差。

数据：data/meta/industry_map.csv（instrument,industry）
生成：python execution/build_industry_map.py

小资金 topk 很少时 3% 硬约束几乎不可满足，故默认仅当
strategy.topk >= industry_min_positions（默认 20）时启用。
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

QUANT = Path(__file__).resolve().parents[1]
MAP_PATH = QUANT / "data" / "meta" / "industry_map.csv"

_MAP: dict[str, str] | None = None


def load_industry_map(path: Path | None = None) -> dict[str, str]:
    global _MAP
    p = path or MAP_PATH
    if _MAP is not None and path is None:
        return _MAP
    if not p.exists():
        return {}
    df = pd.read_csv(p)
    if "instrument" not in df.columns or "industry" not in df.columns:
        return {}
    m = {str(r.instrument): str(r.industry) for r in df.itertuples()
         if pd.notna(r.industry) and str(r.industry).strip()}
    if path is None:
        _MAP = m
    return m


def industry_weights(instruments: list[str],
                     shares: dict[str, float] | None = None,
                     prices: pd.Series | None = None,
                     mapping: dict[str, str] | None = None) -> dict[str, float]:
    """等权或市值加权的行业权重。shares/prices 齐全时按市值，否则等权。"""
    mapping = mapping if mapping is not None else load_industry_map()
    if not instruments:
        return {}
    w: dict[str, float] = {}
    if shares is not None and prices is not None:
        vals = {}
        for inst in instruments:
            px = float(prices.get(inst, 0) or 0) if hasattr(prices, "get") else 0.0
            sh = float(shares.get(inst, 0) or 0)
            if px > 0 and sh > 0:
                vals[inst] = px * sh
        total = sum(vals.values()) or 1.0
        for inst, v in vals.items():
            ind = mapping.get(inst, "UNKNOWN")
            w[ind] = w.get(ind, 0.0) + v / total
        return w
    # 等权
    n = len(instruments) or 1
    for inst in instruments:
        ind = mapping.get(inst, "UNKNOWN")
        w[ind] = w.get(ind, 0.0) + 1.0 / n
    return w


def max_deviation(port: dict[str, float], bench: dict[str, float]) -> float:
    keys = set(port) | set(bench)
    if not keys:
        return 0.0
    return max(abs(port.get(k, 0.0) - bench.get(k, 0.0)) for k in keys)


def would_breach(current_insts: list[str], add: str, remove: set[str],
                 max_dev: float, bench: dict[str, float],
                 mapping: dict[str, str] | None = None) -> bool:
    """假设卖出 remove、买入 add 后，行业偏离是否超过 max_dev。"""
    mapping = mapping if mapping is not None else load_industry_map()
    if not mapping or add not in mapping:
        return False  # 无映射时不拦截
    kept = [i for i in current_insts if i not in remove]
    if add not in kept:
        kept.append(add)
    port = industry_weights(kept, mapping=mapping)
    return max_deviation(port, bench) > max_dev + 1e-12
