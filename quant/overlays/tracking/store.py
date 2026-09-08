"""持仓追踪快照与任务状态 I/O。"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from . import ANALYZE_JOB_FILE, JOB_FILE, ROOT, SNAPSHOT_FILE

TZ = ZoneInfo("Asia/Shanghai")


def _now() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")


def ensure_dir() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)


# ---------------- snapshot ----------------

def load_snapshot() -> dict[str, Any] | None:
    if not SNAPSHOT_FILE.exists():
        return None
    try:
        return json.loads(SNAPSHOT_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def save_snapshot(payload: dict[str, Any]) -> Path:
    ensure_dir()
    payload = dict(payload)
    payload["updated_at"] = _now()
    tmp = SNAPSHOT_FILE.parent / f".snapshot.{os.getpid()}.{uuid4().hex[:8]}.tmp"
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    os.replace(tmp, SNAPSHOT_FILE)
    return SNAPSHOT_FILE


# ---------------- job ----------------

def _idle() -> dict[str, Any]:
    return {"status": "idle", "message": "暂无任务"}


def _read_job_file(path: Path) -> dict[str, Any]:
    ensure_dir()
    if not path.exists():
        return _idle()
    try:
        text = path.read_text()
        decoder = json.JSONDecoder()
        obj, _ = decoder.raw_decode(text.lstrip())
        return obj if isinstance(obj, dict) else {"status": "idle", "message": "状态文件损坏"}
    except (json.JSONDecodeError, ValueError, TypeError, OSError):
        return {"status": "idle", "message": "状态文件损坏"}


def _locked_write_file(path: Path, payload: dict[str, Any], *,
                       only_if_running: bool = False) -> dict[str, Any]:
    import fcntl

    ensure_dir()
    lock = path.parent / f"{path.name}.lock"
    lock.touch(exist_ok=True)
    with lock.open("a+") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            cur = _read_job_file(path)
            if only_if_running and cur.get("status") != "running":
                return cur
            cur.update(payload)
            cur["updated_at"] = _now()
            text = json.dumps(cur, ensure_ascii=False, indent=2) + "\n"
            tmp = path.parent / f".{path.stem}.{os.getpid()}.{uuid4().hex[:8]}.tmp"
            try:
                tmp.write_text(text)
                os.replace(tmp, path)
            finally:
                if tmp.exists():
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
            return cur
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


def _write_job_file(path: Path, payload: dict[str, Any], *,
                    only_if_running: bool = False) -> dict[str, Any]:
    last_err: Exception | None = None
    for attempt in range(5):
        try:
            return _locked_write_file(path, payload, only_if_running=only_if_running)
        except OSError as e:  # noqa: PERF203
            last_err = e
            time.sleep(0.02 * (attempt + 1))
    assert last_err is not None
    raise last_err


def read_job() -> dict[str, Any]:
    return _read_job_file(JOB_FILE)


def write_job(payload: dict[str, Any], *, only_if_running: bool = False) -> dict[str, Any]:
    return _write_job_file(JOB_FILE, payload, only_if_running=only_if_running)


def read_analyze_job() -> dict[str, Any]:
    return _read_job_file(ANALYZE_JOB_FILE)


def write_analyze_job(payload: dict[str, Any], *, only_if_running: bool = False) -> dict[str, Any]:
    return _write_job_file(ANALYZE_JOB_FILE, payload, only_if_running=only_if_running)


def start_job() -> dict[str, Any]:
    return write_job({
        "id": uuid4().hex[:12],
        "kind": "snapshot",
        "status": "running",
        "started_at": _now(),
        "finished_at": None,
        "pct": 2,
        "message": "任务已启动…",
        "phase": "start",
        "done_count": 0,
        "total": 0,
        "current": None,
        "current_name": "",
    })


def tick(i: int, total: int, *, instrument: str, name: str = "", message: str = "") -> dict[str, Any]:
    total = max(int(total), 1)
    i = max(0, int(i))
    pct = 8 + int(88 * i / total)
    pct = min(96, max(8, pct))
    label = f"{instrument} {name}".strip()
    msg = f"[{i}/{total}] {label}"
    if message:
        msg += f" · {message[:80]}"
    return write_job({
        "done_count": i,
        "total": total,
        "pct": pct,
        "current": instrument,
        "current_name": name,
        "message": msg,
        "phase": "build",
    }, only_if_running=True)


def finish_job(ok: bool = True, message: str | None = None, **extra: Any) -> dict[str, Any]:
    return write_job({
        "status": "done" if ok else "error",
        "pct": 100 if ok else max(int(read_job().get("pct") or 0), 5),
        "message": message or ("追踪快照完成" if ok else "追踪快照失败"),
        "finished_at": _now(),
        "phase": "done" if ok else "error",
        **extra,
    })


def start_analyze_job(*, accounts: list[str], instruments: list[str],
                      names: dict[str, str] | None = None) -> dict[str, Any]:
    return write_analyze_job({
        "id": uuid4().hex[:12],
        "kind": "analyze",
        "status": "running",
        "started_at": _now(),
        "finished_at": None,
        "pct": 2,
        "message": f"准备分析 {len(instruments)} 只（实盘线 + TA线持仓）",
        "phase": "start",
        "accounts": accounts,
        "instruments": instruments,
        "names": names or {},
        "done_count": 0,
        "total": len(instruments),
        "current": None,
        "current_name": "",
        "n_sent_ok": 0,
        "n_rs_ok": 0,
        "n_swing_ok": 0,
    })


_ANALYZE_BANDS = {
    "sentiment": (5, 28),
    "research": (28, 82),
    "swing": (82, 96),
    "attach": (96, 99),
}


def tick_analyze(phase: str, i: int, total: int, *, instrument: str = "",
                 name: str = "", message: str = "", **extra: Any) -> dict[str, Any]:
    total = max(int(total), 1)
    i = max(0, int(i))
    lo, hi = _ANALYZE_BANDS.get(phase, (5, 96))
    pct = lo + int((hi - lo) * i / total)
    pct = min(hi, max(lo, pct))
    label = f"{instrument} {name}".strip()
    phase_cn = {"sentiment": "舆情", "research": "研究", "swing": "短线", "attach": "回写"}.get(phase, phase)
    msg = f"{phase_cn} [{i}/{total}]"
    if label:
        msg += f" {label}"
    if message:
        msg += f" · {message[:80]}"
    payload = {
        "done_count": i,
        "total": total,
        "pct": pct,
        "phase": phase,
        "current": instrument or None,
        "current_name": name,
        "message": msg,
    }
    payload.update(extra)
    return write_analyze_job(payload, only_if_running=True)


def finish_analyze_job(ok: bool = True, message: str | None = None, **extra: Any) -> dict[str, Any]:
    return write_analyze_job({
        "status": "done" if ok else "error",
        "pct": 100 if ok else max(int(read_analyze_job().get("pct") or 0), 5),
        "message": message or ("持仓分析完成" if ok else "持仓分析失败"),
        "finished_at": _now(),
        "phase": "done" if ok else "error",
        **extra,
    })
