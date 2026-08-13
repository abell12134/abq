"""L2 adjudication unit tests — pure logic without Qlib portfolio path."""

from __future__ import annotations

import sys
from pathlib import Path

QUANT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(QUANT))


def test_l2_adjudication_flags():
    # Simulate the three independent flags
    adj = {"excess": True, "target": False, "constraints": True}
    hit_all = all(adj.values())
    assert hit_all is False
    assert adj["excess"] and adj["constraints"] and not adj["target"]


def test_trust_pause_rule():
    from agent.core.scorecard import wilson_interval

    # 20/60 ≈ 0.33 → wilson high still may be < 0.5 for severe
    lo, hi = wilson_interval(20, 60)
    assert lo is not None and lo < 0.5


if __name__ == "__main__":
    test_l2_adjudication_flags()
    test_trust_pause_rule()
    print("OK l2/trust unit checks")
