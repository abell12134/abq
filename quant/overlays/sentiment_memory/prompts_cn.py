"""舆情摘要与持续分析提示词。"""

from __future__ import annotations


SYSTEM = """你是 A 股舆情与基本面跟踪助手（研究/学习用途，不构成投资建议）。
任务：综合近 90 日（约三个月）的【公司公告/财报】【政策宏观】【媒体舆情】与本地长期记忆，
对单只股票做结构化摘要与风险跟踪。

规则：
1. 只依据提供的材料，不要臆造未出现的事实；证据不足请明确写「证据不足」。
2. 公告/财报优先于媒体二手解读；定期报告、业绩预告/快报必须在 summary 或 key_events 中体现（若材料有）。
3. 政策宏观只写与该公司所在行业/业务明显相关，或足以影响整体风险偏好的重大政策；无关噪音忽略。
4. 区分：硬伤风险（立案/造假/暴雷/重大诉讼/控股股东重大违规等） vs 普通利空/噪音。
5. 情绪分 score ∈ [-1,1]：-1 极度负面，0 中性，1 极度正面；结合近 90 日材料综合判断。
6. sentiment 只能取：positive / neutral / negative / mixed。
7. 只输出一个 JSON 对象，不要 markdown 围栏外的闲聊。
"""


def _split_buckets(news: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    anns, policy, media = [], [], []
    for n in news:
        src = str(n.get("source") or "")
        kind = str(n.get("kind") or "")
        title = str(n.get("title") or "")
        if src.startswith("ann_") or kind in {"公司公告", "财报公告"} or title.startswith("[财报") or title.startswith("[公司公告]"):
            anns.append(n)
        elif src.startswith("policy_") or kind == "政策宏观" or title.startswith("[政策]"):
            policy.append(n)
        else:
            media.append(n)
    return anns, policy, media


def _fmt_items(rows: list[dict], limit: int) -> list[str]:
    lines = []
    for i, n in enumerate(rows[:limit], 1):
        title = (n.get("title") or "")[:100]
        content = (n.get("content") or "")[:200]
        lines.append(f"{i}. [{n.get('source')}] {n.get('published')} | {title}")
        if content and content != title:
            lines.append(f"   {content}")
        if n.get("url"):
            lines.append(f"   url: {n['url']}")
    if not lines:
        lines.append("（无）")
    return lines


def build_user_prompt(
    day: str,
    instrument: str,
    name: str,
    news: list[dict],
    memories: list[dict],
) -> str:
    anns, policy, media = _split_buckets(news)
    lines = [
        f"分析日: {day}",
        f"标的: {instrument} {name}",
        "",
        f"## 一、公司公告 / 财报（近 90 日，优先；共 {len(anns)} 条）",
        *_fmt_items(anns, 25),
        "",
        f"## 二、政策 / 宏观（近窗筛选；共 {len(policy)} 条）",
        *_fmt_items(policy, 15),
        "",
        f"## 三、媒体舆情 / 电报（东财/财联社/新浪；共 {len(media)} 条）",
        *_fmt_items(media, 25),
        "",
        "## 四、本地长期记忆检索（相似历史片段）",
    ]
    if not memories:
        lines.append("（暂无历史记忆）")
    else:
        for i, m in enumerate(memories[:8], 1):
            lines.append(
                f"{i}. score={m.get('score')} [{m.get('source')}] "
                f"{m.get('published')} | {m.get('title')}"
            )
            if m.get("snippet"):
                lines.append(f"   {m['snippet']}")

    lines += [
        "",
        "请严格输出如下 JSON：",
        "{",
        '  "instrument": "SH600000",',
        '  "name": "简称",',
        '  "sentiment": "positive|neutral|negative|mixed",',
        '  "score": 0.0,',
        '  "headline": "一句话结论（≤40字）",',
        '  "summary": "250字以内中文摘要，须覆盖财报要点（若有）与相关政策（若有）",',
        '  "risk_tags": ["可选硬伤标签"],',
        '  "key_events": [{"date":"YYYY-MM-DD","event":"…","impact":"利多|利空|中性"}],',
        '  "fundamentals": "财报/业绩要点一句话；无则写证据不足",',
        '  "policy_impact": "相关政策影响一句话；无则写无明显相关政策",',
        '  "watchpoints": ["后续跟踪点，最多5条"],',
        '  "stance": "可继续跟踪|谨慎|建议回避（研究口径，非投资建议）"',
        "}",
    ]
    return "\n".join(lines)
