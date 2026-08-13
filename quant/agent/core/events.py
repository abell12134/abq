"""Market event helpers for settlement — limit-up/down annotation + ST early settle.

Caliber v1.1 (events):
  - Hit still uses close prices (no fill simulation)
  - Annotate entry/resolve limit-up/down for Critic
  - If ST/delist event falls in [entry, planned_resolve], settle early on event day
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def limit_pct(instrument: str) -> float:
    code = instrument[2:] if len(instrument) > 2 else instrument
    inst = instrument.upper()
    if inst.startswith("BJ") or code.startswith(("688", "689", "300", "301")):
        return 0.20
    return 0.10


@dataclass
class EventFlags:
    """Per-day flags keyed by YYYY-MM-DD."""

    limit_up: dict[str, bool] = field(default_factory=dict)
    limit_down: dict[str, bool] = field(default_factory=dict)
    # first calendar/trading day instrument became ST or delisted within window
    st_event_day: str | None = None
    delist_event_day: str | None = None
    notes: list[str] = field(default_factory=list)


def flags_from_closes(
    instrument: str,
    closes: dict[str, float],
    *,
    tolerance: float = 0.97,
) -> EventFlags:
    """Infer limit-up/down from consecutive closes (golden-test friendly)."""
    days = sorted(closes)
    lim = limit_pct(instrument) * tolerance
    lu: dict[str, bool] = {}
    ld: dict[str, bool] = {}
    for i, d in enumerate(days):
        c = closes.get(d)
        if c is None or not (c == c) or c <= 0:
            continue
        if i == 0:
            lu[d] = False
            ld[d] = False
            continue
        prev = closes.get(days[i - 1])
        if prev is None or not (prev == prev) or prev <= 0:
            lu[d] = False
            ld[d] = False
            continue
        ret = c / prev - 1.0
        lu[d] = bool(ret >= lim)
        ld[d] = bool(ret <= -lim)
    return EventFlags(limit_up=lu, limit_down=ld)


def detect_st_from_name(name: str | None) -> bool:
    if not name:
        return False
    u = name.upper()
    return "ST" in u or "退" in name


def annotate_outcome(
    outcome: dict[str, Any],
    flags: EventFlags | None,
    *,
    early_reason: str | None = None,
) -> dict[str, Any]:
    out = dict(outcome)
    if early_reason:
        out["early_settle_reason"] = early_reason
    if not flags:
        return out
    entry = out.get("entry_date")
    resolve = out.get("resolve_at")
    critic: list[str] = list(out.get("event_notes") or [])
    if entry:
        if flags.limit_up.get(entry):
            critic.append(f"入场日 {entry} 涨停（收盘判定，不模拟成交）")
            out["entry_limit_up"] = True
        if flags.limit_down.get(entry):
            critic.append(f"入场日 {entry} 跌停（收盘判定，不模拟成交）")
            out["entry_limit_down"] = True
    if resolve:
        if flags.limit_up.get(resolve):
            critic.append(f"结算日 {resolve} 涨停（收盘判定）")
            out["resolve_limit_up"] = True
        if flags.limit_down.get(resolve):
            critic.append(f"结算日 {resolve} 跌停（收盘判定）")
            out["resolve_limit_down"] = True
    if flags.st_event_day:
        out["st_event_day"] = flags.st_event_day
    if flags.delist_event_day:
        out["delist_event_day"] = flags.delist_event_day
    critic.extend(flags.notes)
    if critic:
        out["event_notes"] = critic
    return out
