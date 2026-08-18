"""从 LLM 正文里抠 JSON 对象（本地模型常夹思维链 / 未转义引号 / 尾逗号）。"""

from __future__ import annotations

import json
import re
from typing import Any

_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)
_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


def parse_json_object(text: str) -> dict[str, Any] | None:
    text = (text or "").strip()
    if not text:
        return None
    text = _THINK.sub("", text).strip()
    fence = _FENCE.search(text)
    blob = fence.group(1) if fence else _first_object(text)
    if not blob:
        return None
    obj = _loads(blob)
    return obj if isinstance(obj, dict) else None


def _first_object(text: str) -> str | None:
    start = text.find("{")
    if start < 0:
        return None
    end = text.rfind("}")
    if end > start:
        return text[start : end + 1]
    return text[start:]


def _loads(blob: str) -> Any | None:
    for candidate in (blob, _repair(blob)):
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
    """去掉尾逗号，并把字符串值里未转义的 \" 转义掉。"""
    s = re.sub(r",(\s*[}\]])", r"\1", blob)
    s = s.replace("\u201c", "「").replace("\u201d", "」")
    s = s.replace("\u2018", "‘").replace("\u2019", "’")
    return _escape_inner_quotes(s)


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
