"""舆情分析任务状态（看板进度条用）。"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo
from uuid import uuid4

QUANT = Path(__file__).resolve().parents[2]
ROOT = QUANT / "data" / "overlays" / "sentiment_memory"
JOB_FILE = ROOT / "job.json"
TZ = ZoneInfo("Asia/Shanghai")


def _now() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")


def ensure_dir() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)


def read_job() -> dict[str, Any]:
    ensure_dir()
    if not JOB_FILE.exists():
        return {"status": "idle", "message": "暂无任务"}
    try:
        return json.loads(JOB_FILE.read_text())
    except json.JSONDecodeError:
        return {"status": "idle", "message": "状态文件损坏"}


def write_job(payload: dict[str, Any]) -> dict[str, Any]:
    ensure_dir()
    cur = read_job()
    cur.update(payload)
    cur["updated_at"] = _now()
    JOB_FILE.write_text(json.dumps(cur, ensure_ascii=False, indent=2))
    return cur


def start_job(*, instrument: str | None = None,
              account: str | None = None,
              dry_run: bool = False) -> dict[str, Any]:
    job = {
        "id": uuid4().hex[:12],
        "status": "running",
        "instrument": instrument,
        "account": account,
        "dry_run": dry_run,
        "started_at": _now(),
        "finished_at": None,
        "pct": 3,
        "message": "任务已启动，正在采集…",
        "phase": "start",
        "universe": [],
        "current": None,
        "done_count": 0,
        "total": 0,
        "last_line": "",
    }
    return write_job(job)


def finish_job(ok: bool = True, message: str | None = None) -> dict[str, Any]:
    return write_job({
        "status": "done" if ok else "error",
        "pct": 100 if ok else max(read_job().get("pct") or 0, 5),
        "message": message or ("分析完成" if ok else "分析失败"),
        "finished_at": _now(),
        "phase": "done" if ok else "error",
    })


def update_from_line(line: str) -> dict[str, Any] | None:
    """根据 run_memory 日志行推进进度。"""
    line = (line or "").rstrip()
    if not line:
        return None
    job = read_job()
    if job.get("status") != "running":
        return None
    patch: dict[str, Any] = {"last_line": line[:240]}

    if "全局电报入库" in line or "全局" in line and "入库" in line:
        patch.update(pct=12, message="全局电报/政策入库中…", phase="ingest")
    elif "跟踪标的" in line:
        # [OK] 跟踪标的 8: SH600282 ...
        m = re.search(r"跟踪标的\s+(\d+)", line)
        total = int(m.group(1)) if m else job.get("total") or 0
        codes = re.findall(r"(SH\d{6}|SZ\d{6})", line)
        patch.update(
            pct=20, message=f"待分析 {total or len(codes)} 只…",
            phase="universe", total=total or len(codes),
            universe=codes or job.get("universe") or [],
        )
    elif "舆情" in line and "分析中" in line:
        #   · SH600299 安迪苏: 舆情 39 条 → 分析中…
        m = re.search(r"·\s*(SH\d{6}|SZ\d{6})\s*([^\s:]*)", line)
        cur = m.group(1) if m else None
        name = (m.group(2) if m else "") or ""
        universe = job.get("universe") or []
        done = int(job.get("done_count") or 0)
        total = int(job.get("total") or 0) or max(len(universe), 1)
        # 进入分析阶段：20% + 当前进度
        pct = 20 + int(75 * done / total)
        pct = min(94, max(22, pct))
        label = f"{cur or ''} {name}".strip()
        patch.update(
            pct=pct,
            message=f"正在分析 {label}（{done}/{total}）…",
            phase="analyze",
            current=cur,
        )
    elif "sentiment=" in line:
        done = int(job.get("done_count") or 0) + 1
        total = int(job.get("total") or 0) or 1
        pct = 20 + int(75 * done / total)
        pct = min(96, max(25, pct))
        patch.update(
            done_count=done,
            pct=pct,
            message=f"已完成 {done}/{total}…",
            phase="analyze",
        )
    elif "[DONE]" in line:
        patch.update(pct=99, message="正在收尾…", phase="finishing")
    elif "[FAIL]" in line:
        patch.update(message=f"部分失败：{line[:120]}", phase="analyze")

    return write_job(patch)
