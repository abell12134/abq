"""分析前补齐舆情/公告：无库内条目则采集入库（不调 sentiment_memory 全量 LLM）。"""

from __future__ import annotations

import logging
from typing import Any

from overlays.sentiment_memory import store as sm_store

from . import candidates as CD
from .schema import EVENT_LOOKBACK_DAYS

log = logging.getLogger(__name__)
MIN_ITEMS = 2          # 低于此阈值触发采集
COLLECT_LOOKBACK = 14  # 采集回看天数


def _count_in_store(instrument: str, lookback: int | None = None) -> int:
    lb = lookback or EVENT_LOOKBACK_DAYS
    events = CD.recent_events({instrument.upper()}, lookback_days=lb)
    return len(events.get(instrument.upper(), []))


def collect_instrument(instrument: str, name: str = "", lookback: int = COLLECT_LOOKBACK) -> int:
    """拉取公告/新闻并 append_raw；返回新增条数。"""
    from overlays.sentiment_memory import sources as S

    inst = instrument.upper()
    try:
        items = S.collect_for_instrument(inst, name=name, lookback_days=lookback)
        added = sm_store.append_raw(items)
        log.info("swing sentiment collect %s: fetched=%d added=%d", inst, len(items), added)
        return added
    except Exception as e:  # noqa: BLE001
        log.warning("swing sentiment collect failed %s: %s", inst, e)
        return 0


def refresh_candidate_events(cand: dict[str, Any], lookback: int | None = None) -> dict[str, Any]:
    lb = lookback or EVENT_LOOKBACK_DAYS
    inst = cand["instrument"]
    events = CD.recent_events({inst}, lookback_days=lb).get(inst, [])
    boost, hits = CD.event_boost(events)
    cand["events"] = [
        {"source": e.get("source"), "kind": e.get("kind"),
         "published": e.get("published"), "title": e.get("title"), "url": e.get("url")}
        for e in events[:12]
    ]
    cand["catalyst_hints"] = hits
    cand["event_boost"] = boost
    return cand


def ensure_for_candidates(
    candidates: list[dict[str, Any]],
    names: dict[str, str],
    *,
    min_items: int = MIN_ITEMS,
) -> dict[str, Any]:
    """对候选列表按需采集；返回统计。"""
    collected, skipped, failed = 0, 0, 0
    for c in candidates:
        inst = c["instrument"]
        n_before = len(c.get("events") or [])
        if n_before >= min_items:
            skipped += 1
            continue
        name = names.get(inst, "") or ""
        added = collect_instrument(inst, name)
        if added > 0 or n_before < min_items:
            refresh_candidate_events(c)
            collected += 1
        else:
            failed += 1
    return {"collected": collected, "skipped": skipped, "failed": failed}
