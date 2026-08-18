"""研究宇宙构建：合并 舆情跟踪 / 短线猎手 / 当日订单 三源，去重并标 sources。

返回每只票：{instrument, sources[], swing_action, swing_score, order_side, sentiment_score}
全部经 sentiment_memory.run_memory.normalize_instrument 归一化（幂等）。
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


def _normalize(raw: str) -> str | None:
    from overlays.sentiment_memory.run_memory import normalize_instrument
    return normalize_instrument(raw)


def build_research_universe(
    day: str,
    account: str | None = None,
) -> list[dict[str, Any]]:
    """合并三源去重。day 为研究日（用于读 swing 预测与订单）。"""
    universe: dict[str, dict[str, Any]] = {}

    def _entry(inst: str) -> dict[str, Any]:
        return universe.setdefault(inst, {
            "instrument": inst, "sources": [],
            "swing_action": None, "swing_score": None,
            "order_side": None, "sentiment_score": None,
        })

    # 1) 舆情跟踪
    try:
        from overlays.sentiment_memory import store as sm_store
        cat = sm_store.load_catalog()
        for inst, entry in (cat.get("instruments") or {}).items():
            n = _normalize(inst)
            if not n:
                continue
            e = _entry(n)
            if "sentiment" not in e["sources"]:
                e["sources"].append("sentiment")
            e["sentiment_score"] = entry.get("score")
    except Exception as e:  # noqa: BLE001
        log.info("舆情 catalog 读取失败: %s", e)

    # 2) 短线猎手 predict/watch
    try:
        from overlays.swing_hunter.schema import latest_prediction_day, read_predictions
        d = latest_prediction_day() or day
        pf = read_predictions(d)
        if pf:
            for p in pf.predictions:
                if p.action not in ("predict", "watch"):
                    continue
                n = _normalize(p.instrument)
                if not n:
                    continue
                e = _entry(n)
                if "swing" not in e["sources"]:
                    e["sources"].append("swing")
                e["swing_action"] = p.action
                e["swing_score"] = p.swing_score
    except Exception as e:  # noqa: BLE001
        log.info("swing 预测读取失败: %s", e)

    # 3) 当日订单（BUY+SELL）
    if account:
        try:
            import common as C  # ops.common
            dirs = C.account_subdirs(account)
            order_day = C.resolve_order_day(account, day)
            if order_day:
                of = dirs["orders"] / f"{order_day}.csv"
                if of.exists():
                    import pandas as pd
                    o = pd.read_csv(of)
                    if "instrument" in o.columns:
                        for _, row in o.iterrows():
                            n = _normalize(str(row["instrument"]))
                            if not n:
                                continue
                            e = _entry(n)
                            if "orders" not in e["sources"]:
                                e["sources"].append("orders")
                            e["order_side"] = str(row.get("side", "")).upper() or None
        except Exception as e:  # noqa: BLE001
            log.info("订单读取失败 account=%s: %s", account, e)

    # 过滤非法 + 排序：有订单动作 > swing predict > 其余
    def _key(e: dict[str, Any]) -> tuple:
        src_rank = 0
        if "orders" in e["sources"]:
            src_rank = 3
        elif e.get("swing_action") == "predict":
            src_rank = 2
        elif "swing" in e["sources"]:
            src_rank = 1
        return (-src_rank, e["instrument"])

    out = [e for e in universe.values()
           if len(e["instrument"]) >= 8 and e["instrument"][:2] in {"SH", "SZ"}]
    out.sort(key=_key)
    return out
