"""高峰/闲时 LLM 路由。

高峰（北京时间 9:00–12:00、14:00–18:00）优先自部署端点（`LLM_PEAK_*`）：
  - Ollama（URL 含 `ollama` 或 `LLM_PEAK_BACKEND=ollama`）→ 原生 `/api/chat`，
    顶层 `think=False` 关闭思维链；
  - 其它 OpenAI 兼容端点 → `/v1/chat/completions`。
闲时或高峰失败时回落到 DeepSeek 云 API（configs/secret.env）。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
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
    backend: str = "openai"  # openai | ollama


def _detect_backend(base_url: str, explicit: str | None = None) -> str:
    if (explicit or "").strip().lower() in ("ollama", "openai"):
        return explicit.strip().lower()
    return "ollama" if "ollama" in (base_url or "").lower() else "openai"


def _ollama_root(base_url: str) -> str:
    """`.../ollama/v1` → `.../ollama`；已是根路径则原样返回。"""
    root = (base_url or "").rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3]
    return root.rstrip("/")


def peak_endpoint() -> LLMEndpoint | None:
    key = _get("LLM_PEAK_API_KEY")
    base = _get("LLM_PEAK_BASE_URL", "http://127.0.0.1:8001/ollama/v1")
    model = _get(
        "LLM_PEAK_MODEL",
        "frob/deepseek-v4-flash-0731:284b-a13b-ud-q2_k_xl",
    )
    if not key or not base:
        return None
    backend = _detect_backend(base, _get("LLM_PEAK_BACKEND"))
    return LLMEndpoint(
        label="peak", base_url=base.rstrip("/"), api_key=key,
        model=model or "frob/deepseek-v4-flash-0731:284b-a13b-ud-q2_k_xl",
        is_peak=True, backend=backend,
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
        model=model, is_peak=False, backend="openai",
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


def _extract_text(msg: Any) -> str:
    """从 OpenAI / Ollama message 取可见正文；空则回退 reasoning。"""
    if isinstance(msg, dict):
        text = (msg.get("content") or "").strip()
        if text:
            return text
        return str(msg.get("thinking") or msg.get("reasoning")
                   or msg.get("reasoning_content") or "").strip()
    text = (getattr(msg, "content", None) or "").strip()
    if text:
        return text
    text = (
        getattr(msg, "reasoning_content", None)
        or getattr(msg, "reasoning", None)
        or getattr(msg, "thinking", None)
        or ""
    )
    if not text:
        raw = getattr(msg, "model_extra", None) or {}
        if isinstance(raw, dict):
            text = raw.get("reasoning_content") or raw.get("reasoning") or raw.get("thinking") or ""
    return str(text).strip()


def _chat_ollama(ep: LLMEndpoint, messages: list[dict[str, str]], *,
                 temperature: float, max_tokens: int,
                 timeout: float) -> tuple[str, dict[str, Any]]:
    """原生 Ollama `/api/chat`；顶层 think=False 才能真正关思维链。"""
    url = f"{_ollama_root(ep.base_url)}/api/chat"
    think_raw = (_get("LLM_PEAK_THINK", "false") or "false").strip().lower()
    think = think_raw in ("1", "true", "yes", "on")
    body: dict[str, Any] = {
        "model": ep.model,
        "messages": messages,
        "stream": False,
        "think": think,
        "options": {
            "temperature": temperature,
            "num_predict": int(max_tokens),
        },
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {ep.api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"HTTP {e.code}: {detail}") from e
    msg = data.get("message") or {}
    text = _extract_text(msg)
    if not text:
        raise RuntimeError(f"{ep.label}/{ep.model} 返回空内容")
    meta = {
        "endpoint": ep.label,
        "backend": "ollama",
        "model": ep.model,
        "base_url": ep.base_url,
        "ollama_url": url,
        "think": think,
        "peak_hour": is_peak_hour(),
        "max_tokens": max_tokens,
        "usage": {
            "prompt_tokens": data.get("prompt_eval_count"),
            "completion_tokens": data.get("eval_count"),
        },
    }
    return text, meta


def _chat_openai(ep: LLMEndpoint, messages: list[dict[str, str]], *,
                 temperature: float, max_tokens: int,
                 timeout: float) -> tuple[str, dict[str, Any]]:
    from openai import OpenAI

    client = OpenAI(api_key=ep.api_key, base_url=ep.base_url, timeout=timeout)
    kwargs: dict[str, Any] = {
        "model": ep.model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if ep.is_peak:
        # 非 Ollama 自部署推理模型的尽力关 thinking（Ollama 走原生 API）
        kwargs["extra_body"] = {
            "chat_template_kwargs": {"enable_thinking": False},
            "enable_thinking": False,
            "think": False,
        }
    resp = client.chat.completions.create(**kwargs)
    text = _extract_text(resp.choices[0].message)
    if not text:
        raise RuntimeError(f"{ep.label}/{ep.model} 返回空内容")
    meta = {
        "endpoint": ep.label,
        "backend": "openai",
        "model": ep.model,
        "base_url": ep.base_url,
        "peak_hour": is_peak_hour(),
        "max_tokens": max_tokens,
        "usage": {
            "prompt_tokens": getattr(resp.usage, "prompt_tokens", None),
            "completion_tokens": getattr(resp.usage, "completion_tokens", None),
        },
    }
    return text, meta


def chat(messages: list[dict[str, str]], *,
         temperature: float = 0.3, max_tokens: int | None = None,
         force: str | None = None,
         timeout: float | None = None) -> tuple[str, dict[str, Any]]:
    """调用 LLM；高峰失败自动回落闲时。返回 (text, meta)。"""
    tried: list[str] = []
    errors: list[str] = []
    primary = resolve_endpoint(force)
    candidates = [primary]
    if primary.is_peak and force is None:
        candidates.append(offpeak_endpoint())

    last_err: Exception | None = None
    for ep in candidates:
        tried.append(ep.label)
        # Ollama 关 thinking 后无需 8k；OpenAI 高峰仍给足预算
        if max_tokens is not None:
            ep_max = max_tokens
        elif ep.backend == "ollama":
            ep_max = 2048
        elif ep.is_peak:
            ep_max = 8192
        else:
            ep_max = 4096
        ep_timeout = timeout if timeout is not None else (180.0 if ep.is_peak else 90.0)
        try:
            if ep.backend == "ollama":
                text, meta = _chat_ollama(
                    ep, messages, temperature=temperature,
                    max_tokens=ep_max, timeout=ep_timeout,
                )
            else:
                text, meta = _chat_openai(
                    ep, messages, temperature=temperature,
                    max_tokens=ep_max, timeout=ep_timeout,
                )
            meta["tried"] = tried
            return text, meta
        except Exception as e:  # noqa: BLE001
            last_err = e
            errors.append(f"{ep.label}/{ep.backend}: {e}")
            continue
    raise RuntimeError("LLM 调用失败：" + " | ".join(errors)) from last_err
