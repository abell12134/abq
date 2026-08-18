"""研究分析输出契约：分析师报告 + 中英双裁决 + 合并方向。

文件：data/overlays/research/reports/{instrument}/{date}.json
目录：data/overlays/research/catalog.json

裁决 JSON schema（中/英 Judge 各产一份）：
  {"action":"buy|sell|hold","confidence":0~1,"target_price":float|null,
   "horizon_days":5~15,"stop_pct":-0.05~0,"reasons":[1~3],"risk_tags":[...],
   "summary":"≤160字"}

合并方向（进账本）：buy→up，sell→down，hold→不发预测。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

QUANT = Path(__file__).resolve().parents[2]
ROOT = QUANT / "data" / "overlays" / "research"

HORIZON_MIN, HORIZON_MAX = 5, 15
HORIZON_DEFAULT = 10

# 动作集合
ACTIONS = frozenset({"buy", "sell", "hold"})
# 方向（账本口径）
DIRECTIONS = frozenset({"up", "down", "hold"})

# 风险标签词典（与 prompts 对齐；LLM 输出从中选取，未知标签过滤）
RISK_TAGS = frozenset({
    "高位放量滞涨", "题材退潮风险", "业绩不及预期风险", "解禁减持临近",
    "立案处罚风险", "停牌风险", "流动性风险", "大盘系统性风险",
    "估值偏高", "财务恶化风险", "行业政策风险", "技术破位风险",
})

# 催化类型（信息性，不强制约束）
CATALYST_TYPES = frozenset({
    "业绩超预期", "政策行业利好", "重大合同订单", "股东增持回购",
    "题材板块启动", "技术突破放量", "估值修复", "其他催化",
})


@dataclass
class Verdict:
    """单语言裁决（CN 或 EN）。"""
    action: str = "hold"           # buy | sell | hold
    confidence: float = 0.0
    target_price: float | None = None
    horizon_days: int = HORIZON_DEFAULT
    stop_pct: float = -0.05
    reasons: list[str] = field(default_factory=list)
    risk_tags: list[str] = field(default_factory=list)
    summary: str = ""
    lang: str = "cn"               # cn | en
    parse_error: bool = False
    raw: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "Verdict":
        if not d:
            return cls()
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class AnalystReport:
    """单个分析师产出（共享，CN）。"""
    kind: str = "market"           # market | news | fundamentals | social
    content: str = ""
    lang: str = "cn"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "AnalystReport":
        if not d:
            return cls()
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class ResearchReport:
    instrument: str = ""
    name: str = ""
    date: str = ""
    sources: list[str] = field(default_factory=list)
    analysts: list[AnalystReport] = field(default_factory=list)
    verdict_cn: Verdict = field(default_factory=Verdict)
    verdict_en: Verdict = field(default_factory=lambda: Verdict(lang="en"))
    merged_direction: str = "hold"   # up | down | hold
    merged_confidence: float = 0.0
    consensus: str = "agree"         # agree | disagree | partial
    pred_id: str | None = None
    status: str = "ok"               # ok | fail_open | dry_run
    created_at: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument": self.instrument,
            "name": self.name,
            "date": self.date,
            "sources": list(self.sources),
            "analysts": [a.to_dict() for a in self.analysts],
            "verdict_cn": self.verdict_cn.to_dict(),
            "verdict_en": self.verdict_en.to_dict(),
            "merged_direction": self.merged_direction,
            "merged_confidence": self.merged_confidence,
            "consensus": self.consensus,
            "pred_id": self.pred_id,
            "status": self.status,
            "created_at": self.created_at,
            "meta": dict(self.meta),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "ResearchReport":
        if not d:
            return cls()
        return cls(
            instrument=str(d.get("instrument", "")),
            name=str(d.get("name", "")),
            date=str(d.get("date", "")),
            sources=[str(x) for x in (d.get("sources") or [])],
            analysts=[AnalystReport.from_dict(x) for x in (d.get("analysts") or [])],
            verdict_cn=Verdict.from_dict(d.get("verdict_cn")),
            verdict_en=Verdict.from_dict({**(d.get("verdict_en") or {}), "lang": "en"}),
            merged_direction=str(d.get("merged_direction", "hold")),
            merged_confidence=float(d.get("merged_confidence") or 0.0),
            consensus=str(d.get("consensus", "agree")),
            pred_id=d.get("pred_id"),
            status=str(d.get("status", "ok")),
            created_at=str(d.get("created_at", "")),
            meta=dict(d.get("meta") or {}),
        )


# ---------------- 清洗 ----------------


def normalize_verdict(obj: dict[str, Any] | None, lang: str = "cn") -> Verdict:
    """入库前清洗 LLM 裁决 JSON。"""
    if not isinstance(obj, dict):
        return Verdict(lang=lang, parse_error=True)
    action = str(obj.get("action", "hold")).lower().strip()
    # 容错：常见同义
    action = {
        "buy": "buy", "买入": "buy", "long": "buy", "up": "buy", "增持": "buy",
        "加仓": "buy", "加码": "buy", "做多": "buy", "建仓": "buy",
        "sell": "sell", "卖出": "sell", "short": "sell", "down": "sell", "减持": "sell",
        "减仓": "sell", "清仓": "sell", "做空": "sell", "止盈": "sell", "止损": "sell",
        "hold": "hold", "持有": "hold", "观望": "hold", "neutral": "hold", "watch": "hold",
    }.get(action, "hold" if action not in ACTIONS else action)
    if action not in ACTIONS:
        action = "hold"

    def _f(v, lo, hi, default=0.0):
        try:
            x = float(v)
        except (TypeError, ValueError):
            return default
        return max(lo, min(hi, x))

    horizon = int(_f(obj.get("horizon_days"), HORIZON_MIN, HORIZON_MAX, HORIZON_DEFAULT))
    if horizon < HORIZON_MIN:
        horizon = HORIZON_MIN
    elif horizon > HORIZON_MAX:
        horizon = HORIZON_MAX

    tp = obj.get("target_price")
    try:
        tp = float(tp) if tp not in (None, "", 0) else None
        if tp is not None and tp <= 0:
            tp = None
    except (TypeError, ValueError):
        tp = None

    return Verdict(
        action=action,
        confidence=_f(obj.get("confidence"), 0.0, 1.0),
        target_price=tp,
        horizon_days=horizon,
        stop_pct=_f(obj.get("stop_pct"), -0.20, 0.0, -0.05),
        reasons=[str(r)[:160] for r in (obj.get("reasons") or [])][:3],
        risk_tags=[t for t in (obj.get("risk_tags") or []) if t in RISK_TAGS][:4],
        summary=str(obj.get("summary") or "")[:400],
        lang=lang,
    )


def action_to_direction(action: str) -> str:
    return {"buy": "up", "sell": "down"}.get(action, "hold")
