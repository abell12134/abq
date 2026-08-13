"""Settlement caliber — bump only with full historical recompute (see recompute.py)."""

from __future__ import annotations

# v1: excess vs benchmark, halt-skip horizon
# v1.1: + limit-up/down Critic notes; ST/退市事件日提前结算
CALIBER = "caliber.v1.1.events"
CALIBER_LEGACY = "caliber.v1.direction_excess"
HORIZON_DEFAULT = 10
TOP_K_EMIT = 30  # top / bottom each side from daily signals
SHADOW_MIN_N = 30
SHADOW_MIN_DAYS = 40

BENCHMARK_MAP = {
    "CSI500": "SH000905",
    "CSI300": "SH000300",
    "CSI_ALL": "SH000985",
    "中证500": "SH000905",
    "沪深300": "SH000300",
    "中证全A": "SH000985",
    "SH000905": "SH000905",
    "SH000300": "SH000300",
    "SH000985": "SH000985",
}

DEFAULT_BENCHMARK = "CSI500"
STRATEGY_VERSION = "lgbm_planC.champion"
FEATURE_VERSION = "alpha158_plus_lab"
