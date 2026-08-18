"""研究分析管线：4 分析师（共享 CN）→ CN 辩论 → EN 辩论 → 合并裁决。

LLM 路由复用 sentiment_memory.llm_router（默认本地 LLM_PEAK_*；--force-llm offpeak 走 DeepSeek）。
任何 LLM 异常 fail-open：该语言裁决标 parse_error，不阻塞另一语言；全失败 → hold。
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from overlays.llm_json import parse_json_object
from overlays.sentiment_memory import llm_router  # noqa: WPS433

from . import prompts_cn as PC
from . import prompts_en as PE
from .data_bundle import gather
from .schema import (
    AnalystReport,
    ResearchReport,
    Verdict,
    action_to_direction,
    normalize_verdict,
)

log = logging.getLogger(__name__)
TZ = ZoneInfo("Asia/Shanghai")

ANALYST_KINDS = ("market", "news", "fundamentals", "social")


def _chat(system: str, user: str, temperature: float, *,
          force_llm: str | None = None) -> tuple[str, dict[str, Any]]:
    t0 = time.monotonic()
    text, meta = llm_router.chat(
        [{"role": "system", "content": system},
         {"role": "user", "content": user}],
        temperature=temperature,
        force=force_llm,
    )
    meta = dict(meta or {})
    meta["latency_sec"] = round(time.monotonic() - t0, 2)
    return text, meta


def run_analysts(bundle: dict[str, Any], *, force_llm: str | None = None,
                 dry_run: bool = False) -> list[AnalystReport]:
    """4 个分析师各跑一次（CN，共享数据）。"""
    reports: list[AnalystReport] = []
    for kind in ANALYST_KINDS:
        if dry_run:
            reports.append(AnalystReport(kind=kind, content="dry-run：未调用 LLM"))
            continue
        try:
            text, _ = _chat(PC.ANALYST_KIND_SYSTEM[kind],
                            PC.analyst_user(kind, bundle), 0.2, force_llm=force_llm)
            reports.append(AnalystReport(kind=kind, content=text.strip()))
        except Exception as e:  # noqa: BLE001
            log.info("分析师 %s 失败: %s", kind, e)
            reports.append(AnalystReport(
                kind=kind, content=f"[分析师异常] {e}", ))
    return reports


def _run_one_debate(
    analysts: list[AnalystReport],
    instrument: str,
    lang: str,
    *,
    force_llm: str | None = None,
    debate_rounds: int = 1,
) -> tuple[Verdict, list[dict[str, Any]]]:
    """单语言多空辩论 + 评判。返回 (Verdict, trace)。"""
    digest = PC.analyst_digest([a.to_dict() for a in analysts])
    if lang == "en":
        bull_sys, bear_sys, judge_sys = PE.BULL_SYSTEM_EN, PE.BEAR_SYSTEM_EN, PE.JUDGE_SYSTEM_EN
        bull_fn, bear_fn, judge_fn = PE.bull_user_en, PE.bear_user_en, PE.judge_user_en
    else:
        bull_sys, bear_sys, judge_sys = PC.BULL_SYSTEM_CN, PC.BEAR_SYSTEM_CN, PC.JUDGE_SYSTEM_CN
        bull_fn, bear_fn, judge_fn = PC.bull_user_cn, PC.bear_user_cn, PC.judge_user_cn

    trace: list[dict[str, Any]] = []
    bull, bear = "", ""
    for i in range(max(1, debate_rounds)):
        try:
            bull, m_b = _chat(
                bull_sys,
                bull_fn(digest if i == 0 else f"{digest}\n\n上轮空头：\n{bear}"),
                0.3, force_llm=force_llm)
            trace.append({"role": f"bull_{i+1}", **m_b})
        except Exception as e:  # noqa: BLE001
            bull, trace = f"[多头异常] {e}", trace
        try:
            bear, m_r = _chat(bear_sys, bear_fn(digest, bull), 0.3, force_llm=force_llm)
            trace.append({"role": f"bear_{i+1}", **m_r})
        except Exception as e:  # noqa: BLE001
            bear, trace = f"[空头异常] {e}", trace

    try:
        judge_raw, m_j = _chat(judge_sys, judge_fn(instrument, digest, bull, bear),
                               0.1, force_llm=force_llm)
        trace.append({"role": "judge", **m_j})
        obj = parse_json_object(judge_raw)
        verdict = normalize_verdict(obj, lang=lang)
        if verdict.parse_error:
            verdict.raw = (judge_raw or "")[:8000]
    except Exception as e:  # noqa: BLE001
        log.info("%s judge 失败: %s", lang, e)
        verdict = Verdict(lang=lang, parse_error=True, summary=str(e)[:160])

    return verdict, trace


def merge_verdicts(cn: Verdict, en: Verdict) -> tuple[str, float, str]:
    """合并中英裁决 → (merged_direction, merged_confidence, consensus)。"""
    d_cn = action_to_direction(cn.action) if not cn.parse_error else "hold"
    d_en = action_to_direction(en.action) if not en.parse_error else "hold"

    if cn.parse_error and en.parse_error:
        return "hold", 0.0, "disagree"
    if cn.parse_error:
        return d_en, round(en.confidence * 0.6, 3), "partial"
    if en.parse_error:
        return d_cn, round(cn.confidence * 0.6, 3), "partial"
    if d_cn == d_en:
        return d_cn, round((cn.confidence + en.confidence) / 2, 3), "agree"
    # 不一致
    if d_cn == "hold":
        return d_en, round(en.confidence * 0.6, 3), "partial"
    if d_en == "hold":
        return d_cn, round(cn.confidence * 0.6, 3), "partial"
    # buy vs sell 正面冲突 → 观望
    return "hold", round(max(cn.confidence, en.confidence) * 0.4, 3), "disagree"


def analyze_instrument(
    instrument: str,
    name: str,
    day: str,
    *,
    force_llm: str | None = None,
    debate_rounds: int = 1,
    dry_run: bool = False,
    sources: list[str] | None = None,
    global_cache: dict[str, Any] | None = None,
    lookback_days: int = 90,
) -> ResearchReport:
    """对单只标的跑完整研究管线。"""
    instrument = instrument.upper()
    t_all = time.monotonic()

    bundle = gather(instrument, name, day, lookback_days=lookback_days,
                    global_cache=global_cache)

    if dry_run:
        report = ResearchReport(
            instrument=instrument, name=name, date=day,
            sources=list(sources or []),
            analysts=[AnalystReport(kind=k, content="dry-run") for k in ANALYST_KINDS],
            verdict_cn=Verdict(lang="cn", summary="dry-run：未调用 LLM"),
            verdict_en=Verdict(lang="en", summary="dry-run: no LLM"),
            merged_direction="hold", merged_confidence=0.0, consensus="agree",
            status="dry_run",
            created_at=datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"),
            meta={"bundle_stats": _bundle_stats(bundle), "dry_run": True},
        )
        return report

    analysts = run_analysts(bundle, force_llm=force_llm)
    verdict_cn, tr_cn = _run_one_debate(analysts, instrument, "cn",
                                        force_llm=force_llm, debate_rounds=debate_rounds)
    verdict_en, tr_en = _run_one_debate(analysts, instrument, "en",
                                        force_llm=force_llm, debate_rounds=debate_rounds)
    merged_dir, merged_conf, consensus = merge_verdicts(verdict_cn, verdict_en)

    return ResearchReport(
        instrument=instrument, name=name, date=day,
        sources=list(sources or []),
        analysts=analysts,
        verdict_cn=verdict_cn,
        verdict_en=verdict_en,
        merged_direction=merged_dir,
        merged_confidence=merged_conf,
        consensus=consensus,
        status="ok",
        created_at=datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"),
        meta={
            "bundle_stats": _bundle_stats(bundle),
            "force_llm": force_llm,
            "debate_rounds": debate_rounds,
            "latency_sec": round(time.monotonic() - t_all, 2),
            "trace_cn": [{"role": t.get("role"), "latency_sec": t.get("latency_sec")}
                         for t in tr_cn if isinstance(t, dict)],
            "trace_en": [{"role": t.get("role"), "latency_sec": t.get("latency_sec")}
                         for t in tr_en if isinstance(t, dict)],
        },
    )


def _bundle_stats(bundle: dict[str, Any]) -> dict[str, Any]:
    m = bundle.get("market") or {}
    return {
        "market_ok": bool(m.get("ok")),
        "market_bars": m.get("bars"),
        "news_count": len(bundle.get("news") or []),
        "has_fundamentals": bool((bundle.get("fundamentals") or {}).get("info")),
        "has_sentiment": bool((bundle.get("social") or {}).get("sentiment")),
    }
