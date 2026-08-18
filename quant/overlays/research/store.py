"""研究报告持久化。

布局（均在 data/overlays/research/）：
  reports/{instrument}/{date}.json   个股研究报告（含中英双裁决）
  catalog.json                       标的目录（最近报告摘要）
  {date}.done                        当日已跑哨兵（幂等）
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

QUANT = Path(__file__).resolve().parents[2]
ROOT = QUANT / "data" / "overlays" / "research"
TZ = ZoneInfo("Asia/Shanghai")


def ensure_dirs() -> Path:
    (ROOT / "reports").mkdir(parents=True, exist_ok=True)
    return ROOT


def _now() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")


def _report_dir(instrument: str) -> Path:
    return ROOT / "reports" / instrument.upper()


def save_report(report: dict[str, Any]) -> Path:
    ensure_dirs()
    inst = str(report.get("instrument", "")).upper()
    day = str(report.get("date", ""))
    d = _report_dir(inst)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{day}.json"
    payload = dict(report)
    payload["instrument"] = inst
    payload["date"] = day
    payload["saved_at"] = _now()
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    _update_catalog(inst, day, payload)
    return path


def load_report(instrument: str, day: str | None = None) -> dict[str, Any] | None:
    d = _report_dir(instrument)
    if not d.exists():
        return None
    if day:
        p = d / f"{day}.json"
        return json.loads(p.read_text()) if p.exists() else None
    files = sorted(d.glob("????-??-??.json"), reverse=True)
    return json.loads(files[0].read_text()) if files else None


def list_reports(instrument: str, limit: int = 30) -> list[dict[str, Any]]:
    d = _report_dir(instrument)
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob("????-??-??.json"), reverse=True)[:limit]:
        try:
            obj = json.loads(p.read_text())
        except json.JSONDecodeError:
            continue
        vc = obj.get("verdict_cn") or {}
        out.append({
            "date": obj.get("date") or p.stem,
            "merged_direction": obj.get("merged_direction"),
            "merged_confidence": obj.get("merged_confidence"),
            "consensus": obj.get("consensus"),
            "action_cn": (vc.get("action") if isinstance(vc, dict) else None),
            "pred_id": obj.get("pred_id"),
        })
    return out


def _update_catalog(instrument: str, day: str, report: dict[str, Any]) -> None:
    ensure_dirs()
    path = ROOT / "catalog.json"
    cat: dict[str, Any] = {"instruments": {}}
    if path.exists():
        try:
            cat = json.loads(path.read_text())
        except json.JSONDecodeError:
            pass
    inst = instrument.upper()
    vc = report.get("verdict_cn") or {}
    ve = report.get("verdict_en") or {}
    entry = cat.setdefault("instruments", {}).setdefault(inst, {})
    entry.update({
        "instrument": inst,
        "name": report.get("name") or entry.get("name", ""),
        "latest_date": day,
        "sources": report.get("sources") or [],
        "merged_direction": report.get("merged_direction"),
        "merged_confidence": report.get("merged_confidence"),
        "consensus": report.get("consensus"),
        "action_cn": vc.get("action") if isinstance(vc, dict) else None,
        "action_en": ve.get("action") if isinstance(ve, dict) else None,
        "pred_id": report.get("pred_id"),
        "updated_at": report.get("saved_at"),
    })
    cat["updated_at"] = _now()
    path.write_text(json.dumps(cat, ensure_ascii=False, indent=2))


def load_catalog() -> dict[str, Any]:
    ensure_dirs()
    path = ROOT / "catalog.json"
    if not path.exists():
        return {"instruments": {}, "updated_at": None}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return {"instruments": {}, "updated_at": None}


def list_tracked_instruments() -> list[str]:
    return sorted(load_catalog().get("instruments", {}).keys())


def mark_done(day: str) -> Path:
    ensure_dirs()
    p = ROOT / f"{day}.done"
    p.touch()
    return p


def is_done(day: str) -> bool:
    return (ROOT / f"{day}.done").exists()


def latest_research_day() -> str | None:
    """最新有报告的日期。"""
    d = ROOT / "reports"
    if not d.exists():
        return None
    days: set[str] = set()
    for inst_dir in d.iterdir():
        if not inst_dir.is_dir():
            continue
        for f in inst_dir.glob("????-??-??.json"):
            days.add(f.stem)
    return sorted(days)[-1] if days else None
