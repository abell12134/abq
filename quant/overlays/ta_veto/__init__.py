"""TradingAgents-inspired qualitative buy veto overlay for A-shares.

Does not replace LGBM ranking. Consumed by make_trade_plan after UMP.
Zero-key research: announcements + news + fundamentals; multi-round debate.
"""

from .schema import (
    RISK_TAGS,
    VetoDecision,
    VetoFile,
    apply_veto_policy,
    load_vetoed_instruments,
    veto_dir,
    veto_path,
    write_veto_file,
)

__all__ = [
    "RISK_TAGS",
    "VetoDecision",
    "VetoFile",
    "apply_veto_policy",
    "load_vetoed_instruments",
    "veto_dir",
    "veto_path",
    "write_veto_file",
]
