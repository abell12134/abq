"""研究分析任务状态（看板进度条用）。克隆 swing_hunter/job.py 的并发安全写。"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from .store import ROOT

JOB_FILE = ROOT / "job.json"
LOCK_FILE = ROOT / "job.json.lock"
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
        text = JOB_FILE.read_text()
        decoder = json.JSONDecoder()
        obj, _ = decoder.raw_decode(text.lstrip())
        return obj if isinstance(obj, dict) else {"status": "idle", "message": "状态文件损坏"}
    except (json.JSONDecodeError, ValueError, TypeError, OSError):
        return {"status": "idle", "message": "状态文件损坏"}


def _locked_write(payload: dict[str, Any], *, only_if_running: bool = False) -> dict[str, Any]:
    import fcntl

    ensure_dir()
    LOCK_FILE.touch(exist_ok=True)
    with LOCK_FILE.open("a+") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            cur = read_job()
            if only_if_running and cur.get("status") != "running":
                return cur
            cur.update(payload)
            cur["updated_at"] = _now()
            text = json.dumps(cur, ensure_ascii=False, indent=2) + "\n"
            tmp = JOB_FILE.parent / f".job.{os.getpid()}.{uuid4().hex[:8]}.tmp"
            try:
                tmp.write_text(text)
                os.replace(tmp, JOB_FILE)
            finally:
                if tmp.exists():
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
            return cur
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


def write_job(payload: dict[str, Any], *, only_if_running: bool = False) -> dict[str, Any]:
    last_err: Exception | None = None
    for attempt in range(5):
        try:
            return _locked_write(payload, only_if_running=only_if_running)
        except OSError as e:  # noqa: PERF203
            last_err = e
            time.sleep(0.02 * (attempt + 1))
    assert last_err is not None
    raise last_err


def start_job(*, account: str | None = None, dry_run: bool = False,
              day: str | None = None) -> dict[str, Any]:
    job = {
        "id": uuid4().hex[:12],
        "status": "running",
        "account": account,
        "day": day,
        "dry_run": dry_run,
        "started_at": _now(),
        "finished_at": None,
        "pct": 2,
        "message": "任务已启动…",
        "phase": "start",
        "current": None,
        "current_name": "",
        "done_count": 0,
        "total": 0,
        "n_buy": 0,
        "n_sell": 0,
        "n_hold": 0,
        "last_line": "",
        "last_action": None,
    }
    return write_job(job)


def finish_job(ok: bool = True, message: str | None = None) -> dict[str, Any]:
    return write_job({
        "status": "done" if ok else "error",
        "pct": 100 if ok else max(int(read_job().get("pct") or 0), 5),
        "message": message or ("研究分析完成" if ok else "研究分析失败"),
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
        "pct": 12 if total else 90,
        "message": f"待研究分析 {total} 只…" if total else "无可研究标的",
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
    pct = 12 + int(83 * i / total)
    pct = min(94, max(12, pct))
    label = f"{instrument} {name}".strip()
    job = read_job()
    counts = {
        "n_buy": int(job.get("n_buy") or 0),
        "n_sell": int(job.get("n_sell") or 0),
        "n_hold": int(job.get("n_hold") or 0),
    }
    if action in ("buy", "sell", "hold"):
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
    }, only_if_running=True)


def update_from_line(line: str) -> dict[str, Any] | None:
    """根据 run_research 日志行推进进度（web 子进程 tee 时用）。"""
    line = (line or "").rstrip()
    if not line:
        return None
    job = read_job()
    if job.get("status") != "running":
        return None
    patch: dict[str, Any] = {"last_line": line[:240]}

    if "研究宇宙" in line:
        m = re.search(r"研究宇宙\s+(\d+)", line)
        total = int(m.group(1)) if m else int(job.get("total") or 0)
        patch.update(total=total, pct=10, message=f"研究宇宙 {total} 只…", phase="universe")
    elif "待研究分析" in line or "待 LLM 深析" in line:
        m = re.search(r"(?:待研究分析|待 LLM 深析)\s+(\d+)", line)
        total = int(m.group(1)) if m else int(job.get("total") or 0)
        patch.update(total=total, done_count=0, pct=12,
                     message=f"待研究分析 {total} 只…", phase="llm")
    elif re.search(r"·\s*\[(\d+)/(\d+)\]\s+(SH\d{6}|SZ\d{6})", line):
        m = re.search(
            r"·\s*\[(\d+)/(\d+)\]\s+(SH\d{6}|SZ\d{6})\s*([^:]*):"
            r"\s*action=(\w+).*?\|\s*(.*)$",
            line,
        )
        if m:
            i, total, inst, name, action, reason = m.groups()
            return tick_llm(
                int(i), int(total),
                instrument=inst, name=(name or "").strip(),
                action=action, reason=(reason or "").strip(),
            )
    elif "[DONE]" in line:
        patch.update(pct=98, phase="finishing")
    elif "[FAIL]" in line:
        patch.update(message=f"部分失败：{line[:120]}", phase="llm")

    return write_job(patch, only_if_running=True)
