"""达标案例挖掘 → swing_patterns.yaml（类比 factor_lab 轻量版）。

来源：
  1. mine_from_hit — 猎手预测 hit 终态自动写入（status=candidate）
  2. mine_from_live_fills — 实盘 fills BUY→SELL 达标回合（status=live_case）
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .schema import (
    HIT_PCT,
    HIT_PCT_TIER2,
    HIT_PCT_TIER3,
    QUANT,
    ROOT,
    TrackRecord,
)

PATTERNS_PATH = QUANT / "overlays" / "swing_hunter" / "swing_patterns.yaml"
TZ = ZoneInfo("Asia/Shanghai")

sys.path.insert(0, str(QUANT / "ops"))


def _load_yaml() -> dict[str, Any]:
    if not PATTERNS_PATH.exists():
        return {"patterns": [], "updated_at": None}
    try:
        import yaml
        return yaml.safe_load(PATTERNS_PATH.read_text()) or {"patterns": []}
    except Exception:  # noqa: BLE001
        return {"patterns": []}


def _save_yaml(data: dict[str, Any]) -> None:
    import yaml
    data["updated_at"] = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
    PATTERNS_PATH.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )


def _append_mirror(entry: dict[str, Any]) -> None:
    mirror = ROOT / "patterns_mined.jsonl"
    mirror.parent.mkdir(parents=True, exist_ok=True)
    with mirror.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _hit_tier(ret: float) -> int:
    if ret >= HIT_PCT_TIER3:
        return 3
    if ret >= HIT_PCT_TIER2:
        return 2
    if ret >= HIT_PCT:
        return 1
    return 0


def _upsert_pattern(entry: dict[str, Any]) -> dict[str, Any] | None:
    """按 id 幂等追加；已存在则返回 None。"""
    pid = entry.get("id")
    data = _load_yaml()
    patterns = data.get("patterns") or []
    if any(p.get("id") == pid for p in patterns):
        return None
    patterns.append(entry)
    data["patterns"] = patterns
    _save_yaml(data)
    _append_mirror(entry)
    return entry


def mine_from_hit(
    rec: TrackRecord,
    prediction: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """hit 终态时写入一条候选模式；重复 (instrument,pred_date) 幂等。"""
    if rec.result != "hit":
        return None
    pred = prediction or {}
    pid = f"hit_{rec.pred_date.replace('-', '')}_{rec.instrument}"
    entry = {
        "id": pid,
        "status": "candidate",
        "source": "swing_hit",
        "mined_at": datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "instrument": rec.instrument,
        "name": rec.name,
        "pred_date": rec.pred_date,
        "result": rec.result,
        "result_return": rec.result_return,
        "hit_tier": rec.hit_tier,
        "days_held": rec.days_held,
        "catalysts": rec.catalysts or pred.get("catalysts") or [],
        "confidence": rec.confidence,
        "swing_score": rec.swing_score,
        "factor_brief": pred.get("factor_brief") or {},
        "reasons": rec.reasons or pred.get("reasons") or [],
        "risk_tags": pred.get("risk_tags") or [],
        "notes": "自动从 hit 案例挖掘；需样本外验证后才可晋升 live",
    }
    return _upsert_pattern(entry)


def _load_fill_legs(account: str) -> list[dict[str, Any]]:
    """按日读取 fills，返回带 day 的成交腿列表。"""
    import common as C  # noqa: WPS433
    import pandas as pd

    fills_dir = C.ensure_account_dirs(account)["fills"]
    if not fills_dir.exists():
        return []
    legs: list[dict[str, Any]] = []
    for path in sorted(fills_dir.glob("????-??-??.csv")):
        day = path.stem
        try:
            df = pd.read_csv(path)
        except Exception:  # noqa: BLE001
            continue
        if df.empty or "instrument" not in df.columns:
            continue
        for _, row in df.iterrows():
            side = str(row.get("side", "")).upper()
            if side not in {"BUY", "SELL"}:
                continue
            try:
                price = float(row["price"])
                shares = float(row.get("shares", 0) or 0)
            except (TypeError, ValueError, KeyError):
                continue
            if price <= 0 or shares <= 0:
                continue
            legs.append({
                "day": day,
                "instrument": str(row["instrument"]).upper(),
                "side": side,
                "price": price,
                "shares": shares,
            })
    return legs


def _pair_round_trips(legs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """FIFO 配对 BUY→SELL 回合（按 instrument）。"""
    from collections import defaultdict, deque

    open_buys: dict[str, deque] = defaultdict(deque)
    trips: list[dict[str, Any]] = []
    for leg in legs:
        inst = leg["instrument"]
        if leg["side"] == "BUY":
            open_buys[inst].append(leg)
            continue
        remaining = leg["shares"]
        while remaining > 1e-9 and open_buys[inst]:
            buy = open_buys[inst][0]
            matched = min(remaining, buy["shares"])
            ret = leg["price"] / buy["price"] - 1.0
            trips.append({
                "instrument": inst,
                "buy_day": buy["day"],
                "sell_day": leg["day"],
                "buy_price": buy["price"],
                "sell_price": leg["price"],
                "shares": matched,
                "result_return": round(ret, 4),
            })
            buy["shares"] -= matched
            remaining -= matched
            if buy["shares"] <= 1e-9:
                open_buys[inst].popleft()
    return trips


def _trading_days_held(buy_day: str, sell_day: str) -> int:
    """买卖日之间的交易日计数（含卖出日，不含买入日；至少 1）。"""
    try:
        import common as C  # noqa: WPS433
        C.init_qlib()
        from qlib.data import D
        cal = [str(x)[:10] for x in D.calendar(
            freq="day", start_time=buy_day, end_time=sell_day)]
        # 持有日 = 买入次日到卖出日
        held = [d for d in cal if d > buy_day and d <= sell_day]
        return max(1, len(held))
    except Exception:  # noqa: BLE001
        # 回退：自然日粗估
        try:
            delta = (datetime.strptime(sell_day, "%Y-%m-%d")
                     - datetime.strptime(buy_day, "%Y-%m-%d")).days
            return max(1, delta)
        except ValueError:
            return 1


def _lookup_name(inst: str) -> str:
    try:
        from overlays.sentiment_memory.run_memory import _lookup_names  # noqa: WPS433
        return _lookup_names([inst]).get(inst, "") or ""
    except Exception:  # noqa: BLE001
        return ""


def mine_from_live_fills(
    account: str = "live_manual_10k",
    min_ret: float = HIT_PCT,
) -> list[dict[str, Any]]:
    """从实盘 fills 挖掘 BUY→SELL 达标回合 → status=live_case。

    幂等键：live_{buyDate}_{instrument}
    """
    legs = _load_fill_legs(account)
    trips = _pair_round_trips(legs)
    written: list[dict[str, Any]] = []

    # 批量拉入场日特征（信号日≈买入前一交易日，用买入日特征即可）
    feat_by_inst_day: dict[tuple[str, str], dict[str, Any]] = {}
    need = [(t["instrument"], t["buy_day"]) for t in trips
            if t["result_return"] >= min_ret]
    if need:
        try:
            from overlays.swing_hunter.candidates import load_price_features  # noqa: WPS433
            by_day: dict[str, list[str]] = {}
            for inst, day in need:
                by_day.setdefault(day, []).append(inst)
            for day, insts in by_day.items():
                feats = load_price_features(sorted(set(insts)), day)
                for inst, feat in feats.items():
                    feat_by_inst_day[(inst, day)] = feat
        except Exception:  # noqa: BLE001
            pass

    for t in trips:
        ret = float(t["result_return"])
        if ret < min_ret:
            continue
        buy_day = t["buy_day"]
        inst = t["instrument"]
        pid = f"live_{buy_day.replace('-', '')}_{inst}"
        tier = _hit_tier(ret)
        days_held = _trading_days_held(buy_day, t["sell_day"])
        # 信号日：买入日前一自然日近似；夹具用买卖日即可
        signal_day = buy_day
        try:
            import common as C  # noqa: WPS433
            C.init_qlib()
            from qlib.data import D
            start = (datetime.strptime(buy_day, "%Y-%m-%d") - timedelta(days=10)
                     ).strftime("%Y-%m-%d")
            cal_ext = [str(x)[:10] for x in D.calendar(
                freq="day", start_time=start, end_time=buy_day)]
            prev = [d for d in cal_ext if d < buy_day]
            if prev:
                signal_day = prev[-1]
        except Exception:  # noqa: BLE001
            pass

        entry = {
            "id": pid,
            "status": "live_case",
            "source": "live_fill",
            "mined_at": datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"),
            "account": account,
            "instrument": inst,
            "name": _lookup_name(inst),
            "signal_day": signal_day,
            "buy_day": buy_day,
            "sell_day": t["sell_day"],
            "buy_price": t["buy_price"],
            "sell_price": t["sell_price"],
            "result": "hit",
            "result_return": ret,
            "hit_tier": tier,
            "days_held": days_held,
            "catalysts": [],
            "factor_brief": feat_by_inst_day.get((inst, buy_day), {}),
            "reasons": [
                f"实盘 {buy_day}@{t['buy_price']} → {t['sell_day']}@{t['sell_price']} "
                f"({ret * 100:+.1f}%)"
            ],
            "risk_tags": [],
            "notes": "实盘 fills 达标回合；用于候选加权与评测夹具",
        }
        if _upsert_pattern(entry):
            written.append(entry)
    return written


def load_patterns(
    limit: int = 30,
    statuses: set[str] | None = None,
) -> list[dict[str, Any]]:
    data = _load_yaml()
    patterns = data.get("patterns") or []
    if statuses:
        patterns = [p for p in patterns if p.get("status") in statuses]
    patterns = sorted(
        patterns,
        key=lambda p: str(p.get("mined_at") or ""),
        reverse=True,
    )
    return patterns[:limit] if limit else patterns
