"""每日 delta 跟踪：仅对「今日新增」公告/舆情做轻量 LLM 更新（省 token）。"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from overlays.sentiment_memory import store as sm_store

from . import analyze as A
from . import sentiment_prep as SP
from .schema import ACTIVE_STATES, QUANT, ROOT, TrackRecord
from . import store

log = logging.getLogger(__name__)
TZ = ZoneInfo("Asia/Shanghai")
DELTA_DIR = ROOT / "deltas"


def _delta_path(day: str, instrument: str) -> Path:
    return DELTA_DIR / day / f"{instrument.upper()}.json"


def load_seen_ids(rec: TrackRecord) -> set[str]:
    ids = set(rec.seen_news_ids or [])
    for d in rec.deltas or []:
        for iid in d.get("new_item_ids") or []:
            ids.add(str(iid))
    return ids


def find_new_items(instrument: str, seen: set[str], lookback: int = 7) -> list[dict[str, Any]]:
    raw = sm_store.load_raw(lookback_days=lookback, instrument=instrument.upper())
    out = []
    for it in raw:
        iid = str(it.get("id") or "")
        if not iid or iid in seen:
            continue
        out.append(it)
    out.sort(key=lambda x: str(x.get("published", "")), reverse=True)
    return out


def run_delta_for_record(
    rec: TrackRecord,
    day: str,
    name: str = "",
    *,
    dry_run: bool = False,
    force_llm: str | None = None,
) -> dict[str, Any] | None:
    """活跃跟踪票：有新条目则 delta LLM；无新条目跳过。"""
    if rec.state not in ACTIVE_STATES:
        return None

    seen = load_seen_ids(rec)
    new_items = find_new_items(rec.instrument, seen)
    if not new_items:
        return None

    # 库内仍不足时先采集
    if len(new_items) < 1:
        SP.collect_instrument(rec.instrument, name or rec.name)
        new_items = find_new_items(rec.instrument, seen)

    if not new_items:
        return None

    last_ret = None
    if rec.daily:
        last_ret = rec.daily[-1].get("ret")
    mfe = rec.mfe

    if dry_run:
        delta = {
            "date": day,
            "instrument": rec.instrument,
            "stance": "hold",
            "headline": f"dry-run：{len(new_items)} 条新增未分析",
            "summary": "",
            "new_item_ids": [it["id"] for it in new_items[:20]],
            "new_items_preview": [
                {"published": it.get("published"), "title": it.get("title"),
                 "source": it.get("source")}
                for it in new_items[:8]
            ],
            "dry_run": True,
        }
    else:
        delta = A.analyze_delta(
            rec.instrument, name or rec.name, day,
            new_items=new_items,
            position_ctx={
                "state": rec.state, "days_held": rec.days_held,
                "entry_price": rec.entry_price, "last_ret": last_ret, "mfe": mfe,
                "catalysts": rec.catalysts, "pred_date": rec.pred_date,
            },
            force_llm=force_llm,
        )

    rec.seen_news_ids = list(seen | {it["id"] for it in new_items})
    rec.deltas = list(rec.deltas or [])
    rec.deltas.append(delta)
    rec.deltas = rec.deltas[-30:]

    path = _delta_path(day, rec.instrument)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(delta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return delta


def run_delta_updates(
    day: str,
    names: dict[str, str] | None = None,
    *,
    dry_run: bool = False,
    force_llm: str | None = None,
) -> dict[str, Any]:
    """全部活跃跟踪票跑 delta；返回摘要。"""
    names = names or {}
    active = store.all_active_records()
    summary = {"day": day, "active": len(active), "updated": 0, "skipped": 0, "details": []}
    for rec in active:
        try:
            delta = run_delta_for_record(
                rec, day, names.get(rec.instrument, rec.name),
                dry_run=dry_run, force_llm=force_llm,
            )
            store.upsert_record(rec)
            if delta:
                summary["updated"] += 1
                summary["details"].append(
                    f"{rec.instrument}: {delta.get('stance')} | {delta.get('headline', '')[:50]}")
            else:
                summary["skipped"] += 1
        except Exception as e:  # noqa: BLE001
            log.warning("delta failed %s: %s", rec.instrument, e)
            summary["details"].append(f"{rec.instrument}: FAIL {e}")
    return summary
