"""达标案例挖掘 → swing_patterns.yaml（类比 factor_lab 轻量版）。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .schema import QUANT, ROOT, TrackRecord

PATTERNS_PATH = QUANT / "overlays" / "swing_hunter" / "swing_patterns.yaml"
TZ = ZoneInfo("Asia/Shanghai")


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


def mine_from_hit(
    rec: TrackRecord,
    prediction: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """hit 终态时写入一条候选模式；重复 (instrument,pred_date) 幂等。"""
    if rec.result != "hit":
        return None
    pred = prediction or {}
    pid = f"hit_{rec.pred_date.replace('-', '')}_{rec.instrument}"
    data = _load_yaml()
    patterns = data.get("patterns") or []
    if any(p.get("id") == pid for p in patterns):
        return None

    entry = {
        "id": pid,
        "status": "candidate",
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
    patterns.append(entry)
    data["patterns"] = patterns
    _save_yaml(data)

    # 同步 JSON 镜像（看板/API 读取）
    mirror = ROOT / "patterns_mined.jsonl"
    mirror.parent.mkdir(parents=True, exist_ok=True)
    with mirror.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def load_patterns(limit: int = 30) -> list[dict[str, Any]]:
    data = _load_yaml()
    patterns = sorted(
        data.get("patterns") or [],
        key=lambda p: str(p.get("mined_at") or ""),
        reverse=True,
    )
    return patterns[:limit]
