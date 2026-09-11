"""sector_forecast 读写：predictions / models / eval / job。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

QUANT = Path(__file__).resolve().parents[2]
ROOT = QUANT / "data" / "overlays" / "sector_forecast"
TZ = ZoneInfo("Asia/Shanghai")


def _now() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")


def ensure_dirs() -> Path:
    for sub in ("predictions", "models", "eval"):
        (ROOT / sub).mkdir(parents=True, exist_ok=True)
    return ROOT


def pred_path(day: str) -> Path:
    return ROOT / "predictions" / f"{day}.json"


def save_prediction(day: str, payload: dict[str, Any]) -> Path:
    ensure_dirs()
    payload = dict(payload)
    payload["day"] = day
    payload.setdefault("generated", _now())
    path = pred_path(day)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    path.with_suffix(".done").write_text(_now() + "\n")
    return path


def load_prediction(day: str) -> dict[str, Any] | None:
    path = pred_path(day)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def list_pred_days(limit: int = 80) -> list[str]:
    d = ROOT / "predictions"
    if not d.exists():
        return []
    return sorted((p.stem for p in d.glob("20*.json")), reverse=True)[:limit]


def latest_pred_day() -> str | None:
    days = list_pred_days(1)
    return days[0] if days else None


def load_latest() -> tuple[str | None, dict[str, Any] | None]:
    day = latest_pred_day()
    if not day:
        return None, None
    return day, load_prediction(day)


def model_dir() -> Path:
    ensure_dirs()
    return ROOT / "models"


def eval_path(name: str = "scorecard.json") -> Path:
    ensure_dirs()
    return ROOT / "eval" / name


def save_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return path


def load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def append_settled(row: dict[str, Any]) -> None:
    ensure_dirs()
    path = ROOT / "eval" / "settled.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


JOB_FILE = ROOT / "job.json"


def read_job() -> dict[str, Any]:
    if not JOB_FILE.exists():
        return {"status": "idle", "message": "暂无任务"}
    try:
        return json.loads(JOB_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {"status": "idle", "message": "状态文件损坏"}


def write_job(payload: dict[str, Any]) -> dict[str, Any]:
    ensure_dirs()
    cur = dict(payload)
    cur["updated_at"] = _now()
    JOB_FILE.write_text(json.dumps(cur, ensure_ascii=False, indent=2) + "\n")
    return cur
