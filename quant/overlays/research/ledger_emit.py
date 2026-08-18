"""把研究裁决写进 agent_api 可结算账本（L1 direction 预测，shadow 冷启动）。

方向映射：buy→up，sell→down，hold→不发。
target_price 等额外字段 settler 忽略（仅信息性）；结算走 excess-return-vs-benchmark over horizon。
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from agent.core import store
from agent.core.caliber import CALIBER, DEFAULT_BENCHMARK, FEATURE_VERSION

log = logging.getLogger(__name__)
TZ = ZoneInfo("Asia/Shanghai")

STRATEGY_ID = "overlay.research"
STRATEGY_VERSION = "overlay.research.shadow"

_DIR_MAP = {"buy": "up", "sell": "down"}


def register_strategy(*, db_path: Path | None = None) -> None:
    now = datetime.now(TZ).isoformat()
    store.upsert_strategy(
        {
            "strategy_id": STRATEGY_ID,
            "name": "研究分析（多空辩论双裁）",
            "version": STRATEGY_VERSION,
            "state": "shadow",
            "trust_weight": 0.0,  # 冷启动：不进主推荐权重，仅 shadow 计分
            "claim_type": "direction",
            "rolling_n": 0,
            "rolling_hit_rate": None,
            "wilson_low": None,
            "wilson_high": None,
            "pause_reason": None,
            "bad_windows": 0,
            "updated_at": now,
        },
        path=db_path,
    )


def emit_research_prediction(
    report: dict[str, Any],
    *,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """把单只研究报告的 merged 裁决发进账本。hold / parse 全失败 → 不发。"""
    direction = _DIR_MAP.get(str(report.get("merged_direction") or "").lower())
    if not direction:
        return {"ok": False, "reason": f"direction={report.get('merged_direction')} 不发预测"}

    inst = str(report.get("instrument", "")).upper()
    day = str(report.get("date", ""))
    if not inst or not day:
        return {"ok": False, "reason": "缺 instrument/date"}

    pred_id = f"pred_{day.replace('-', '')}_RESEARCH_{inst}_{direction}"
    existing = store.get_prediction(pred_id, path=db_path)
    if existing and existing.get("status") == "resolved":
        return {"ok": False, "reason": "已 resolved，不覆盖", "pred_id": pred_id}

    vc = report.get("verdict_cn") or {}
    ve = report.get("verdict_en") or {}
    if not isinstance(vc, dict):
        vc = {}
    if not isinstance(ve, dict):
        ve = {}

    # horizon：优先 CN 裁决，否则 EN，否则默认
    horizon = vc.get("horizon_days") or ve.get("horizon_days") or 10
    try:
        horizon = int(horizon)
    except (TypeError, ValueError):
        horizon = 10

    confidence = float(report.get("merged_confidence") or 0.0)
    confidence = max(0.0, min(1.0, confidence))

    target_price = vc.get("target_price") or ve.get("target_price")
    risk_tags = list(dict.fromkeys(
        (vc.get("risk_tags") or []) + (ve.get("risk_tags") or [])))[:8]

    claim: dict[str, Any] = {
        "direction": direction,
        "vs": DEFAULT_BENCHMARK,
        "lang_consensus": report.get("consensus"),
        "target_price": target_price,  # 信息性，settler 忽略
        "risk_tags": risk_tags,
        "verdict_cn_action": vc.get("action"),
        "verdict_en_action": ve.get("action"),
    }

    now = datetime.now(TZ).isoformat()
    pred = {
        "pred_id": pred_id,
        "level": "L1",
        "object": inst,
        "object_name": report.get("name") or "",
        "claim_type": "direction",
        "claim": claim,
        "horizon": horizon,
        "benchmark": DEFAULT_BENCHMARK,
        "settlement_caliber": CALIBER,
        "confidence": confidence,
        "raw_confidence": confidence,
        "strategy_version": STRATEGY_VERSION,
        "feature_snapshot": {
            "feature_version": FEATURE_VERSION,
            "pit_timestamp": f"{day}T15:00:00+08:00",
            "sources": report.get("sources") or [],
            "overlay": "research",
        },
        "created_at": now,
        "pred_date": day,
        "resolve_at": None,
        "status": "shadow",
        "outcome": None,
        "failure_conditions": [
            "研究分析 shadow 预测，不进主推荐权重",
            "晋升须积累足量 resolved 样本并通过二项门",
        ],
        "critic_notes": [
            f"merged from CN+EN debate; consensus={report.get('consensus')}",
            f"sources={','.join(report.get('sources') or []) or 'none'}",
        ],
        "synthetic": False,
    }
    store.upsert_prediction(pred, path=db_path)
    log.info("研究预测入库 %s direction=%s conf=%.3f", pred_id, direction, confidence)
    return {"ok": True, "pred_id": pred_id, "direction": direction,
            "confidence": confidence, "horizon": horizon}
