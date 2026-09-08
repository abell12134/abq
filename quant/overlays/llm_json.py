"""从 LLM 正文里抠 JSON 对象（本地模型常夹思维链 / 未转义引号 / 尾逗号 / 截断）。

加固点（vs 旧版）：
1. 思维链前缀：不只抓第一个 `{`，而是从每个 `{` 试解析，挑含报告字段（sentiment/score/headline…）的对象。
2. 截断自动补全：JSON 被 max_tokens 砍断时，按字符串/括号栈自动补 `"` `}` `]` 再解析。
"""

from __future__ import annotations

import json
import re
from typing import Any

_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)
_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)

# 报告对象特征键：用于在多个 `{` 候选里挑真正的报告对象（排除思维链里的示例 JSON）
_REPORT_KEYS = frozenset({
    "sentiment", "score", "headline", "summary", "instrument",
    "risk_tags", "stance", "key_events", "watchpoints", "fundamentals",
})


def parse_json_object(text: str) -> dict[str, Any] | None:
    text = (text or "").strip()
    if not text:
        return None
    text = _THINK.sub("", text).strip()
    # 1) 代码块围栏 ```json {...} ```
    fence = _FENCE.search(text)
    if fence:
        obj = _try_loads(fence.group(1))
        if isinstance(obj, dict):
            return obj
    # 2) 扫描每个 `{` 候选，挑含报告字段最多的对象
    return _scan_objects(text)


def _scan_objects(text: str) -> dict[str, Any] | None:
    """从每个 `{` 位置尝试解析，返回含报告字段最多的 dict。

    思维链前缀（如 nemotron 的 "Here's a thinking process:..."）里可能含示例 `{...}`，
    旧版只抓第一个 `{` 会误中；这里按命中报告字段数排序，优先真正的报告对象。
    """
    best: tuple[int, dict] | None = None  # (命中字段数, obj)
    i = 0
    n = len(text)
    while i < n:
        i = text.find("{", i)
        if i < 0:
            break
        blob = _object_from(text, i)
        i += 1
        if not blob:
            continue
        obj = _try_loads(blob)
        if not isinstance(obj, dict):
            continue
        hits = len(_REPORT_KEYS & set(obj.keys()))
        if best is None or hits > best[0]:
            best = (hits, obj)
            # 命中大半报告字段即可早停，避免长文本里扫所有 `{`
            if hits >= 3:
                break
    return best[1] if best else None


def _object_from(text: str, start: int) -> str | None:
    """从 start 处的 `{` 取到与之配对的最外层 `}`（平衡括号，跳过字符串内的括号）。

    无配对 `}` 时（截断）返回到末尾，交给 _close_truncated 补全。
    """
    depth = 0
    in_str = False
    esc = False
    for j in range(start, len(text)):
        c = text[j]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start : j + 1]
    return text[start:]  # 截断：无配对 }，返回到末尾


def _try_loads(blob: str) -> Any | None:
    """多策略解析：原样 → 修复（尾逗号/智能引号/未转义引号）→ 截断补全 → 补全+修复。"""
    candidates = (
        blob,
        _repair(blob),
        _close_truncated(blob),
        _repair(_close_truncated(blob)),
    )
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
        try:
            obj, _ = json.JSONDecoder().raw_decode(candidate)
            return obj
        except json.JSONDecodeError:
            pass
    return None


def _repair(blob: str) -> str:
    """去掉尾逗号，并把字符串值里未转义的 " 转义掉，智能引号归一化。"""
    s = re.sub(r",(\s*[}\]])", r"\1", blob)
    s = s.replace("\u201c", "「").replace("\u201d", "」")
    s = s.replace("\u2018", "‘").replace("\u2019", "’")
    return _escape_inner_quotes(s)


def _close_truncated(blob: str) -> str:
    """截断自动补全：按字符串/括号栈补 `"` `}` `]`，救回被 max_tokens 砍断的 JSON。"""
    stack: list[str] = []
    in_str = False
    esc = False
    for c in blob:
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            stack.append("}")
        elif c == "[":
            stack.append("]")
        elif c in "}]":
            if stack and stack[-1] == c:
                stack.pop()
    suffix = ""
    if in_str:
        # 字符串未闭合：先补引号；若引号前是裸 key（如 "key_e）需补 : ""
        suffix += '"'
    closers = "".join(reversed(stack))
    if not closers and not suffix:
        return blob  # 已完整，无需补
    # 去掉悬空尾逗号（避免 ," → "," 后又补 } 产生 ,}）
    body = blob.rstrip()
    if body.endswith(","):
        body = body[:-1]
    return body + suffix + closers


def _escape_inner_quotes(s: str) -> str:
    out: list[str] = []
    in_str = False
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if not in_str:
            out.append(c)
            if c == '"':
                in_str = True
            i += 1
            continue
        if c == "\\":
            out.append(c)
            if i + 1 < n:
                out.append(s[i + 1])
                i += 2
            else:
                i += 1
            continue
        if c == '"':
            j = i + 1
            while j < n and s[j] in " \t\r\n":
                j += 1
            # 空字符串 / 字段结束：后面是 JSON 结构符
            if j >= n or s[j] in ",:}]":
                in_str = False
                out.append(c)
            else:
                out.append('\\"')
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)
