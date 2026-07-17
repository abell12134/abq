"""ta_veto 输出契约：日终 JSON + fail-open / 置信度 / 日否决上限。

文件：data/overlays/ta_veto/YYYY-MM-DD.json

make_trade_plan 只消费 ``vetoed`` 列表；缺文件、status=fail_open、或解析失败
时一律视为空否决（fail-open），不阻断出单。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

QUANT = Path(__file__).resolve().parents[2]
DEFAULT_VETO_DIR = QUANT / "data" / "overlays" / "ta_veto"

# 仅命中这些标签且 confidence ≥ 阈值时才允许生效为 VETO
RISK_TAGS = frozenset({
    "造假嫌疑",
    "重大诉讼",
    "停牌风险",
    "明显利空未定价",
    "财务恶化",
})

DEFAULT_CONFIDENCE_THRESHOLD = 0.7
# 每日最多否决数的上限公式：min(configured_cap, max(0, len(candidates)-1))
DEFAULT_MAX_VETOES = 1


@dataclass
class VetoDecision:
    instrument: str
    action: str  # "veto" | "pass"
    confidence: float
    risk_tags: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "VetoDecision":
        return cls(
            instrument=str(d["instrument"]),
            action=str(d.get("action", "pass")).lower(),
            confidence=float(d.get("confidence", 0.0)),
            risk_tags=[str(t) for t in (d.get("risk_tags") or [])],
            reasons=[str(r) for r in (d.get("reasons") or [])],
        )


@dataclass
class VetoFile:
    date: str
    status: str  # "ok" | "fail_open"
    candidates: list[str]
    decisions: list[VetoDecision]
    vetoed: list[str]
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD
    max_vetoes: int = DEFAULT_MAX_VETOES
    fail_reason: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "status": self.status,
            "fail_reason": self.fail_reason,
            "confidence_threshold": self.confidence_threshold,
            "max_vetoes": self.max_vetoes,
            "candidates": list(self.candidates),
            "decisions": [d.to_dict() for d in self.decisions],
            "vetoed": list(self.vetoed),
            "meta": dict(self.meta),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "VetoFile":
        return cls(
            date=str(d["date"]),
            status=str(d.get("status", "fail_open")),
            candidates=[str(x) for x in (d.get("candidates") or [])],
            decisions=[VetoDecision.from_dict(x) for x in (d.get("decisions") or [])],
            vetoed=[str(x) for x in (d.get("vetoed") or [])],
            confidence_threshold=float(
                d.get("confidence_threshold", DEFAULT_CONFIDENCE_THRESHOLD)
            ),
            max_vetoes=int(d.get("max_vetoes", DEFAULT_MAX_VETOES)),
            fail_reason=d.get("fail_reason"),
            meta=dict(d.get("meta") or {}),
        )


def veto_dir(base: Path | None = None) -> Path:
    return Path(base) if base else DEFAULT_VETO_DIR


def veto_path(day: str, base: Path | None = None) -> Path:
    return veto_dir(base) / f"{day}.json"


def apply_veto_policy(
    decisions: Iterable[VetoDecision],
    *,
    candidates: list[str],
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    max_vetoes: int = DEFAULT_MAX_VETOES,
) -> list[str]:
    """将原始决策压成生效否决名单。

    规则：
      1. action==veto 且 confidence≥阈值 且 risk_tags ∩ RISK_TAGS 非空；
      2. 按 confidence 降序；
      3. 日上限 = min(max_vetoes, max(0, len(candidates)-1))，保证至少留 1 个可买。
    """
    cap = min(int(max_vetoes), max(0, len(candidates) - 1))
    if cap <= 0:
        return []

    scored: list[tuple[float, str]] = []
    cand_set = set(candidates)
    for d in decisions:
        if d.instrument not in cand_set:
            continue
        if d.action != "veto":
            continue
        if float(d.confidence) < float(confidence_threshold):
            continue
        tags = {t.strip() for t in d.risk_tags if t and str(t).strip()}
        if not (tags & RISK_TAGS):
            continue
        scored.append((float(d.confidence), d.instrument))

    scored.sort(key=lambda x: (-x[0], x[1]))
    out: list[str] = []
    seen: set[str] = set()
    for _, inst in scored:
        if inst in seen:
            continue
        out.append(inst)
        seen.add(inst)
        if len(out) >= cap:
            break
    return out


def write_veto_file(payload: VetoFile, base: Path | None = None) -> Path:
    path = veto_path(payload.date, base)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload.to_dict(), ensure_ascii=False, indent=2) + "\n")
    path.with_suffix(".done").touch()
    return path


def read_veto_file(day: str, base: Path | None = None) -> VetoFile | None:
    path = veto_path(day, base)
    if not path.exists():
        return None
    try:
        return VetoFile.from_dict(json.loads(path.read_text()))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def load_vetoed_instruments(day: str, base: Path | None = None) -> set[str]:
    """供 make_trade_plan 调用：fail-open 返回空集。"""
    vf = read_veto_file(day, base)
    if vf is None:
        return set()
    if vf.status != "ok":
        return set()
    # 再跑一遍策略，防止手改 JSON 绕过上限
    effective = apply_veto_policy(
        vf.decisions,
        candidates=vf.candidates,
        confidence_threshold=vf.confidence_threshold,
        max_vetoes=vf.max_vetoes,
    )
    return set(effective)
