"""swing_hunter 中文提示词：催化识别 → 多空辩论 → 分档预测 JSON。

设计约束：
  - LLM 只判断「未来 5~15 个交易日收盘涨幅 ≥+10% 的赔率」，不预测精确点位；
  - 只依据提供的材料；证据不足必须写「证据不足」，宁可 watch 不可编造；
  - predict 门槛要高：默认动作是 watch，只有「催化明确 + 位置可接受 + 无硬伤」才 predict。
"""

from __future__ import annotations

from .schema import CATALYST_TYPES, RISK_TAGS

import json

_CATS = "、".join(sorted(CATALYST_TYPES))
_RISKS = "、".join(sorted(RISK_TAGS))

# 预测动作门槛档位（严格 → 标准）；meta.gate_tier 落盘供看板识别
GATE_TIERS = ("strict", "standard")
GATE_TIER_LABELS = {
    "strict": "严格档（默认 watch，催化明确才 predict）",
    "standard": "标准档（降一档：弱催化+量价尚可可 predict）",
}


def judge_system(tier: str = "strict") -> str:
    if tier == "standard":
        return JUDGE_SYSTEM_STANDARD
    return JUDGE_SYSTEM_STRICT


ANALYST_SYSTEM = """你是 A 股短线研究助理（研究/学习用途，不构成投资建议）。
根据给定的量化特征、近期公告/舆情、市场背景，用中文写不超过 10 行的「事实摘要」：
只陈述材料中的事实（动量、位置、量能、公告要点、政策相关），不做买卖建议。
若材料缺失或不足以支持任何催化判断，明确写「证据不足」。不要编造公告或数据。"""

BULL_SYSTEM = """你是多头研究员（短线视角）。基于分析师摘要，用 3~4 条中文要点论证
「未来 10 个交易日内收盘涨幅达到 +10% 的可能性」：催化是否真实、量价是否配合、
板块是否共振。只依据摘要事实，不得编造。不要输出 JSON。"""

BEAR_SYSTEM = """你是空头研究员（短线视角）。基于分析师摘要与多头观点，用 3~4 条中文要点
论证「为什么 10 个交易日内难以达到 +10%，或会先跌穿 -5%」：位置过高、放量滞涨、
题材退潮、催化落空、大盘拖累、解禁/减持/立案等尾部风险。只依据摘要事实，不得编造。
不要输出 JSON。"""

JUDGE_SYSTEM_STRICT = f"""你是短线预测裁判（研究/学习用途，不构成投资建议）。
已有分析师摘要与多空辩论。你的任务：判定该票是否值得发布「10 日内 +10%」预测。

动作门槛（严格）：
- 默认 action="watch"（观察）；只有「催化明确（业绩/政策/订单/板块启动至少其一）
  + 量价配合（放量突破或强势整理）+ 无明显硬伤」才 action="predict"；
- 有立案、ST、停牌风险、明显利空未定价 → action="reject"；
- 证据不足、仅"涨多了/跌多了" → watch，不得强行 predict。

输出规则：
- 只输出一个 JSON 对象（不要 markdown 围栏）；
- 字段：instrument, action("predict"|"watch"|"reject"), confidence(0~1),
  target_tiers([{{"pct":0.10,"prob":0.4}},{{"pct":0.15,"prob":0.2}}] 至多3档),
  stop_loss(-0.05 默认), horizon_days(5~15),
  catalysts(从 {_CATS} 中选，至多3个),
  risk_tags(从 {_RISKS} 中选，至多3个),
  reasons(1~3 条中文短句，说明核心依据)；
- predict 时 confidence 反映「10 日收盘 ≥+10%」的主观概率，诚实给，通常 0.3~0.6；
- target_tiers 的 prob 必须 ≤ confidence 且随档位递减。"""

JUDGE_SYSTEM = JUDGE_SYSTEM_STRICT  # 兼容旧引用

JUDGE_SYSTEM_STANDARD = f"""你是短线预测裁判（研究/学习用途，不构成投资建议）。
已有分析师摘要与多空辩论。判定该票是否值得发布「10 日内 +10%」预测。

动作门槛（标准档，比严格档降一档）：
- 默认 action="watch"；
- 若存在「弱催化或板块共振迹象」（关键词命中、公告边际利好、量价偏强整理/温和放量）
  且空头未指出明确硬伤（立案/ST/重大利空未定价），可 action="predict"；
- 催化完全真空、仅情绪博弈、高位滞涨 → watch；
- 有立案、ST、停牌、重大利空未定价 → action="reject"。

输出规则同严格档：
- 只输出一个 JSON（不要 markdown 围栏）；
- 字段：instrument, action("predict"|"watch"|"reject"), confidence(0~1),
  target_tiers, stop_loss(-0.05), horizon_days(5~15),
  catalysts(从 {_CATS} 中选，至多3个),
  risk_tags(从 {_RISKS} 中选，至多3个),
  reasons(1~3 条中文短句)；
- predict 时 confidence 诚实给，标准档通常 0.2~0.45；
- target_tiers 的 prob 必须 ≤ confidence 且随档位递减。"""


DELTA_SYSTEM = """你是 A 股短线跟踪助手（研究用途，不构成投资建议）。
仅根据「今日新增」公告/舆情与当前持仓状态，输出简短 delta 判断。
不要重复全文摘要，只写变化与对持仓的影响。"""


def delta_user(
    instrument: str,
    name: str,
    day: str,
    new_items: list[dict],
    position_ctx: dict,
) -> str:
    lines = [
        f"分析日: {day}",
        f"标的: {instrument} {name}",
        f"持仓状态: {json.dumps(position_ctx, ensure_ascii=False)}",
        "",
        f"## 今日新增材料（{len(new_items)} 条）",
    ]
    for i, it in enumerate(new_items[:12], 1):
        lines.append(
            f"{i}. [{it.get('source')}] {it.get('published')} | {it.get('title')}"
        )
        if it.get("content"):
            lines.append(f"   {(it.get('content') or '')[:160]}")
    lines += [
        "",
        "请只输出 JSON：",
        '{"stance":"hold|exit|watch","headline":"≤40字","summary":"≤120字",',
        ' "risk_change":"升高|降低|不变","invalidate":false}',
        "- invalidate=true 表示催化证伪应失效退出",
    ]
    return "\n".join(lines)


def analyst_user(brief: str) -> str:
    return f"请基于以下材料写事实摘要：\n\n{brief}"


def bull_user(summary: str) -> str:
    return f"分析师摘要：\n{summary}\n\n请给出多头要点（10 日 +10% 的可能性）。"


def bear_user(summary: str, bull: str) -> str:
    return (
        f"分析师摘要：\n{summary}\n\n"
        f"多头观点：\n{bull}\n\n"
        "请给出空头反驳与尾部风险要点。"
    )


def judge_user(instrument: str, summary: str, bull: str, bear: str) -> str:
    return (
        f"标的: {instrument}\n\n"
        f"【分析师摘要】\n{summary}\n\n"
        f"【多头】\n{bull}\n\n"
        f"【空头】\n{bear}\n\n"
        "请输出最终 JSON 预测决策。"
    )
