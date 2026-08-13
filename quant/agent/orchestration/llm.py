"""Agent LLM access — prefer LLM_PEAK_* (local Ollama / nemotron).

Uses overlays.sentiment_memory.llm_router; Agent always tries peak first
regardless of clock hour (production console wants the self-hosted model).
Falls back to offpeak unless AGENT_LLM_PEAK_ONLY=1.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

QUANT = Path(__file__).resolve().parents[2]


def _router():
    sys.path.insert(0, str(QUANT / "overlays"))
    from sentiment_memory import llm_router as R  # noqa: WPS433

    return R


def peak_only() -> bool:
    return (os.environ.get("AGENT_LLM_PEAK_ONLY") or "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def chat(
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.2,
    max_tokens: int | None = 2048,
    timeout: float | None = 120.0,
) -> tuple[str, dict[str, Any]]:
    """Peak-first chat for the analysis Agent."""
    R = _router()
    errors: list[str] = []

    # 1) force peak (LLM_PEAK_BASE_URL / LLM_PEAK_MODEL)
    try:
        text, meta = R.chat(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            force="peak",
            timeout=timeout,
        )
        meta = dict(meta)
        meta["agent_route"] = "peak"
        return text, meta
    except Exception as exc:  # noqa: BLE001
        errors.append(f"peak: {exc}")
        if peak_only():
            raise RuntimeError("; ".join(errors)) from exc

    # 2) offpeak fallback (DeepSeek etc.)
    text, meta = R.chat(
        messages,
        temperature=temperature,
        max_tokens=max_tokens,
        force="offpeak",
        timeout=timeout,
    )
    meta = dict(meta)
    meta["agent_route"] = "offpeak_fallback"
    meta["peak_errors"] = errors
    return text, meta


def describe_route() -> dict[str, Any]:
    R = _router()
    peak = R.peak_endpoint()
    return {
        "peak_only": peak_only(),
        "peak_configured": peak is not None,
        "peak_base_url": peak.base_url if peak else None,
        "peak_model": peak.model if peak else None,
        "peak_backend": peak.backend if peak else None,
        "is_peak_hour": R.is_peak_hour(),
    }
