"""A 股数据适配：价量/状态来自 qlib，不用 Reddit/StockTwits。

新闻/公告默认关闭；无 LLM 时由 run_veto fail-open。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import yaml

QUANT = Path(__file__).resolve().parents[2]
DEFAULT_CFG = QUANT / "configs" / "global.yaml"


def _qlib_uri() -> str:
    cfg = yaml.safe_load(DEFAULT_CFG.read_text())
    return str(Path(cfg["paths"]["qlib_data"]).expanduser())


def init_qlib() -> None:
    import qlib

    qlib.init(provider_uri=_qlib_uri(), region="cn")


def load_price_context(
    instruments: list[str],
    day: str,
    lookback: int = 60,
) -> dict[str, dict[str, Any]]:
    """为候选标的构建截至 day 的价量摘要（无前视）。"""
    if not instruments:
        return {}
    init_qlib()
    from qlib.data import D

    start = (pd.Timestamp(day) - pd.Timedelta(days=lookback * 2)).strftime("%Y-%m-%d")
    fields = ["$close/$factor", "$open/$factor", "$high/$factor", "$low/$factor", "$volume", "$close"]
    df = D.features(instruments, fields, start_time=start, end_time=day)
    if df is None or df.empty:
        return {i: {"instrument": i, "note": "no_price_data"} for i in instruments}

    out: dict[str, dict[str, Any]] = {}
    for inst in instruments:
        try:
            sub = df.xs(inst, level="instrument").sort_index()
        except KeyError:
            out[inst] = {"instrument": inst, "note": "no_price_data"}
            continue
        if sub.empty:
            out[inst] = {"instrument": inst, "note": "no_price_data"}
            continue
        # 真实价优先用 $close/$factor；若缺失则退回 $close
        px = sub["$close/$factor"].dropna()
        if px.empty:
            px = sub["$close"].dropna()
        vol = sub["$volume"].dropna()
        if len(px) < 5:
            out[inst] = {"instrument": inst, "note": "insufficient_bars", "n_bars": int(len(px))}
            continue
        last = float(px.iloc[-1])
        ret5 = float(px.iloc[-1] / px.iloc[-6] - 1) if len(px) >= 6 else None
        ret20 = float(px.iloc[-1] / px.iloc[-21] - 1) if len(px) >= 21 else None
        rets = px.pct_change().dropna()
        vol20 = float(rets.tail(20).std()) if len(rets) >= 5 else None
        liq = None
        if len(vol) >= 20:
            liq = float(vol.tail(5).mean() / (vol.tail(20).mean() + 1e-9))
        high20 = float(px.tail(20).max())
        out[inst] = {
            "instrument": inst,
            "asof": day,
            "last_price": round(last, 4),
            "ret_5d": None if ret5 is None else round(ret5, 4),
            "ret_20d": None if ret20 is None else round(ret20, 4),
            "vol_20d": None if vol20 is None else round(vol20, 4),
            "near_20d_high": round(last / high20 - 1, 4) if high20 else None,
            "liq_ratio_5_20": None if liq is None else round(liq, 4),
            "n_bars": int(len(px)),
        }
    return out


def load_trade_flags(instruments: list[str], day: str) -> pd.DataFrame:
    """涨跌停/停牌标记（复用 ops.common.trade_status）。"""
    import sys

    sys.path.insert(0, str(QUANT / "ops"))
    import common as C  # noqa: WPS433

    if not instruments:
        return pd.DataFrame(columns=["limit_up", "limit_down", "suspended"])
    return C.trade_status(sorted(instruments), day)


def build_candidate_brief(
    instrument: str,
    score: float | None,
    rank: int | None,
    price_ctx: dict[str, Any],
    flags: dict[str, Any] | None = None,
    research_text: str | None = None,
) -> str:
    """给 LLM 的单票中文简报（价量 + 可选公告/新闻/基本面）。"""
    flags = flags or {}
    lines = [
        f"标的: {instrument}",
        f"模型分数: {score if score is not None else 'N/A'}",
        f"当日横截面名次: {rank if rank is not None else 'N/A'}",
        f"价量摘要: {json_dumps_safe(price_ctx)}",
    ]
    if flags:
        lines.append(
            "交易状态: "
            f"涨停={bool(flags.get('limit_up'))} "
            f"跌停={bool(flags.get('limit_down'))} "
            f"停牌={bool(flags.get('suspended'))}"
        )
    if research_text:
        lines.append(research_text)
    else:
        lines.append("舆情/社交数据: 本阶段关闭")
        lines.append("公告/新闻/基本面: 未注入")
    return "\n".join(lines)


def json_dumps_safe(obj: Any) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False, default=str)
