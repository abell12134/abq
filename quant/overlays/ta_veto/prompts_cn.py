"""中文提示词：基本面+公告+新闻简报 → Bull/Bear 多轮 → 最终 VETO/PASS。"""

from __future__ import annotations

from .schema import RISK_TAGS

RISK_TAG_LIST = "、".join(sorted(RISK_TAGS))

ANALYST_SYSTEM = """你是 A 股卖方分析师助理。根据给定的价量、基本面、公告、新闻，
用中文写一份不超过 12 行的「事实摘要」，只陈述材料中的事实与明显风险线索，不要给买卖建议。
若某类数据缺失，明确写「缺失」。不要编造公告或财报数字。"""

BULL_SYSTEM = """你是多头研究员。基于分析师摘要，用 3~5 条中文要点论证「可以买入/风险可控」。
只依据摘要中的事实；不得编造新闻或财务数据。不要输出 JSON。"""

BEAR_SYSTEM = """你是空头研究员。基于分析师摘要与多头观点，用 3~5 条中文要点论证「应否决买入」的尾部风险。
重点关注：造假嫌疑、重大诉讼、停牌风险、明显利空未定价、财务恶化。
只依据摘要中的事实；不得编造。不要输出 JSON。"""

JUDGE_SYSTEM = f"""你是组合风险裁判。已有分析师摘要、多头与空头辩论。
你的唯一任务：决定对本标的的买入候选是 pass 还是 veto。

硬规则：
- 只输出一个 JSON 对象（不要 markdown）；
- 字段：instrument, action("veto"|"pass"), confidence(0~1),
  risk_tags(数组), reasons(1~3 条中文短句)；
- veto 时 risk_tags 必须从：{RISK_TAG_LIST}；
- 证据不足、仅估值偏贵/涨多了 → pass；
- 公告/新闻中有明确立案、退市风险、巨额亏损恶化、停牌风险等 → 才可高置信 veto。
"""


def analyst_user(brief: str) -> str:
    return f"请基于以下材料写事实摘要：\n\n{brief}"


def bull_user(summary: str) -> str:
    return f"分析师摘要：\n{summary}\n\n请给出多头要点。"


def bear_user(summary: str, bull: str) -> str:
    return (
        f"分析师摘要：\n{summary}\n\n"
        f"多头观点：\n{bull}\n\n"
        "请给出空头反驳与尾部风险要点。"
    )


def judge_user(instrument: str, summary: str, bull: str, bear: str, rounds: int) -> str:
    return (
        f"标的: {instrument}\n辩论轮数: {rounds}\n\n"
        f"【分析师摘要】\n{summary}\n\n"
        f"【多头】\n{bull}\n\n"
        f"【空头】\n{bear}\n\n"
        "请输出最终 JSON 裁判。"
    )


# 兼容旧单次调用
SYSTEM_PROMPT = JUDGE_SYSTEM
DEBATE_HINT = "结合多空观点后只输出 JSON。"


def user_prompt_for_instrument(brief: str) -> str:
    return f"材料：\n{brief}\n\n请输出 JSON 裁判。"
