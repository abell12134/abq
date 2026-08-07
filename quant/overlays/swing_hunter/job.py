"""短线猎手任务状态（看板进度条用）。"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from .schema import ROOT

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
    JOB_FILE.write_text(json.dumps(cur, ensure_ascii=False, indent=2) + "\n")
    return cur


def start_job(*, account: str | None = None, dry_run: bool = False,
              track_only: bool = False, day: str | None = None) -> dict[str, Any]:
    job = {
        "id": uuid4().hex[:12],
        "status": "running",
        "account": account,
        "day": day,
        "dry_run": dry_run,
        "track_only": track_only,
        "started_at": _now(),
        "finished_at": None,
        "pct": 2,
        "message": "任务已启动…",
        "phase": "start",
        "current": None,
        "current_name": "",
        "done_count": 0,
        "total": 0,
        "n_predict": 0,
        "n_watch": 0,
        "n_reject": 0,
        "last_line": "",
        "last_action": None,
    }
    return write_job(job)


def finish_job(ok: bool = True, message: str | None = None) -> dict[str, Any]:
    return write_job({
        "status": "done" if ok else "error",
        "pct": 100 if ok else max(int(read_job().get("pct") or 0), 5),
        "message": message or ("短线猎手完成" if ok else "短线猎手失败"),
        "finished_at": _now(),
        "phase": "done" if ok else "error",
    })


def set_phase(phase: str, message: str, pct: int | None = None) -> dict[str, Any]:
    patch: dict[str, Any] = {"phase": phase, "message": message}
    if pct is not None:
        patch["pct"] = max(0, min(99, int(pct)))
    return write_job(patch)


def set_llm_total(total: int) -> dict[str, Any]:
    total = max(0, int(total))
    return write_job({
        "total": total,
        "done_count": 0,
        "phase": "llm",
        "pct": 15 if total else 90,
        "message": f"待 LLM 深析 {total} 只…" if total else "无可深析候选",
    })


def tick_llm(
    i: int,
    total: int,
    *,
    instrument: str,
    name: str = "",
    action: str = "",
    reason: str = "",
) -> dict[str, Any]:
    total = max(int(total), 1)
    i = max(0, int(i))
    # 15% ~ 92% 分配给 LLM 循环
    pct = 15 + int(77 * i / total)
    pct = min(92, max(15, pct))
    label = f"{instrument} {name}".strip()
    job = read_job()
    counts = {
        "n_predict": int(job.get("n_predict") or 0),
        "n_watch": int(job.get("n_watch") or 0),
        "n_reject": int(job.get("n_reject") or 0),
    }
    if action in ("predict", "watch", "reject"):
        counts[f"n_{action}"] = counts[f"n_{action}"] + 1
    msg = f"[{i}/{total}] {label}: {action or '…'}"
    if reason:
        msg += f" · {reason[:80]}"
    return write_job({
        "done_count": i,
        "total": total,
        "pct": pct,
        "phase": "llm",
        "current": instrument,
        "current_name": name,
        "last_action": action or None,
        "message": msg,
        "last_line": msg[:240],
        **counts,
    })


def update_from_line(line: str) -> dict[str, Any] | None:
    """根据 run_swing 日志行推进进度（web 子进程 tee 时用）。"""
    line = (line or "").rstrip()
    if not line:
        return None
    job = read_job()
    if job.get("status") != "running":
        return None
    patch: dict[str, Any] = {"last_line": line[:240]}

    if "[SKIP]" in line and "同日已有" in line:
        patch.update(pct=95, message="同日已有预测，跳过 LLM…", phase="skip")
    elif "跟踪更新" in line:
        patch.update(pct=8, message="跟踪更新中…", phase="track")
    elif "候选池" in line:
        patch.update(pct=12, message=line.strip()[:120], phase="candidates")
    elif "待 LLM 深析" in line:
        m = re.search(r"待 LLM 深析\s+(\d+)", line)
        total = int(m.group(1)) if m else int(job.get("total") or 0)
        patch.update(
            total=total, done_count=0, pct=15,
            message=f"待 LLM 深析 {total} 只…", phase="llm",
        )
    elif "舆情预采集" in line:
        patch.update(pct=14, message="舆情预采集完成，开始 LLM…", phase="prep")
    elif re.search(r"·\s*\[(\d+)/(\d+)\]\s+(SH\d{6}|SZ\d{6})", line):
        m = re.search(
            r"·\s*\[(\d+)/(\d+)\]\s+(SH\d{6}|SZ\d{6})\s*([^:]*)"
            r":\s*action=(\w+).*?\|\s*(.*)$",
            line,
        )
        if m:
            i, total, inst, name, action, reason = m.groups()
            return tick_llm(
                int(i), int(total),
                instrument=inst, name=(name or "").strip(),
                action=action, reason=(reason or "").strip(),
            )
    elif "[gate]" in line:
        patch.update(pct=94, message="门槛降档裁判中…", phase="gate")
    elif "[DONE]" in line:
        patch.update(pct=98, message="正在写报告…", phase="finishing")
    elif "[WARN] gate" in line:
        patch.update(message=f"gate 告警：{line[:100]}", phase="gate")

    return write_job(patch)
