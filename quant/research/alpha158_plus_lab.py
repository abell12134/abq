"""Alpha158 + factor_lab 中 status=live 的因子。

用法：在 workflow_baseline.yaml 把 handler 指到本类；无 live 因子时行为与 Alpha158 一致。
晋升因子后须重训（run_baseline / rolling_retrain）再由 predict_daily 加载新模型。
"""

from __future__ import annotations

import sys
from pathlib import Path

from qlib.contrib.data.handler import Alpha158

QUANT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(QUANT / "factor_lab"))


def _live_feature_exprs() -> list[tuple[str, str]]:
    """返回 [(expr, name), ...]；factor_lab 不可用时返回空。"""
    try:
        import factor_lib as FL
        lib = FL.load_lib()
        live = FL.by_status(lib, "live")
    except Exception:
        return []
    out = []
    for name, fac in live.items():
        expr = (fac or {}).get("expr")
        if not expr:
            continue
        # Qlib 特征名需相对干净；前缀避免与 Alpha158 撞名
        safe = "LAB_" + "".join(c if c.isalnum() or c == "_" else "_" for c in name).upper()
        out.append((expr, safe))
    return out


class Alpha158PlusLab(Alpha158):
    """在 Alpha158 158 维基础上追加 factor_lab live 表达式。"""

    def get_feature_config(self):
        fields, names = super().get_feature_config()
        extra = _live_feature_exprs()
        for expr, name in extra:
            if name in names:
                continue
            fields.append(expr)
            names.append(name)
        return fields, names
