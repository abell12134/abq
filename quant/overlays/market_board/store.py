"""market_board 持久化：盘后快照 JSON 契约（.done 标记），与 swing_hunter 同款风格。

布局（data/overlays/market_board/）：
  daily/YYYY-MM-DD.json       盘后全量快照（温度计/周期/梯队/强势）
  daily/YYYY-MM-DD.done       完成标记
  intraday/latest.json        盘中实时快照（P2 手动刷新写入）
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

QUANT = Path(__file__).resolve().parents[2]
ROOT = QUANT / "data" / "overlays" / "market_board"

TZ = ZoneInfo("Asia/Shanghai")


def _now() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")


def ensure_dirs() -> Path:
    for sub in ("daily", "intraday"):
        (ROOT / sub).mkdir(parents=True, exist_ok=True)
    return ROOT


# ---------------- 盘后快照 ----------------

def daily_path(day: str) -> Path:
    return ROOT / "daily" / f"{day}.json"


def save_daily(day: str, payload: dict[str, Any]) -> Path:
    ensure_dirs()
    payload["day"] = day
    payload["generated"] = _now()
    path = daily_path(day)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    path.with_suffix(".done").write_text(_now() + "\n")
    return path


def load_daily(day: str) -> dict[str, Any] | None:
    path = daily_path(day)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


def latest_daily_day() -> str | None:
    d = ROOT / "daily"
    if not d.exists():
        return None
    days = sorted(p.stem for p in d.glob("20*.json"))
    return days[-1] if days else None


def load_latest_daily() -> tuple[str | None, dict[str, Any] | None]:
    day = latest_daily_day()
    if not day:
        return None, None
    return day, load_daily(day)


def list_daily_days(limit: int = 40) -> list[str]:
    d = ROOT / "daily"
    if not d.exists():
        return []
    return sorted((p.stem for p in d.glob("20*.json")), reverse=True)[:limit]


# ---------------- 盘中快照（P2） ----------------

def save_intraday(payload: dict[str, Any]) -> Path:
    ensure_dirs()
    payload["generated"] = _now()
    path = ROOT / "intraday" / "latest.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return path


def load_intraday() -> dict[str, Any] | None:
    path = ROOT / "intraday" / "latest.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


def intraday_is_fresh(data: dict[str, Any] | None, *, session_day: str | None = None) -> bool:
    """盘中快照是否仍属当日会话（跨日则视为过期，避免展示旧实时徽章）。"""
    if not data or not data.get("ok"):
        return False
    today = session_day or datetime.now(TZ).strftime("%Y-%m-%d")
    if data.get("session_day"):
        return data["session_day"] == today
    return (data.get("generated") or "")[:10] == today
