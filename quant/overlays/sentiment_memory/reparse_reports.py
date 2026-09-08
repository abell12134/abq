"""回填重解析历史失败报告。

对每份 meta.parse_error=True 的报告，用加固后的 parse_json_object 重新抠 meta.raw；
若成功，按 analyze_instrument 同口径重建报告字段并落盘 + 更新 catalog。
空响应（raw 为空）的无法救，跳过。

用法：
    python -m overlays.sentiment_memory.reparse_reports
    python -m overlays.sentiment_memory.reparse_reports --dry-run   # 只统计不落盘
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

QUANT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(QUANT))

from overlays.llm_json import parse_json_object  # noqa: E402
from overlays.sentiment_memory import store  # noqa: E402


def _build_report(parsed: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
    """从解析出的 dict + 原失败报告（保留 news/meta 等）重建标准报告。"""
    score = parsed.get("score", 0.0)
    try:
        score = float(score)
    except (TypeError, ValueError):
        score = 0.0
    score = max(-1.0, min(1.0, score))
    sentiment = str(parsed.get("sentiment") or "neutral").lower()
    if sentiment not in {"positive", "neutral", "negative", "mixed"}:
        sentiment = "neutral"
    name = base.get("name") or str(parsed.get("name") or "")
    return {
        "instrument": base.get("instrument"),
        "name": name,
        "date": base.get("date"),
        "sentiment": sentiment,
        "score": score,
        "headline": str(parsed.get("headline") or "")[:80],
        "summary": str(parsed.get("summary") or "")[:600],
        "fundamentals": str(parsed.get("fundamentals") or "")[:200],
        "policy_impact": str(parsed.get("policy_impact") or "")[:200],
        "risk_tags": [str(t) for t in (parsed.get("risk_tags") or [])][:8],
        "key_events": [
            {
                "date": str(e.get("date", ""))[:10],
                "event": str(e.get("event", ""))[:80],
                "impact": str(e.get("impact", "中性"))[:8],
            }
            for e in (parsed.get("key_events") or [])[:10]
            if isinstance(e, dict)
        ],
        "watchpoints": [str(w) for w in (parsed.get("watchpoints") or [])][:5],
        "stance": str(parsed.get("stance") or "可继续跟踪")[:40],
        # 保留原报告的采集侧信息
        "news_count": base.get("news_count"),
        "announcement_count": base.get("announcement_count"),
        "policy_count": base.get("policy_count"),
        "memories_used": base.get("memories_used"),
        "meta": {**(base.get("meta") or {}), "reparsed": True, "parse_error": False},
        "news_preview": base.get("news_preview") or [],
        "memories": base.get("memories") or [],
    }


def reparse_all(*, dry_run: bool = False) -> dict[str, int]:
    root = store.ROOT / "reports"
    files = sorted(root.glob("*/????-??-??.json"))
    stats = {"total": 0, "failed": 0, "empty": 0, "recovered": 0, "still_fail": 0}
    for f in files:
        stats["total"] += 1
        try:
            d = json_loads(f)
        except Exception:
            continue
        meta = d.get("meta") or {}
        if not meta.get("parse_error"):
            continue
        stats["failed"] += 1
        raw = meta.get("raw", "") or ""
        if not raw:
            stats["empty"] += 1
            continue
        parsed = parse_json_object(raw)
        if not parsed or not (parsed.get("sentiment") or parsed.get("headline")
                              or parsed.get("summary")):
            stats["still_fail"] += 1
            continue
        stats["recovered"] += 1
        if dry_run:
            continue
        new = _build_report(parsed, d)
        store.save_report(str(d["instrument"]), str(d["date"]), new)
    return stats


def json_loads(path: Path) -> dict[str, Any]:
    import json
    return json.loads(path.read_text())


def main() -> int:
    p = argparse.ArgumentParser(description="回填重解析历史失败报告")
    p.add_argument("--dry-run", action="store_true", help="只统计，不落盘")
    args = p.parse_args()
    s = reparse_all(dry_run=args.dry_run)
    print(f"[reparse] 总报告 {s['total']} | 失败 {s['failed']} "
          f"(空响应 {s['empty']} / 有内容 {s['failed'] - s['empty']})")
    print(f"[reparse] 救回 {s['recovered']} | 仍失败 {s['still_fail']} "
          f"| dry_run={args.dry_run}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
