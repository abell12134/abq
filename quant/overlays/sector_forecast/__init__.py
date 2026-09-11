"""板块预测 overlay：申万一级行业 10/20 日相对中证500 超额，可结算。

claim 由截面模型产生，LLM 不参与数值；LLM 只对已选候选写简报。
"""

from __future__ import annotations

from pathlib import Path

QUANT = Path(__file__).resolve().parents[2]
ROOT = QUANT / "data" / "overlays" / "sector_forecast"
