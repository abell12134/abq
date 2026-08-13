"""Multiple-testing gates for challenger promotion (deterministic)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


def binom_sf_two_sided(hits: int, n: int, p0: float = 0.5) -> float:
    """Two-sided binomial p-value via normal approx with continuity (n large) or exact sum for n<=200."""
    if n <= 0:
        return 1.0
    if n <= 200:
        # exact two-sided: sum probs as extreme as observed
        from math import comb

        phat = hits / n
        p_obs = comb(n, hits) * (p0**hits) * ((1 - p0) ** (n - hits))
        total = 0.0
        for k in range(n + 1):
            pk = comb(n, k) * (p0**k) * ((1 - p0) ** (n - k))
            if pk <= p_obs + 1e-15:
                total += pk
        return min(1.0, total)
    # normal approx
    mu = n * p0
    var = n * p0 * (1 - p0)
    z = abs(hits - mu) / math.sqrt(var)
    # erfc for two-sided
    return math.erfc(z / math.sqrt(2))


def holm_bonferroni(pvalues: list[float], alpha: float = 0.05) -> list[bool]:
    """Return reject flags for each hypothesis (Holm step-down)."""
    m = len(pvalues)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: pvalues[i])
    reject = [False] * m
    for rank, idx in enumerate(order):
        thresh = alpha / (m - rank)
        if pvalues[idx] <= thresh:
            reject[idx] = True
        else:
            break  # step-down stops
    # Holm: once fail, all later fail — already handled by break; earlier rejects stay
    return reject


@dataclass
class ChallengerEval:
    strategy_id: str
    n: int
    hits: int
    hit_rate: float | None
    p_value: float
    pass_gate: bool
    reason: str
    oos_rank_ic: float | None = None
    champion_hit_rate: float | None = None


def eval_hit_rate_challenger(
    *,
    strategy_id: str,
    hits: int,
    n: int,
    champion_hit_rate: float | None,
    oos_rank_ic: float | None = None,
    min_n: int = 30,
    p0: float = 0.5,
) -> ChallengerEval:
    if n < min_n:
        return ChallengerEval(
            strategy_id=strategy_id,
            n=n,
            hits=hits,
            hit_rate=None if n == 0 else hits / n,
            p_value=1.0,
            pass_gate=False,
            reason=f"样本不足 n={n}<{min_n}",
            oos_rank_ic=oos_rank_ic,
            champion_hit_rate=champion_hit_rate,
        )
    rate = hits / n
    p = binom_sf_two_sided(hits, n, p0=p0)
    # one-sided preference: must beat random AND not worse than champion
    beat_random = rate > p0 and p < 0.05  # raw; Holm applied across set later
    beat_champ = champion_hit_rate is None or rate >= champion_hit_rate
    ok = beat_random and beat_champ
    reason = "通过单因子检验（待 Holm 校正）" if ok else (
        "未显著优于随机" if not beat_random else "未优于 champion 命中率"
    )
    return ChallengerEval(
        strategy_id=strategy_id,
        n=n,
        hits=hits,
        hit_rate=rate,
        p_value=p,
        pass_gate=ok,
        reason=reason,
        oos_rank_ic=oos_rank_ic,
        champion_hit_rate=champion_hit_rate,
    )


def apply_holm(evals: list[ChallengerEval], alpha: float = 0.05) -> list[dict[str, Any]]:
    """Apply Holm across challengers that have n>=min; others keep fail."""
    indexed = [(i, e) for i, e in enumerate(evals) if e.n >= 30 and e.hit_rate is not None]
    pvals = [e.p_value for _, e in indexed]
    rejects = holm_bonferroni(pvals, alpha=alpha)
    reject_map = {indexed[j][0]: rejects[j] for j in range(len(indexed))}
    out = []
    for i, e in enumerate(evals):
        holm_ok = reject_map.get(i, False) and e.pass_gate
        # pass_gate already requires beat champ; Holm tightens significance
        if e.n >= 30 and e.hit_rate is not None and e.hit_rate > 0.5:
            # require Holm reject for final
            final = holm_ok and (e.champion_hit_rate is None or e.hit_rate >= e.champion_hit_rate)
        else:
            final = False
        out.append(
            {
                "strategy_id": e.strategy_id,
                "n": e.n,
                "hits": e.hits,
                "hit_rate": None if e.hit_rate is None else round(e.hit_rate, 4),
                "p_value": round(e.p_value, 6),
                "holm_reject": reject_map.get(i, False),
                "pass_gate": final,
                "reason": (
                    "通过 Holm 多重检验门 + 优于/持平 champion"
                    if final
                    else e.reason + ("；Holm 未通过" if e.n >= 30 and not reject_map.get(i, False) else "")
                ),
                "oos_rank_ic": e.oos_rank_ic,
                "champion_hit_rate": e.champion_hit_rate,
            }
        )
    return out
