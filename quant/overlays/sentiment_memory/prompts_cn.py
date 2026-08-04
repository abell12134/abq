"""舆情摘要与持续分析提示词。"""

from __future__ import annotations

SYSTEM = """你是 A 股舆情分析助手（研究/学习用途，不构成投资建议）。
任务：基于给定公开舆情条目与历史记忆片段，对单只股票做结构化摘要与风险跟踪。

规则：
1. 只依据提供的材料，不要臆造未出现的事实；证据不足请明确写「证据不足」。
2. 区分：硬伤风险（立案/造假/暴雷/重大诉讼/控股股东重大违规等） vs 普通利空/噪音。
3. 情绪分 score ∈ [-1,1]：-1 极度负面，0 中性，1 极度正面；结合近 30–90 日材料综合判断。
4. sentiment 只能取：positive / neutral / negative / mixed。
5. 只输出一个 JSON 对象，不要 markdown 围栏外的闲聊。
"""


def build_user_prompt(
    day: str,
    instrument: str,
    name: str,
    news: list[dict],
    memories: list[dict],
) -> str:
    lines = [
        f"分析日: {day}",
        f"标的: {instrument} {name}",
        "",
        "## 近期舆情条目（按时间倒序，最多 40 条）",
    ]
    for i, n in enumerate(news[:40], 1):
        title = (n.get("title") or "")[:80]
        content = (n.get("content") or "")[:180]
        lines.append(
            f"{i}. [{n.get('source')}] {n.get('published')} | {title}"
        )
        if content and content != title:
            lines.append(f"   {content}")
        if n.get("url"):
            lines.append(f"   url: {n['url']}")

    lines += ["", "## 本地长期记忆检索（相似历史片段）"]
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
        '  "summary": "200字以内中文摘要",',
        '  "risk_tags": ["可选硬伤标签"],',
        '  "key_events": [{"date":"YYYY-MM-DD","event":"…","impact":"利多|利空|中性"}],',
        '  "watchpoints": ["后续跟踪点，最多5条"],',
        '  "stance": "可继续跟踪|谨慎|建议回避（研究口径，非投资建议）"',
        "}",
    ]
    return "\n".join(lines)
