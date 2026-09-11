"""看板任务状态。"""

from __future__ import annotations

from typing import Any

from . import store


def start(message: str = "正在构建行业特征并预测…") -> dict[str, Any]:
    return store.write_job({"status": "running", "pct": 5, "message": message})


def tick(pct: int, message: str) -> dict[str, Any]:
    cur = store.read_job()
    cur.update({"status": "running", "pct": pct, "message": message})
    return store.write_job(cur)


def finish(ok: bool, message: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = {"status": "ok" if ok else "error", "pct": 100, "message": message}
    if extra:
        payload.update(extra)
    return store.write_job(payload)
