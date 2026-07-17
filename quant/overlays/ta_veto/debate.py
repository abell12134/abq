"""多轮辩论图：Analyst → (Bull ↔ Bear)×N → Judge(VETO/PASS)。"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from . import prompts_cn as prompts
from .schema import VetoDecision


ChatFn = Callable[[str, str, float], str]  # system, user, temperature -> content


def _chat(client, model: str, system: str, user: str, temperature: float = 0.2) -> str:
    resp = client.chat.completions.create(
        model=model,
        temperature=temperature,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return (resp.choices[0].message.content or "") if resp.choices else ""


def parse_decision(text: str, instrument: str) -> VetoDecision:
    text = (text or "").strip()
    obj: dict[str, Any] | None = None
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            obj = None
    if not isinstance(obj, dict):
        return VetoDecision(
            instrument=instrument,
            action="pass",
            confidence=0.0,
            risk_tags=[],
            reasons=["LLM 输出无法解析，默认 pass"],
        )
    action = str(obj.get("action", "pass")).lower()
    if action not in {"veto", "pass"}:
        action = "pass"
    try:
        conf = float(obj.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    tags = [str(t) for t in (obj.get("risk_tags") or [])]
    reasons = [str(r) for r in (obj.get("reasons") or [])][:3]
    return VetoDecision(
        instrument=instrument,
        action=action,
        confidence=max(0.0, min(1.0, conf)),
        risk_tags=tags,
        reasons=reasons or ["无理由"],
    )


def run_debate(
    client,
    model: str,
    instrument: str,
    brief: str,
    *,
    debate_rounds: int = 1,
) -> tuple[VetoDecision, dict[str, Any]]:
    """返回 (最终决策, 过程轨迹)。"""
    rounds = max(1, int(debate_rounds))
    trace: dict[str, Any] = {"instrument": instrument, "rounds": rounds, "steps": []}

    summary = _chat(
        client, model, prompts.ANALYST_SYSTEM, prompts.analyst_user(brief), 0.2
    )
    trace["steps"].append({"role": "analyst", "content": summary})

    bull = ""
    bear = ""
    for i in range(rounds):
        bull = _chat(
            client,
            model,
            prompts.BULL_SYSTEM,
            prompts.bull_user(summary if i == 0 else f"{summary}\n\n上轮空头：\n{bear}"),
            0.3,
        )
        trace["steps"].append({"role": f"bull_{i+1}", "content": bull})
        bear = _chat(
            client,
            model,
            prompts.BEAR_SYSTEM,
            prompts.bear_user(summary, bull),
            0.3,
        )
        trace["steps"].append({"role": f"bear_{i+1}", "content": bear})

    judge_raw = _chat(
        client,
        model,
        prompts.JUDGE_SYSTEM,
        prompts.judge_user(instrument, summary, bull, bear, rounds),
        0.1,
    )
    trace["steps"].append({"role": "judge_raw", "content": judge_raw})
    decision = parse_decision(judge_raw, instrument)
    decision.instrument = instrument
    trace["decision"] = decision.to_dict()
    return decision, trace
