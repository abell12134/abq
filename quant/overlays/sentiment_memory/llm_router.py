"""高峰/闲时 LLM 路由。

高峰（北京时间 9:00–12:00、14:00–18:00）优先自部署 Qwen；
闲时或高峰失败时回落到 DeepSeek（configs/secret.env）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

QUANT = Path(__file__).resolve().parents[2]
SECRET = QUANT / "configs" / "secret.env"
TZ = ZoneInfo("Asia/Shanghai")


def _load_secret() -> dict[str, str]:
    env: dict[str, str] = {}
    if SECRET.exists():
        for line in SECRET.read_text().splitlines():
            if line.strip() and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _get(key: str, default: str | None = None) -> str | None:
    s = _load_secret()
    return s.get(key) or os.environ.get(key) or default


def is_peak_hour(now: datetime | None = None) -> bool:
    """高峰时段：北京时间每日 9:00～12:00 和 14:00～18:00。"""
    t = now or datetime.now(TZ)
    if t.tzinfo is None:
        t = t.replace(tzinfo=TZ)
    else:
        t = t.astimezone(TZ)
    minutes = t.hour * 60 + t.minute
    return (9 * 60 <= minutes < 12 * 60) or (14 * 60 <= minutes < 18 * 60)


@dataclass
class LLMEndpoint:
    label: str  # peak | offpeak
    base_url: str
    api_key: str
    model: str
    is_peak: bool


def peak_endpoint() -> LLMEndpoint | None:
    key = _get("LLM_PEAK_API_KEY")
    base = _get("LLM_PEAK_BASE_URL", "http://118.195.177.58:8001/v1")
    model = _get("LLM_PEAK_MODEL", "Qwen3.6-35B-A3B")
    if not key or not base:
        return None
    return LLMEndpoint(
        label="peak", base_url=base.rstrip("/"), api_key=key,
        model=model or "Qwen3.6-35B-A3B", is_peak=True,
    )


def offpeak_endpoint() -> LLMEndpoint:
    key = _get("LLM_API_KEY") or _get("LLM_OFFPEAK_API_KEY")
    if not key:
        raise RuntimeError("缺少 LLM_API_KEY（configs/secret.env）")
    base = (_get("LLM_OFFPEAK_BASE_URL")
            or _get("LLM_BASE_URL", "https://api.deepseek.com"))
    model = (_get("LLM_OFFPEAK_MODEL")
             or _get("LLM_MODEL", "deepseek-v4-flash")
             or "deepseek-v4-flash")
    return LLMEndpoint(
        label="offpeak", base_url=base.rstrip("/"), api_key=key,
        model=model, is_peak=False,
    )


def resolve_endpoint(force: str | None = None) -> LLMEndpoint:
    """force: 'peak' | 'offpeak' | None（按时段自动）。"""
    if force == "offpeak":
        return offpeak_endpoint()
    if force == "peak":
        ep = peak_endpoint()
        if ep is None:
            raise RuntimeError("未配置 LLM_PEAK_API_KEY / LLM_PEAK_BASE_URL")
        return ep
    if is_peak_hour():
        ep = peak_endpoint()
        if ep is not None:
            return ep
    return offpeak_endpoint()


def chat(messages: list[dict[str, str]], *,
         temperature: float = 0.3, max_tokens: int | None = None,
         force: str | None = None,
         timeout: float | None = None) -> tuple[str, dict[str, Any]]:
    """调用 chat.completions；高峰失败自动回落闲时。返回 (text, meta)。

    高峰 Qwen3 默认关闭 thinking（避免 max_tokens 全被推理占满、正文为空），
    并提高超时：自部署单次生成可达 3+ 分钟。
    """
    from openai import OpenAI

    tried: list[str] = []
    errors: list[str] = []
    primary = resolve_endpoint(force)
    candidates = [primary]
    if primary.is_peak and force is None:
        candidates.append(offpeak_endpoint())

    last_err: Exception | None = None
    for ep in candidates:
        tried.append(ep.label)
        # 高峰生成慢且易被 thinking 吃光预算
        ep_max = max_tokens if max_tokens is not None else (8192 if ep.is_peak else 4096)
        ep_timeout = timeout if timeout is not None else (300.0 if ep.is_peak else 90.0)
        client = OpenAI(api_key=ep.api_key, base_url=ep.base_url, timeout=ep_timeout)
        kwargs: dict[str, Any] = {
            "model": ep.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": ep_max,
        }
        if ep.is_peak:
            # Qwen3 / vmlx：关闭思维链，把 token 留给可见 JSON 正文
            kwargs["extra_body"] = {
                "chat_template_kwargs": {"enable_thinking": False},
                "enable_thinking": False,
            }
        try:
            resp = client.chat.completions.create(**kwargs)
            msg = resp.choices[0].message
            text = (msg.content or "").strip()
            if not text:
                # 部分推理模型把草稿放 reasoning_content
                text = (getattr(msg, "reasoning_content", None) or "").strip()
            meta = {
                "endpoint": ep.label,
                "model": ep.model,
                "base_url": ep.base_url,
                "peak_hour": is_peak_hour(),
                "tried": tried,
                "max_tokens": ep_max,
                "usage": {
                    "prompt_tokens": getattr(resp.usage, "prompt_tokens", None),
                    "completion_tokens": getattr(resp.usage, "completion_tokens", None),
                },
            }
            if not text:
                raise RuntimeError(f"{ep.label}/{ep.model} 返回空内容")
            return text, meta
        except Exception as e:  # noqa: BLE001
            last_err = e
            errors.append(f"{ep.label}: {e}")
            continue
    raise RuntimeError("LLM 调用失败：" + " | ".join(errors)) from last_err
