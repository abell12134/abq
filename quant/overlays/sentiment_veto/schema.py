"""sentiment_veto 输出契约。

文件：
  data/overlays/sentiment_veto/YYYY-MM-DD.json
  data/accounts/<live>/orders_exec/YYYY-MM-DD.csv
  data/accounts/<live>/reports/sentiment_checklist_YYYY-MM-DD.md

缺文件 / status≠ok / 解析失败 → fail-open（不否决）。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

QUANT = Path(__file__).resolve().parents[2]
DEFAULT_VETO_DIR = QUANT / "data" / "overlays" / "sentiment_veto"

# 仅命中这些「硬伤」标签且 confidence≥阈值时才生效为 VETO
RISK_TAGS = frozenset({
    "ST退市风险",
    "立案调查",
    "重大诉讼未结",
    "业绩暴雷",
    "财务造假嫌疑",
    "控股股东重大违规",
    "债务担保危机",
    "突发重大利空未消化",
})

DEFAULT_CONFIDENCE_THRESHOLD = 0.7
DEFAULT_MAX_VETOES = 3


@dataclass
class VetoDecision:
    instrument: str
    name: str = ""
    action: str = "pass"  # "veto" | "pass"
    confidence: float = 0.0
    risk_tags: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "VetoDecision":
        return cls(
            instrument=str(d["instrument"]),
            name=str(d.get("name") or ""),
            action=str(d.get("action", "pass")).lower(),
            confidence=float(d.get("confidence", 0.0)),
            risk_tags=[str(t) for t in (d.get("risk_tags") or [])],
            reasons=[str(r) for r in (d.get("reasons") or [])],
            sources=[str(s) for s in (d.get("sources") or [])],
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
    """硬伤否决：标签命中 + 置信度 + 日上限（至少保留 1 个可买）。"""
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
    vf = read_veto_file(day, base)
    if vf is None or vf.status != "ok":
        return set()
    return set(apply_veto_policy(
        vf.decisions,
        candidates=vf.candidates,
        confidence_threshold=vf.confidence_threshold,
        max_vetoes=vf.max_vetoes,
    ))
