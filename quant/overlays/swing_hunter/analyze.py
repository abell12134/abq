"""swing_hunter 分析层：单票简报 → Analyst/Bull/Bear/Judge → Prediction。

LLM 路由复用 sentiment_memory.llm_router（默认本地 LLM_PEAK_*；可用 --force-llm offpeak）。
任何 LLM 异常由 run_swing 捕获并降级为 watch（fail-open 到观察态，不产生假预测）。
"""

from __future__ import annotations

import json
import time
from typing import Any

from overlays.llm_json import parse_json_object
from overlays.sentiment_memory import llm_router  # noqa: WPS433

from . import prompts_cn as prompts
from .schema import Prediction, normalize_prediction


def build_brief(cand: dict[str, Any], name: str, market_notes: list[str] | None) -> str:
    """单票中文简报：量化特征 + 规则催化提示 + 近期公告/舆情 + 市场背景。"""
    feat = cand.get("feat") or {}
    lines = [
        f"标的: {cand['instrument']} {name}",
        f"LGBM 排名/分数: rank={cand.get('rank')} score={round(cand.get('score_lgbm') or 0.0, 4)}",
        "量化特征(截至信号日，后复权): " + json.dumps(feat, ensure_ascii=False),
        f"规则催化提示(关键词命中，仅供参考): {cand.get('catalyst_hints') or '无'}",
    ]
    events = cand.get("events") or []
    if events:
        lines.append(f"近期公告/舆情({len(events)} 条，最新在前):")
        for e in events[:8]:
            tag = e.get("kind") or e.get("source") or ""
            lines.append(f"  - [{tag}] {e.get('published')} | {e.get('title')}")
    else:
        lines.append("近期公告/舆情: 近 3 日库内无该票条目")
    if market_notes:
        lines.append("市场背景(近期政策/宏观):")
        for m in market_notes[:5]:
            lines.append(f"  - {m}")
    return "\n".join(lines)


def _chat(
    system: str,
    user: str,
    temperature: float,
    *,
    force_llm: str | None = None,
) -> tuple[str, dict[str, Any]]:
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


def parse_prediction(text: str, instrument: str) -> dict[str, Any] | None:
    obj = parse_json_object(text)
    return obj if isinstance(obj, dict) else None


def _extract_debate(trace: dict[str, Any]) -> tuple[str, str, str]:
    summary, bull, bear = "", "", ""
    for s in trace.get("steps") or []:
        role = str(s.get("role") or "")
        content = str(s.get("content") or "")
        if role == "analyst":
            summary = content
        elif role.startswith("bull"):
            bull = content
        elif role.startswith("bear"):
            bear = content
    return summary, bull, bear


def _prediction_from_judge(
    obj: dict[str, Any] | None,
    cand: dict[str, Any],
    name: str,
    debate_rounds: int,
    trace_llm_meta: dict[str, Any],
    gate_tier: str,
    gate_fallback: bool = False,
    prev_tier: str | None = None,
) -> Prediction:
    inst = cand["instrument"]
    base_meta = {
        "gate_tier": gate_tier,
        "gate_label": prompts.GATE_TIER_LABELS.get(gate_tier, gate_tier),
        "gate_fallback": gate_fallback,
    }
    if prev_tier:
        base_meta["gate_prev_tier"] = prev_tier

    if obj is None:
        pred = Prediction(
            instrument=inst, name=name, action="watch", confidence=0.0,
            swing_score=float(cand.get("rule_score") or 0.0),
            reasons=["LLM 输出无法解析，降级为观察"],
            factor_brief=cand.get("feat") or {},
            meta={**base_meta, "parse_error": True},
        )
        return normalize_prediction(pred)

    reasons = [str(r) for r in (obj.get("reasons") or [])]
    if gate_fallback and prev_tier:
        reasons.append(
            f"[门槛降档] {prev_tier}→{gate_tier}，"
            f"按{prompts.GATE_TIER_LABELS.get(gate_tier, gate_tier)}重判"
        )
        reasons = reasons[:3]

    pred = Prediction(
        instrument=inst,
        name=name,
        action=str(obj.get("action", "watch")).lower(),
        confidence=obj.get("confidence", 0.0),
        swing_score=float(cand.get("rule_score") or 0.0),
        target_tiers=obj.get("target_tiers") or [],
        stop_loss=float(obj.get("stop_loss", -0.05) or -0.05),
        horizon_days=int(obj.get("horizon_days", 10) or 10),
        catalysts=[str(c) for c in (obj.get("catalysts") or [])],
        risk_tags=[str(t) for t in (obj.get("risk_tags") or [])],
        reasons=reasons,
        factor_brief=cand.get("feat") or {},
        news_brief=cand.get("events") or [],
        meta={
            **base_meta,
            "debate_rounds": debate_rounds,
            "llm": trace_llm_meta,
        },
    )
    pred = normalize_prediction(pred)
    pred.swing_score = round(
        min(1.0, 0.6 * float(cand.get("rule_score") or 0.0) + 0.4 * pred.confidence), 4)
    return pred


def rerun_judge(
    cand: dict[str, Any],
    name: str,
    summary: str,
    bull: str,
    bear: str,
    *,
    gate_tier: str = "standard",
    force_llm: str | None = None,
    gate_fallback: bool = True,
    prev_tier: str = "strict",
) -> tuple[Prediction, dict[str, Any]]:
    """仅重跑 Judge（降档时用，省 analyst/bull/bear token）。"""
    inst = cand["instrument"]
    judge_raw, m_j = _chat(
        prompts.judge_system(gate_tier),
        prompts.judge_user(inst, summary, bull, bear),
        0.1,
        force_llm=force_llm,
    )
    obj = parse_prediction(judge_raw, inst)
    pred = _prediction_from_judge(
        obj, cand, name, 1, m_j, gate_tier,
        gate_fallback=gate_fallback, prev_tier=prev_tier if gate_fallback else None,
    )
    trace = {
        "instrument": inst,
        "name": name,
        "gate_tier": gate_tier,
        "gate_fallback": gate_fallback,
        "gate_prev_tier": prev_tier if gate_fallback else None,
        "steps": [{"role": "judge_raw", "content": judge_raw}],
        "llm_calls": [{"role": "judge", **m_j}],
    }
    trace["decision"] = pred.to_dict()
    return pred, trace


def analyze_candidate(
    cand: dict[str, Any],
    name: str = "",
    market_notes: list[str] | None = None,
    debate_rounds: int = 1,
    force_llm: str | None = None,
    gate_tier: str = "strict",
) -> tuple[Prediction, dict[str, Any]]:
    """对单只候选跑 催化摘要 → 多空辩论 → 裁判预测。返回 (Prediction, trace)。"""
    inst = cand["instrument"]
    brief = build_brief(cand, name, market_notes)
    trace: dict[str, Any] = {
        "instrument": inst,
        "name": name,
        "brief": brief,
        "force_llm": force_llm,
        "gate_tier": gate_tier,
        "steps": [],
        "llm_calls": [],
    }
    t_all = time.monotonic()

    summary, m1 = _chat(prompts.ANALYST_SYSTEM, prompts.analyst_user(brief), 0.2,
                        force_llm=force_llm)
    trace["steps"].append({"role": "analyst", "content": summary})
    trace["llm_calls"].append({"role": "analyst", **m1})

    bull, bear = "", ""
    for i in range(max(1, debate_rounds)):
        bull, m_b = _chat(
            prompts.BULL_SYSTEM,
            prompts.bull_user(summary if i == 0 else f"{summary}\n\n上轮空头：\n{bear}"),
            0.3,
            force_llm=force_llm,
        )
        trace["steps"].append({"role": f"bull_{i+1}", "content": bull})
        trace["llm_calls"].append({"role": f"bull_{i+1}", **m_b})
        bear, m_r = _chat(prompts.BEAR_SYSTEM, prompts.bear_user(summary, bull), 0.3,
                          force_llm=force_llm)
        trace["steps"].append({"role": f"bear_{i+1}", "content": bear})
        trace["llm_calls"].append({"role": f"bear_{i+1}", **m_r})

    judge_raw, m_j = _chat(
        prompts.judge_system(gate_tier),
        prompts.judge_user(inst, summary, bull, bear),
        0.1,
        force_llm=force_llm,
    )
    trace["steps"].append({"role": "judge_raw", "content": judge_raw})
    trace["llm_calls"].append({"role": "judge", **m_j})
    trace["total_latency_sec"] = round(time.monotonic() - t_all, 2)

    obj = parse_prediction(judge_raw, inst)
    pred = _prediction_from_judge(
        obj, cand, name, debate_rounds,
        trace["llm_calls"][-1] if trace["llm_calls"] else {},
        gate_tier,
    )
    trace["decision"] = pred.to_dict()
    return pred, trace


def apply_gate_fallback(
    candidates: list[dict[str, Any]],
    names: dict[str, str],
    rows: list[dict[str, Any]],
    traces: list[dict[str, Any]],
    *,
    force_llm: str | None = None,
    initial_tier: str = "strict",
    fallback_tier: str = "standard",
    max_rerun: int = 15,
    on_progress: Any = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """
    若 initial_tier 下无 predict，则仅重跑 Judge 降一档；标记 gate_tier / gate_fallback。
    rows: run_one 风格 dict 列表（含 prediction）；与 candidates/traces 同序。

    max_rerun: 全量候选时只对 rule_score/swing 最高的前 N 只重跑 Judge，
    避免 100+ 只全量降档卡死（页面进度也会长时间停在 94%）。
    on_progress: 可选回调 (i, total, instrument, action) -> None
    """
    gate_info: dict[str, Any] = {
        "initial_tier": initial_tier,
        "applied_tier": initial_tier,
        "fallback_tier": fallback_tier,
        "fallback_used": False,
        "max_rerun": max_rerun,
        "n_predict_initial": sum(
            1 for r in rows if (r.get("prediction") or {}).get("action") == "predict"
        ),
        "n_predict_final": 0,
        "label_initial": prompts.GATE_TIER_LABELS.get(initial_tier, initial_tier),
        "label_applied": prompts.GATE_TIER_LABELS.get(initial_tier, initial_tier),
    }

    if gate_info["n_predict_initial"] > 0:
        gate_info["n_predict_final"] = gate_info["n_predict_initial"]
        return rows, traces, gate_info

    gate_info["fallback_used"] = True
    gate_info["applied_tier"] = fallback_tier
    gate_info["label_applied"] = prompts.GATE_TIER_LABELS.get(fallback_tier, fallback_tier)

    new_rows = list(rows)
    new_traces = list(traces)

    # 按 swing_score / rule_score 优先，只重跑前 max_rerun
    ranked = sorted(
        range(len(candidates)),
        key=lambda i: (
            -float((rows[i].get("prediction") or {}).get("swing_score")
                   or candidates[i].get("rule_score") or 0),
            candidates[i].get("instrument") or "",
        ),
    )
    if max_rerun and max_rerun > 0:
        ranked = ranked[: int(max_rerun)]
    gate_info["n_rerun"] = len(ranked)

    for j, i in enumerate(ranked, 1):
        c, row, trace = candidates[i], rows[i], traces[i]
        inst = c["instrument"]
        name = names.get(inst, "") or ""
        summary, bull, bear = _extract_debate(trace)
        if not summary:
            if on_progress:
                on_progress(j, len(ranked), inst, "skip")
            continue
        pred, jtrace = rerun_judge(
            c, name, summary, bull, bear,
            gate_tier=fallback_tier,
            force_llm=force_llm,
            gate_fallback=True,
            prev_tier=initial_tier,
        )
        elapsed = round(
            sum(float(x.get("latency_sec") or 0) for x in jtrace.get("llm_calls") or []), 2
        )
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        for call in jtrace.get("llm_calls") or []:
            u = call.get("usage") or {}
            usage["prompt_tokens"] += int(u.get("prompt_tokens") or 0)
            usage["completion_tokens"] += int(u.get("completion_tokens") or 0)
        usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]

        new_row = dict(row)
        new_row["prediction"] = pred.to_dict()
        new_row["elapsed_sec"] = round(float(row.get("elapsed_sec") or 0) + elapsed, 2)
        new_row["usage"] = {
            "prompt_tokens": int((row.get("usage") or {}).get("prompt_tokens") or 0)
            + usage["prompt_tokens"],
            "completion_tokens": int((row.get("usage") or {}).get("completion_tokens") or 0)
            + usage["completion_tokens"],
            "total_tokens": int((row.get("usage") or {}).get("total_tokens") or 0)
            + usage["total_tokens"],
        }
        new_row["gate_fallback"] = True
        new_row["gate_tier"] = fallback_tier
        new_rows[i] = new_row

        merged_trace = dict(trace)
        merged_trace["gate_fallback_judge"] = jtrace
        merged_trace["gate_tier"] = fallback_tier
        merged_trace["gate_prev_tier"] = initial_tier
        new_traces[i] = merged_trace
        if on_progress:
            on_progress(j, len(ranked), inst, pred.action)

    gate_info["n_predict_final"] = sum(
        1 for r in new_rows if (r.get("prediction") or {}).get("action") == "predict"
    )
    return new_rows, new_traces, gate_info


def analyze_delta(
    instrument: str,
    name: str,
    day: str,
    new_items: list[dict[str, Any]],
    position_ctx: dict[str, Any],
    force_llm: str | None = None,
) -> dict[str, Any]:
    """单轮 LLM：仅消化新增条目，输出 hold/exit/watch delta。"""
    text, meta = _chat(
        prompts.DELTA_SYSTEM,
        prompts.delta_user(instrument, name, day, new_items, position_ctx),
        0.2,
        force_llm=force_llm,
    )
    obj = parse_prediction(text, instrument)
    stance = "hold"
    headline = (text or "")[:80]
    summary = (text or "")[:200]
    risk_change = "不变"
    invalidate = False
    if isinstance(obj, dict):
        stance = str(obj.get("stance") or obj.get("action") or "hold").lower()
        if stance not in {"hold", "exit", "watch"}:
            stance = "watch"
        headline = str(obj.get("headline") or headline)[:80]
        summary = str(obj.get("summary") or summary)[:200]
        risk_change = str(obj.get("risk_change") or "不变")[:8]
        invalidate = bool(obj.get("invalidate"))

    return {
        "date": day,
        "instrument": instrument.upper(),
        "name": name,
        "stance": stance,
        "headline": headline,
        "summary": summary,
        "risk_change": risk_change,
        "invalidate": invalidate,
        "new_item_ids": [it.get("id") for it in new_items if it.get("id")],
        "new_items_preview": [
            {"published": it.get("published"), "title": it.get("title"),
             "source": it.get("source")}
            for it in new_items[:8]
        ],
        "meta": meta,
    }


def dry_run_prediction(cand: dict[str, Any], name: str = "") -> Prediction:
    """不调 LLM 的规则降级预测（管线联调用）：一律 watch，不产生买入建议。"""
    pred = Prediction(
        instrument=cand["instrument"],
        name=name,
        action="watch",
        confidence=0.0,
        swing_score=float(cand.get("rule_score") or 0.0),
        reasons=["dry-run：未调用 LLM，仅规则候选记录"],
        catalysts=list(cand.get("catalyst_hints") or []),
        factor_brief=cand.get("feat") or {},
        news_brief=cand.get("events") or [],
        meta={"dry_run": True, "gate_tier": "dry_run"},
    )
    return normalize_prediction(pred)
