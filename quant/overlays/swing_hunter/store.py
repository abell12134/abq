"""swing_hunter 持久化：tracker（每票全生命周期）+ catalog（看板目录与统计）。

布局（data/overlays/swing_hunter/）：
  tracker/{instrument}.json   records: [TrackRecord...]（永久累积）
  catalog.json                latest_date / 活跃预测 / 累计统计
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from .schema import (
    ACTIVE_STATES,
    HIT_PCT,
    QUANT,  # noqa: F401  (re-export 给 run_swing)
    ROOT,
    TrackRecord,
)

TZ = ZoneInfo("Asia/Shanghai")


def _now() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")


def ensure_dirs() -> Path:
    for sub in ("predictions", "tracker"):
        (ROOT / sub).mkdir(parents=True, exist_ok=True)
    return ROOT


# ---------------- tracker ----------------


def _tracker_path(instrument: str) -> Path:
    return ROOT / "tracker" / f"{instrument.upper()}.json"


def load_tracker(instrument: str) -> dict[str, Any]:
    path = _tracker_path(instrument)
    if not path.exists():
        return {"instrument": instrument.upper(), "records": []}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return {"instrument": instrument.upper(), "records": []}


def save_tracker(instrument: str, data: dict[str, Any]) -> Path:
    path = _tracker_path(instrument)
    path.parent.mkdir(parents=True, exist_ok=True)
    data["instrument"] = instrument.upper()
    data["updated_at"] = _now()
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return path


def upsert_record(rec: TrackRecord) -> None:
    """按 (instrument, pred_date) 幂等写入一条跟踪记录。"""
    data = load_tracker(rec.instrument)
    records = [TrackRecord.from_dict(r) for r in data.get("records", [])]
    for i, old in enumerate(records):
        if old.pred_date == rec.pred_date:
            records[i] = rec
            break
    else:
        records.append(rec)
    records.sort(key=lambda r: r.pred_date, reverse=True)
    save_tracker(rec.instrument, {"records": [r.to_dict() for r in records]})


def all_active_records() -> list[TrackRecord]:
    """扫描 tracker 目录，返回全部活跃（triggered/holding）记录。"""
    d = ROOT / "tracker"
    if not d.exists():
        return []
    out: list[TrackRecord] = []
    for f in d.glob("*.json"):
        try:
            data = json.loads(f.read_text())
        except json.JSONDecodeError:
            continue
        for r in data.get("records", []):
            rec = TrackRecord.from_dict(r)
            if rec.state in ACTIVE_STATES:
                out.append(rec)
    return out


def all_records(limit_per_stock: int = 20) -> list[TrackRecord]:
    d = ROOT / "tracker"
    if not d.exists():
        return []
    out: list[TrackRecord] = []
    for f in d.glob("*.json"):
        try:
            data = json.loads(f.read_text())
        except json.JSONDecodeError:
            continue
        recs = [TrackRecord.from_dict(r) for r in data.get("records", [])]
        recs.sort(key=lambda r: r.pred_date, reverse=True)
        out.extend(recs[:limit_per_stock])
    out.sort(key=lambda r: r.pred_date, reverse=True)
    return out


# ---------------- catalog & 统计 ----------------


def compute_stats(records: Iterable[TrackRecord] | None = None) -> dict[str, Any]:
    """累计验证统计（收盘口径）。样本不足时如实展示，不做结论。"""
    recs = list(records) if records is not None else all_records()
    done = [r for r in recs if r.result in {"hit", "stopped", "expired"}]
    hits = [r for r in done if r.result == "hit"]
    tier2 = [r for r in hits if (r.hit_tier or 0) >= 2]
    rets = [r.result_return for r in done if r.result_return is not None]
    stat = {
        "total_predictions": len(recs),
        "active": len([r for r in recs if r.state in ACTIVE_STATES]),
        "settled": len(done),
        "hit": len(hits),
        "hit_tier2": len(tier2),
        "stopped": len([r for r in done if r.result == "stopped"]),
        "expired": len([r for r in done if r.result == "expired"]),
        "hit_rate": round(len(hits) / len(done), 4) if done else None,
        "avg_return": round(sum(rets) / len(rets), 4) if rets else None,
        "hit_target": HIT_PCT,
        "note": "样本 ≥60 笔后才可评估有效性" if len(done) < 60 else "样本量已达评估门槛",
    }
    return stat


def update_catalog(day: str) -> Path:
    ensure_dirs()
    active = all_active_records()
    stat = compute_stats()
    cat = {
        "latest_date": day,
        "active": [
            {
                "instrument": r.instrument,
                "name": r.name,
                "pred_date": r.pred_date,
                "state": r.state,
                "confidence": r.confidence,
                "swing_score": r.swing_score,
                "catalysts": r.catalysts,
                "entry_price": r.entry_price,
                "days_held": r.days_held,
                "mfe": r.mfe,
                "hit_tier": r.hit_tier,
                "latest_delta": (r.deltas[-1] if r.deltas else None),
            }
            for r in sorted(active, key=lambda x: (-x.swing_score, x.instrument))
        ],
        "stats": stat,
        "updated_at": _now(),
    }
    path = ROOT / "catalog.json"
    path.write_text(json.dumps(cat, ensure_ascii=False, indent=2) + "\n")
    return path


def load_catalog() -> dict[str, Any]:
    path = ROOT / "catalog.json"
    if not path.exists():
        return {"latest_date": None, "active": [], "stats": compute_stats([]),
                "updated_at": None}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return {"latest_date": None, "active": [], "stats": {}, "updated_at": None}
