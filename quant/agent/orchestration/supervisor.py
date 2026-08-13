"""Supervisor — LLM orchestration over ledger tools (numbers stay deterministic).

Default path: plan → tools → narrate. Optional LangGraph wrapper in agent.orchestration.graph.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from agent.orchestration.service import build_system_status, list_enriched
from agent.trust.trust import list_trust, refresh_trust

QUANT = Path(__file__).resolve().parents[2]
TZ = ZoneInfo("Asia/Shanghai")

FORBIDDEN = re.compile(r"(必涨|稳赚|保证收益|一定赚钱|稳稳的幸福)")


def _tools() -> dict[str, Any]:
    return {
        "system_status": lambda **_: build_system_status(),
        "list_released": lambda **_: [
            {
                "pred_id": p["pred_id"],
                "object": p["object"],
                "claim": p["claim"],
                "confidence": p.get("confidence"),
                "horizon": p["horizon"],
                "release_gate": p.get("release_gate"),
            }
            for p in list_enriched()
            if p.get("release_gate") == "released"
        ][:30],
        "get_prediction": lambda pred_id, **_: next(
            (p for p in list_enriched() if p["pred_id"] == pred_id),
            {"error": "not_found"},
        ),
        "list_strategies": lambda **_: list_trust(),
        "list_l2": lambda **_: [
            {
                "pred_id": p["pred_id"],
                "claim": p["claim"],
                "status": p["status"],
                "outcome": p.get("outcome"),
            }
            for p in list_enriched()
            if p.get("level") == "L2"
        ][:20],
    }


def _deterministic_reply(
    message: str,
    pred_id: str | None,
    intent: str | None,
    bag: dict[str, Any],
) -> str:
    lines = ["【Supervisor · 确定性回退】", "以下数字均来自预测账本 / 信任账本。", ""]
    st = bag.get("system_status") or build_system_status()
    lines.append(
        f"系统：数据日 {st.get('data_day')} · 模式 {st.get('mode')} · "
        f"口径 {st.get('settlement_caliber')} · 已结算方向 n={st.get('resolved_direction_n')}"
    )
    lines.append(st.get("disclaimer") or "")
    if pred_id and bag.get("get_prediction"):
        p = bag["get_prediction"]
        if "error" not in p:
            sc = p.get("scorecard") or {}
            lines += [
                "",
                f"单票 {p.get('object')} ({p.get('pred_id')})",
                f"Claim {p.get('claim')} · 状态 {p.get('status')} · 放行 {p.get('release_gate')}",
                f"成绩单 n={sc.get('n')} {sc.get('label')} hit={sc.get('hit_rate')} "
                f"CI[{sc.get('wilson_low')},{sc.get('wilson_high')}]",
                f"失效条件：{'；'.join(p.get('failure_conditions') or [])}",
            ]
            if p.get("outcome"):
                lines.append(f"结算 outcome：{json.dumps(p['outcome'], ensure_ascii=False)}")
    if bag.get("list_strategies"):
        lines.append("")
        lines.append("策略信任：")
        for s in bag["list_strategies"]:
            lines.append(
                f"- {s.get('name')} [{s.get('state')}] weight={s.get('trust_weight')} "
                f"n={s.get('rolling_n')} hit={s.get('rolling_hit_rate')} "
                f"Wilson↓={s.get('wilson_low')} {s.get('pause_reason') or ''}"
            )
    if bag.get("list_l2"):
        lines.append("")
        lines.append("L2 组合：")
        for p in bag["list_l2"][:5]:
            lines.append(f"- {p.get('pred_id')} status={p.get('status')} outcome={p.get('outcome')}")
    lines.append("")
    lines.append(f"用户问题：{message}")
    lines.append("说明：LLM 不可用时使用账本直出；不构成收益承诺。")
    return "\n".join(lines)


def _llm_narrate(message: str, bag: dict[str, Any], intent: str | None) -> tuple[str, dict[str, Any]]:
    from agent.orchestration.llm import chat  # noqa: WPS433

    sys_prompt = (
        "你是 A 股量化分析 Agent 的 Supervisor。"
        "只分析不交易。数字必须且只能来自 TOOL_RESULTS JSON，禁止编造命中率/收益。"
        "禁止「必涨/稳赚/保证收益」等不可证伪措辞。"
        "用 Markdown 输出，且必须用二级标题分节（##），节标题固定为："
        "## 路由意图 / ## 关键事实 / ## 成绩与信任 / ## 失效条件 / ## 下一步。"
        "每节 2–5 条要点；关键数字用粗体；不要粘贴完整 JSON / 哈希 / 长字段列表。"
        "pred_id、命中率、n、Wilson 区间、release_gate、status 优先；其余可省略。"
        "若样本不足，明确写「样本不足，不得进主推荐」。"
        "L2 纸面指标须标注非实盘可实现收益。"
    )
    user = (
        f"intent={intent}\nquestion={message}\n\nTOOL_RESULTS:\n"
        f"{json.dumps(bag, ensure_ascii=False, default=str)[:12000]}"
    )
    text, meta = chat(
        [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user},
        ],
        temperature=0.2,
        max_tokens=2048,
        # Keep under webapp proxy budget; peak often 30–90s
        timeout=float(os.environ.get("AGENT_LLM_TIMEOUT", "600") or 600),
    )
    if FORBIDDEN.search(text or ""):
        text = (text or "") + "\n\n[Critic] 检测到不可证伪措辞，已提示忽略；请以账本数字为准。"
    return text, meta


def _select_tools(message: str, pred_id: str | None, intent: str | None) -> list[str]:
    names = ["system_status", "list_strategies"]
    if pred_id:
        names.append("get_prediction")
    low = (message or "").lower()
    if intent == "portfolio" or any(k in message for k in ("组合", "L2", "目标收益", "稳健")):
        names.append("list_l2")
    if intent == "single" or pred_id or any(k in message for k in ("票", "股票", "预测")):
        if "get_prediction" not in names and pred_id:
            names.append("get_prediction")
        names.append("list_released")
    if any(k in message for k in ("策略", "信任", "暂停", "champion", "降权", "因子", "challenger", "晋升", "研究")):
        if "list_strategies" not in names:
            names.append("list_strategies")
    # dedupe preserve order
    seen = set()
    out = []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def run_supervisor(
    message: str,
    *,
    pred_id: str | None = None,
    intent: str | None = None,
    use_llm: bool = True,
) -> dict[str, Any]:
    refresh_trust()
    tools = _tools()
    selected = _select_tools(message, pred_id, intent)
    bag: dict[str, Any] = {}
    tool_trace: list[dict[str, Any]] = []
    for name in selected:
        fn = tools[name]
        kwargs = {"pred_id": pred_id} if name == "get_prediction" else {}
        try:
            result = fn(**kwargs)
        except Exception as exc:  # noqa: BLE001
            result = {"error": str(exc)}
        bag[name] = result
        tool_trace.append({"tool": name, "ok": "error" not in (result if isinstance(result, dict) else {})})

    meta: dict[str, Any] = {"tools": selected, "llm": False}
    text: str
    if use_llm:
        try:
            text, llm_meta = _llm_narrate(message, bag, intent)
            meta["llm"] = True
            meta["llm_meta"] = {k: llm_meta.get(k) for k in ("label", "model", "backend", "tried") if k in llm_meta}
        except Exception as exc:  # noqa: BLE001
            text = _deterministic_reply(message, pred_id, intent, bag)
            meta["llm_error"] = str(exc)
    else:
        text = _deterministic_reply(message, pred_id, intent, bag)

    return {
        "reply": text,
        "meta": meta,
        "tool_trace": tool_trace,
        "ts": datetime.now(TZ).isoformat(),
    }
