"""Tests for binomial + Holm gates."""

from __future__ import annotations

import sys
from pathlib import Path

QUANT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(QUANT))

from agent.trust.gates import apply_holm, binom_sf_two_sided, eval_hit_rate_challenger, holm_bonferroni  # noqa: E402


def test_holm_orders():
    # p=[0.01, 0.04, 0.03], alpha=0.05, m=3
    # sorted: 0.01 (i0), 0.03 (i2), 0.04 (i1)
    # 0.01 <= 0.05/3 → reject; 0.03 > 0.05/2 → stop
    flags = holm_bonferroni([0.01, 0.04, 0.03], alpha=0.05)
    assert flags == [True, False, False]


def test_binom_and_eval():
    p = binom_sf_two_sided(40, 60, 0.5)
    assert p < 0.05
    ev = eval_hit_rate_challenger(
        strategy_id="factorlab.x",
        hits=40,
        n=60,
        champion_hit_rate=0.60,
    )
    assert ev.pass_gate is True
    gated = apply_holm([ev])
    assert gated[0]["pass_gate"] is True


def test_sample_insufficient():
    ev = eval_hit_rate_challenger(
        strategy_id="factorlab.y",
        hits=5,
        n=10,
        champion_hit_rate=0.5,
    )
    assert ev.pass_gate is False


if __name__ == "__main__":
    test_holm_orders()
    test_binom_and_eval()
    test_sample_insufficient()
    print("OK gates tests")
