"""单票数据采集：行情/技术 + 新闻/公告 + 基面 + 情绪面 + 市场背景。

四类数据一次性采集，供 4 个分析师共享。复用：
  - ops.common qlib 行情 + indicators.compute 技术指标
  - overlays.sentiment_memory.sources 多源新闻/公告/政策
  - akshare 公司信息/财务摘要（超时保护）
  - overlays.sentiment_memory.store 舆情长期记忆报告
  - overlays.swing_hunter.schema 短线猎手 news_brief
"""

from __future__ import annotations

import logging
import os
from typing import Any

from . import indicators

log = logging.getLogger(__name__)


def _fetch_klines(instrument: str, n: int = 90) -> list[dict[str, Any]]:
    """qlib 后复权日线（CLI 语境最可靠；webapp 进程亦可）。"""
    try:
        import common as C  # ops.common
        C.init_qlib()
        from qlib.data import D
        cal = C.calendar()
        if not cal:
            return []
        end = cal[-1]
        start = cal[max(0, len(cal) - n - 1)]
        df = D.features([instrument],
                        ["$open/$factor", "$close/$factor", "$high/$factor",
                         "$low/$factor", "$volume"],
                        start_time=str(start), end_time=str(end))
    except Exception as e:  # noqa: BLE001
        log.info("qlib 行情失败 %s: %s", instrument, e)
        return []
    if df is None or df.empty:
        return []
    if "instrument" in getattr(df.index, "names", []):
        df = df.droplevel("instrument")
    rows = []
    for ts, r in df.iterrows():
        try:
            rows.append({
                "date": str(ts)[:10],
                "open": round(float(r.iloc[0]), 3),
                "close": round(float(r.iloc[1]), 3),
                "high": round(float(r.iloc[2]), 3),
                "low": round(float(r.iloc[3]), 3),
                "volume": float(r.iloc[4]),
            })
        except (TypeError, ValueError, IndexError):
            continue
    return rows


def _fetch_fundamentals(instrument: str) -> dict[str, str]:
    """akshare 公司信息 + 财务摘要（带超时，失败返回空串）。"""
    code = instrument[2:]
    out = {"info": "", "financial": ""}

    def _call():
        import akshare as ak
        info = ak.stock_individual_info_em(symbol=code)
        try:
            fin = ak.stock_financial_abstract(symbol=code)
        except Exception:  # noqa: BLE001
            fin = None
        return info, fin

    try:
        import concurrent.futures
        ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            info, fin = ex.submit(_call).result(timeout=20.0)
        finally:
            ex.shutdown(wait=False, cancel_futures=True)
    except Exception as e:  # noqa: BLE001
        log.info("akshare 基面失败 %s: %s", instrument, e or type(e).__name__)
        return out

    try:
        if info is not None and not getattr(info, "empty", True):
            lines = []
            for _, row in info.iterrows():
                lines.append(f"{row.iloc[0]}: {row.iloc[1]}")
            out["info"] = "\n".join(lines)[:800]
    except Exception:  # noqa: BLE001
        pass
    try:
        if fin is not None and not getattr(fin, "empty", True):
            # 取前若干行转置为可读文本
            head = fin.head(6)
            out["financial"] = head.to_string(index=False)[:1200]
    except Exception:  # noqa: BLE001
        pass
    return out


def _market_notes(limit: int = 6) -> list[str]:
    try:
        from overlays.sentiment_memory import store as sm_store
        raw = sm_store.load_raw(lookback_days=3)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for it in raw:
        if (str(it.get("source", "")).startswith("policy_")
                or it.get("kind") == "政策宏观"):
            out.append(f"{it.get('published')} | {it.get('title')}")
    return out[:limit]


def gather(
    instrument: str,
    name: str,
    day: str,
    *,
    lookback_days: int = 90,
    global_cache: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """采集单票四类共享数据。"""
    instrument = instrument.upper()
    bundle: dict[str, Any] = {
        "instrument": instrument, "name": name, "day": day,
        "market": {}, "news": [], "fundamentals": {}, "social": {},
        "market_notes": [],
    }

    # 行情/技术
    kl = _fetch_klines(instrument, n=90)
    bundle["market"] = indicators.compute(kl)

    # 新闻/公告/政策（RESEARCH_SKIP_SOURCES=1 时跳过，便于离线 dry-run）
    if os.environ.get("RESEARCH_SKIP_SOURCES"):
        bundle["news"] = []
    else:
        try:
            from overlays.sentiment_memory import sources as S
            news = S.collect_for_instrument(
                instrument, name=name, lookback_days=lookback_days,
                global_cache=global_cache)
            bundle["news"] = news[:40]
        except Exception as e:  # noqa: BLE001
            log.info("新闻采集失败 %s: %s", instrument, e)
            bundle["news"] = []

    # 基面（RESEARCH_SKIP_FUNDAMENTALS=1 时跳过 akshare）
    if os.environ.get("RESEARCH_SKIP_FUNDAMENTALS"):
        bundle["fundamentals"] = {"info": "", "financial": ""}
    else:
        bundle["fundamentals"] = _fetch_fundamentals(instrument)

    # 情绪面：舆情长期记忆报告 + 短线猎手参考
    social: dict[str, Any] = {"sentiment": "", "score": None,
                              "swing_action": None, "swing_score": None,
                              "swing_news": ""}
    try:
        from overlays.sentiment_memory import store as sm_store
        rep = sm_store.load_report(instrument)
        if rep:
            social["sentiment"] = (f"{rep.get('headline','')}\n{rep.get('summary','')}"
                                   f"\n态度:{rep.get('stance','')} 风险:{','.join(rep.get('risk_tags') or [])}")
            social["score"] = rep.get("score")
    except Exception:  # noqa: BLE001
        pass
    try:
        from overlays.swing_hunter.schema import latest_prediction_day, read_predictions
        d = latest_prediction_day()
        if d:
            pf = read_predictions(d)
            if pf:
                for p in pf.predictions:
                    if p.instrument == instrument:
                        social["swing_action"] = p.action
                        social["swing_score"] = p.swing_score
                        nb = p.news_brief or []
                        social["swing_news"] = "\n".join(
                            f"{n.get('published','')} | {n.get('title','')}"
                            for n in nb[:8])
                        break
    except Exception:  # noqa: BLE001
        pass
    bundle["social"] = social

    bundle["market_notes"] = _market_notes()
    return bundle
