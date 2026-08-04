"""LLM 摘要分析 + 写入向量记忆。"""

from __future__ import annotations

import json
import re
from typing import Any

from . import llm_router, prompts_cn, store


def _parse_report(text: str) -> dict[str, Any] | None:
    text = (text or "").strip()
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    blob = fence.group(1) if fence else None
    if blob is None:
        m = re.search(r"\{.*\}", text, re.S)
        blob = m.group(0) if m else None
    if not blob:
        return None
    try:
        obj = json.loads(blob)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def analyze_instrument(
    day: str,
    instrument: str,
    name: str,
    news: list[dict[str, Any]],
    *,
    dry_run: bool = False,
    force_llm: str | None = None,
) -> dict[str, Any]:
    """对单票做摘要分析并落盘报告/向量。"""
    instrument = instrument.upper()
    # 先入库原始 + 向量（即使 LLM 失败也保留长期记忆原料）
    store.append_raw(news)
    store.upsert_vectors(instrument, [
        {**n, "instrument": instrument} for n in news
    ])

    query = f"{instrument} {name} 风险 业绩 立案 诉讼 股东"
    memories = store.search_memory(instrument, query, top_k=8)

    if dry_run:
        report = {
            "instrument": instrument,
            "name": name,
            "date": day,
            "sentiment": "neutral",
            "score": 0.0,
            "headline": "dry-run：未调用 LLM",
            "summary": f"采集到 {len(news)} 条舆情，向量库 "
                       f"{store.vector_stats(instrument).get('count')} 条。",
            "risk_tags": [],
            "key_events": [],
            "watchpoints": [],
            "stance": "可继续跟踪",
            "news_count": len(news),
            "memories_used": len(memories),
            "meta": {"dry_run": True},
            "news_preview": [
                {"source": n.get("source"), "published": n.get("published"),
                 "title": n.get("title"), "url": n.get("url")}
                for n in news[:15]
            ],
        }
        path = store.save_report(instrument, day, report)
        report["path"] = str(path)
        return report

    prompt = prompts_cn.build_user_prompt(day, instrument, name, news, memories)
    text, meta = llm_router.chat(
        [{"role": "system", "content": prompts_cn.SYSTEM},
         {"role": "user", "content": prompt}],
        force=force_llm,
    )
    parsed = _parse_report(text)
    if not parsed:
        report = {
            "instrument": instrument,
            "name": name,
            "date": day,
            "sentiment": "neutral",
            "score": 0.0,
            "headline": "LLM 输出无法解析",
            "summary": (text or "")[:400],
            "risk_tags": [],
            "key_events": [],
            "watchpoints": [],
            "stance": "可继续跟踪",
            "news_count": len(news),
            "memories_used": len(memories),
            "meta": {**meta, "parse_error": True, "raw_snippet": (text or "")[:500]},
            "news_preview": [
                {"source": n.get("source"), "published": n.get("published"),
                 "title": n.get("title"), "url": n.get("url")}
                for n in news[:15]
            ],
        }
    else:
        score = parsed.get("score", 0.0)
        try:
            score = float(score)
        except (TypeError, ValueError):
            score = 0.0
        score = max(-1.0, min(1.0, score))
        sentiment = str(parsed.get("sentiment") or "neutral").lower()
        if sentiment not in {"positive", "neutral", "negative", "mixed"}:
            sentiment = "neutral"
        report = {
            "instrument": instrument,
            "name": name or str(parsed.get("name") or ""),
            "date": day,
            "sentiment": sentiment,
            "score": score,
            "headline": str(parsed.get("headline") or "")[:80],
            "summary": str(parsed.get("summary") or "")[:600],
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
            "news_count": len(news),
            "memories_used": len(memories),
            "meta": meta,
            "news_preview": [
                {"source": n.get("source"), "published": n.get("published"),
                 "title": n.get("title"), "url": n.get("url")}
                for n in news[:20]
            ],
            "memories": memories[:6],
        }

    # 把摘要本身也写入向量，形成可检索的长期记忆节点
    store.upsert_vectors(instrument, [{
        "id": f"report-{day}-{instrument}",
        "source": "llm_report",
        "instrument": instrument,
        "title": report.get("headline") or f"{instrument} 舆情报告 {day}",
        "content": f"{report.get('summary','')} {' '.join(report.get('risk_tags') or [])}",
        "published": f"{day} 00:00:00",
        "url": "",
    }])
    path = store.save_report(instrument, day, report)
    report["path"] = str(path)
    report["vector_count"] = store.vector_stats(instrument).get("count")
    return report
