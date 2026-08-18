"""研究分析中文提示词：4 分析师 → 多空辩论 → 评判裁决 JSON。

设计要点（复刻 TradingAgents-CN 研究管线，适配本项目数据源）：
  - 分析师只陈述材料事实，不做买卖建议；证据不足写「证据不足」；
  - 多空辩论基于分析师摘要，对话式互相反驳；
  - 评判综合出 buy/sell/hold + 目标价 + 置信度（诚实给，通常 0.3~0.6）；
  - 全程「研究/学习用途，不构成投资建议」。
"""

from __future__ import annotations

import json
from typing import Any

from .schema import RISK_TAGS

_RISKS = "、".join(sorted(RISK_TAGS))

VERDICT_SCHEMA_HINT = (
    '只输出一个 JSON 对象（不要 markdown 围栏）：\n'
    '{"action":"buy|sell|hold","confidence":0~1,"target_price":数值或null,'
    '"horizon_days":5~15,"stop_pct":-0.05~0,'
    f'"risk_tags":(从 [{_RISKS}] 中选，至多4个),'
    '"reasons":[1~3条中文理由，每条1~2句，引用具体数据/点位],"summary":"≤160字中文结论"}'
)

# ---------------- 分析师（共享，CN） ----------------

MARKET_ANALYST_SYSTEM = """你是 A 股技术面分析师（研究/学习用途，不构成投资建议）。
根据给定的行情 K 线与技术指标，用中文写不超过 12 行的「技术面摘要」：
只陈述事实——趋势方向、均线多空排列、MACD/RSI/BOLL 位置、量能变化、近期涨跌、
是否处于超买/超卖、关键支撑/阻力位。不做买卖建议，不预测精确点位。
若行情数据缺失或不足，明确写「行情证据不足」。不要编造数据。"""

NEWS_ANALYST_SYSTEM = """你是 A 股新闻与公告分析师（研究/学习用途，不构成投资建议）。
根据给定的公司公告、财报披露、媒体新闻、政策宏观条目，用中文写不超过 12 行的
「消息面摘要」：按重要性梳理——业绩/财报类公告、重大事项（合同/重组/增减持/诉讼/
立案）、行业与政策影响、媒体舆论倾向。区分「已确定事实」与「传闻/不确定性」。
若材料缺失，明确写「消息面证据不足」。不要编造公告或数据。"""

FUNDAMENTALS_ANALYST_SYSTEM = """你是 A 股基本面分析师（研究/学习用途，不构成投资建议）。
根据给定的公司基本信息与财务摘要，用中文写不超过 10 行的「基本面摘要」：
行业地位、主营业务、市值/估值水平（若可得）、营收与利润趋势、盈利能力、
资产负债与现金流状况（若可得）。不做买卖建议。
若财务数据缺失，明确写「基本面证据不足」。不要编造数据。"""

SOCIAL_ANALYST_SYSTEM = """你是 A 股市场情绪与社交舆论分析师（研究/学习用途，不构成投资建议）。
根据给定的舆情长期记忆报告、短线猎手新闻简报、社交媒体倾向，用中文写不超过 10 行的
「情绪面摘要」：投资者情绪倾向（偏多/偏空/中性/分化）、情绪强度、近期情绪变化方向、
是否存在一致性预期或分歧。不做买卖建议。若材料缺失，明确写「情绪面证据不足」。"""

ANALYST_KIND_SYSTEM = {
    "market": MARKET_ANALYST_SYSTEM,
    "news": NEWS_ANALYST_SYSTEM,
    "fundamentals": FUNDAMENTALS_ANALYST_SYSTEM,
    "social": SOCIAL_ANALYST_SYSTEM,
}


# ---------------- 多空辩论（CN） ----------------

BULL_SYSTEM_CN = """你是多头研究员（研究视角，不构成投资建议）。基于四位分析师的摘要，
用 3~5 条中文要点论证「未来 5~15 个交易日该股跑赢基准（中证500）的可能性」：
技术面是否支持、消息面/基本面是否有利、情绪是否共振、催化是否可期。
只依据摘要事实，不得编造。以对话风格呈现，可直接回应空头观点。不要输出 JSON。"""

BEAR_SYSTEM_CN = """你是空头研究员（研究视角，不构成投资建议）。基于分析师摘要与多头观点，
用 3~5 条中文要点论证「未来 5~15 个交易日该股跑输基准或下跌的风险」：
技术面破位/超买、消息面利空/估值偏高、基本面恶化、情绪过热/分歧、
解禁/减持/立案/停牌等尾部风险。只依据摘要事实，不得编造。
以对话风格反驳多头。不要输出 JSON。"""

JUDGE_SYSTEM_CN = """你是研究评判官（研究/学习用途，不构成投资建议）。
已有四位分析师摘要与多空辩论。综合判定该股未来 5~15 个交易日的方向倾向。

判定原则：
- 默认 action="hold"（持有/观望）；只有在「技术面+消息面/基本面+情绪面至少两方一致支持」
  且空头未指出明确硬伤时，才 action="buy" 或 "sell"；
- 证据不足、多空势均、仅单一维度信号 → hold；
- 有立案/ST/停牌/重大利空未定价 → action="sell" 或 hold（视下跌确定性）；
- confidence 诚实给，反映主观方向概率，通常 0.3~0.6，不要虚高；
- target_price 给一个合理参考价（近期价格基础上），无把握可填 null；
- horizon_days 取 5~15，反映你认为方向兑现所需交易日数。

输出规则：
""" + VERDICT_SCHEMA_HINT


# ---------------- user prompt 构造 ----------------


def _fmt_news(news: list[dict[str, Any]], limit: int = 12) -> str:
    if not news:
        return "（无消息面材料）"
    lines = []
    for i, n in enumerate(news[:limit], 1):
        kind = n.get("kind") or n.get("source") or ""
        lines.append(f"{i}. [{kind}] {n.get('published','')} | {n.get('title','')}")
        c = str(n.get("content") or "").strip()
        if c:
            lines.append(f"   {c[:140]}")
    return "\n".join(lines)


def analyst_user(kind: str, bundle: dict[str, Any]) -> str:
    inst = bundle.get("instrument", "")
    name = bundle.get("name", "")
    head = f"标的: {inst} {name}　分析日: {bundle.get('day','')}"
    if kind == "market":
        m = bundle.get("market") or {}
        ind = {k: v for k, v in m.items() if k not in ("recent_klines", "ok", "bars")}
        kl = m.get("recent_klines") or []
        kl_s = "\n".join(f"  {k.get('date')} close={k.get('close')} vol={k.get('volume')}"
                         for k in kl[-12:])
        return f"{head}\n\n技术指标: {json.dumps(ind, ensure_ascii=False)}\n近期K线:\n{kl_s or '  （无）'}"
    if kind == "news":
        return f"{head}\n\n消息面材料：\n{_fmt_news(bundle.get('news') or [])}"
    if kind == "fundamentals":
        f = bundle.get("fundamentals") or {}
        return (f"{head}\n\n公司基本信息:\n{(f.get('info') or '（无）')[:800]}\n\n"
                f"财务摘要:\n{(f.get('financial') or '（无）')[:1200]}")
    if kind == "social":
        s = bundle.get("social") or {}
        notes = bundle.get("market_notes") or []
        notes_s = "\n".join(f"  - {x}" for x in notes[:6])
        return (f"{head}\n\n舆情长期记忆报告:\n{(s.get('sentiment') or '（无）')[:1000]}\n"
                f"情绪分: {s.get('score')}\n"
                f"短线猎手参考: action={s.get('swing_action')} score={s.get('swing_score')}\n"
                f"短线新闻简报:\n{(s.get('swing_news') or '（无）')[:800]}\n"
                f"市场背景(政策/宏观):\n{notes_s or '  （无）'}")
    return head


def analyst_digest(analysts: list[dict[str, Any]]) -> str:
    """把 4 份分析师报告拼成辩论可读的摘要块。"""
    names = {"market": "技术面", "news": "消息面",
             "fundamentals": "基本面", "social": "情绪面"}
    parts = []
    for a in analysts:
        k = a.get("kind") if isinstance(a, dict) else getattr(a, "kind", "")
        c = a.get("content") if isinstance(a, dict) else getattr(a, "content", "")
        parts.append(f"【{names.get(k, k)}】\n{c}")
    return "\n\n".join(parts)


def bull_user_cn(digest: str) -> str:
    return f"四位分析师摘要：\n{digest}\n\n请给出多头要点（5~15 日跑赢基准的可能性）。"


def bear_user_cn(digest: str, bull: str) -> str:
    return (f"四位分析师摘要：\n{digest}\n\n多头观点：\n{bull}\n\n"
            "请给出空头反驳与下行风险要点。")


def judge_user_cn(instrument: str, digest: str, bull: str, bear: str) -> str:
    return (f"标的: {instrument}\n\n{digest}\n\n【多头】\n{bull}\n\n"
            f"【空头】\n{bear}\n\n请输出最终 JSON 裁决。")
