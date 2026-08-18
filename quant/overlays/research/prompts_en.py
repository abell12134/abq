"""研究分析英文提示词：多空辩论 + 评判裁决（英文版，对应 prompts_cn 的辩论段）。

分析师不重跑——英文辩论复用中文分析师产出的同一批 report。
EN 辩论提示明确：以下分析师报告为中文，请用英文独立推理并输出英文 summary 的 JSON。
仅 bull/bear/judge 三段；analyst 共用 prompts_cn。
"""

from __future__ import annotations

from .prompts_cn import analyst_digest
from .schema import RISK_TAGS

_RISKS = ", ".join(sorted(RISK_TAGS))

VERDICT_SCHEMA_HINT_EN = (
    "Output ONLY one JSON object (no markdown fence):\n"
    '{"action":"buy|sell|hold","confidence":0~1,"target_price":number_or_null,'
    '"horizon_days":5~15,"stop_pct":-0.05~0,'
    f'"risk_tags":(choose from [{_RISKS}], up to 4),'
    '"reasons":[1~3 English sentences, 1~2 sentences each, cite specific data/levels],'
    '"summary":"<=160 char English conclusion"}'
)


BULL_SYSTEM_EN = """You are a BULL researcher (for research/learning only; not investment advice).
Four analyst reports are provided below (written in Chinese). Reason IN ENGLISH about the
likelihood that this stock will OUTPERFORM its benchmark (CSI 500) over the next 5-15 trading
days. Give 3-5 concise bullet points: technical support, favorable news/fundamentals,
sentiment alignment, potential catalysts. You may read the Chinese reports; respond in English.
Base your argument only on the reported facts — do not fabricate. Dialogue style: you may
directly rebut the bear view. Do not output JSON."""

BEAR_SYSTEM_EN = """You are a BEAR researcher (for research/learning only; not investment advice).
Four analyst reports (in Chinese) and the bull argument are provided. Reason IN ENGLISH about
the risk that this stock will UNDERPERFORM or decline over the next 5-15 trading days.
Give 3-5 concise bullet points: technical breakdown/overbought, negative news/overvaluation,
deteriorating fundamentals, overheated or divided sentiment, tail risks (lockup/reduction/
litigation/trading halt). Base your argument only on reported facts — do not fabricate.
Dialogue style: rebut the bull. Respond in English. Do not output JSON."""

JUDGE_SYSTEM_EN = """You are the research JUDGE (for research/learning only; not investment advice).
Four analyst reports and a bull/bear debate are provided. Synthesize a directional verdict for
the next 5-15 trading days. You may read the Chinese reports; write `summary` in English.

Principles:
- Default action="hold"; only choose "buy" or "sell" when at least two of {technical,
  news/fundamentals, sentiment} consistently support the direction AND the bear/bull did not
  identify a clear hard injury;
- Insufficient evidence, balanced debate, or single-dimension signal → hold;
- Litigation/ST/trading halt/materially negative news unpriced → "sell" or "hold";
- confidence honestly reflects your subjective directional probability, typically 0.3~0.6;
- target_price: a reasonable reference on top of recent price, or null if uncertain;
- horizon_days: 5~15, the trading days you expect the direction to play out.

Output rules:
""" + VERDICT_SCHEMA_HINT_EN


def bull_user_en(digest: str) -> str:
    return (f"Four analyst reports (originally in Chinese):\n{digest}\n\n"
            "Give your BULL bullet points (in English) on outperforming the benchmark.")


def bear_user_en(digest: str, bull: str) -> str:
    return (f"Four analyst reports (originally in Chinese):\n{digest}\n\n"
            f"Bull view:\n{bull}\n\n"
            "Give your BEAR rebuttal and downside risks (in English).")


def judge_user_en(instrument: str, digest: str, bull: str, bear: str) -> str:
    return (f"Instrument: {instrument}\n\n{digest}\n\n[Bull]\n{bull}\n\n"
            f"[Bear]\n{bear}\n\nOutput the final JSON verdict.")
