"""零 Key A 股数据源：公告 / 新闻 / 基本面。

默认链（可配置）：
  announcements: cninfo → eastmoney（巨潮不稳时自动回退）
  news: eastmoney
  fundamentals: baostock

舆情（雪球等）本阶段关闭。所有输出均按 asof 日截断，防前视。
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

import pandas as pd
import requests

logger = logging.getLogger(__name__)

_UA = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://data.eastmoney.com/",
}

DEFAULT_VENDORS = {
    # 巨潮在部分网络不稳定；默认东财优先，cninfo 作备选
    "announcements": ["eastmoney", "cninfo"],
    "news": ["eastmoney"],
    "fundamentals": ["baostock"],
    "sentiment": [],  # 首期关闭
}


def to_pure_code(instrument: str) -> str:
    """SH600000 / SZ000967 → 600000 / 000967。"""
    s = str(instrument).upper().strip()
    for p in ("SH", "SZ", "BJ"):
        if s.startswith(p) and len(s) == 8:
            return s[2:]
    return s[-6:] if len(s) >= 6 else s


def to_baostock_code(instrument: str) -> str:
    s = str(instrument).upper().strip()
    if s.startswith("SH") and len(s) == 8:
        return f"sh.{s[2:]}"
    if s.startswith("SZ") and len(s) == 8:
        return f"sz.{s[2:]}"
    if s.startswith("BJ") and len(s) == 8:
        return f"bj.{s[2:]}"
    code = to_pure_code(s)
    if code.startswith(("5", "6", "9")):
        return f"sh.{code}"
    return f"sz.{code}"


def _parse_dt(val: Any) -> pd.Timestamp | None:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    s = str(val).strip()
    # 东财 display_time 形如 2026-07-09 18:54:12:900（秒后还跟毫秒）
    import re

    s = re.sub(r"(\d{2}:\d{2}:\d{2}):\d+$", r"\1", s)
    try:
        ts = pd.to_datetime(s, errors="coerce")
    except Exception:
        return None
    if pd.isna(ts):
        return None
    return pd.Timestamp(ts)


def _filter_asof(items: list[dict[str, Any]], asof: str, date_key: str = "date") -> list[dict[str, Any]]:
    cut = pd.Timestamp(asof) + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
    out = []
    for it in items:
        ts = _parse_dt(it.get(date_key))
        if ts is None:
            continue
        if ts <= cut:
            out.append(it)
    return out


# --------------------------------------------------------------------------- #
# Announcements
# --------------------------------------------------------------------------- #


def fetch_announcements_cninfo(
    instrument: str,
    asof: str,
    lookback_days: int = 30,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """巨潮资讯公告（akshare 封装）。失败返回 []。"""
    try:
        import akshare as ak
    except ImportError:
        logger.warning("akshare 未安装，跳过 cninfo")
        return []

    code = to_pure_code(instrument)
    start = (pd.Timestamp(asof) - pd.Timedelta(days=lookback_days)).strftime("%Y%m%d")
    end = pd.Timestamp(asof).strftime("%Y%m%d")
    try:
        df = ak.stock_zh_a_disclosure_report_cninfo(
            symbol=code, market="沪深京", start_date=start, end_date=end
        )
    except Exception as exc:  # noqa: BLE001
        logger.info("cninfo 拉取失败 %s: %s", instrument, exc)
        return []
    if df is None or df.empty:
        return []

    # 列名随版本可能变化
    colmap = {c: c for c in df.columns}
    title_col = next((c for c in df.columns if "标题" in str(c) or "title" in str(c).lower()), None)
    date_col = next((c for c in df.columns if "日期" in str(c) or "time" in str(c).lower() or "date" in str(c).lower()), None)
    type_col = next((c for c in df.columns if "类别" in str(c) or "类型" in str(c) or "category" in str(c).lower()), None)
    if not title_col or not date_col:
        return []

    items = []
    for _, row in df.iterrows():
        items.append({
            "date": str(row[date_col]),
            "title": str(row[title_col]),
            "category": str(row[type_col]) if type_col else "",
            "source": "cninfo",
        })
    items = _filter_asof(items, asof)
    items.sort(key=lambda x: str(x.get("date", "")), reverse=True)
    return items[:limit]


def fetch_announcements_eastmoney(
    instrument: str,
    asof: str,
    lookback_days: int = 30,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """东方财富个股公告（公开 JSON，零 Key）。"""
    code = to_pure_code(instrument)
    url = "https://np-anotice-stock.eastmoney.com/api/security/ann"
    params = {
        "sr": -1,
        "page_size": max(limit * 3, 30),
        "page_index": 1,
        "ann_type": "A",
        "client_source": "web",
        "stock_list": code,
    }
    try:
        r = requests.get(url, params=params, headers=_UA, timeout=20)
        r.raise_for_status()
        data = r.json()
    except Exception as exc:  # noqa: BLE001
        logger.info("eastmoney 公告失败 %s: %s", instrument, exc)
        return []

    rows = ((data.get("data") or {}).get("list")) or []
    start = pd.Timestamp(asof) - pd.Timedelta(days=lookback_days)
    items = []
    for it in rows:
        title = it.get("title") or it.get("notice_title") or ""
        dt = it.get("display_time") or it.get("notice_date") or it.get("eiTime") or ""
        cats = [c.get("column_name", "") for c in (it.get("columns") or []) if c.get("column_name")]
        ts = _parse_dt(dt)
        if ts is None or ts < start:
            continue
        items.append({
            "date": ts.strftime("%Y-%m-%d %H:%M:%S"),
            "title": str(title),
            "category": ",".join(cats),
            "source": "eastmoney",
        })
    items = _filter_asof(items, asof)
    items.sort(key=lambda x: str(x.get("date", "")), reverse=True)
    return items[:limit]


def fetch_announcements(
    instrument: str,
    asof: str,
    vendors: list[str] | None = None,
    lookback_days: int = 30,
    limit: int = 10,
) -> list[dict[str, Any]]:
    vendors = vendors or DEFAULT_VENDORS["announcements"]
    for v in vendors:
        v = v.lower()
        if v == "cninfo":
            items = fetch_announcements_cninfo(instrument, asof, lookback_days, limit)
        elif v in {"eastmoney", "em"}:
            items = fetch_announcements_eastmoney(instrument, asof, lookback_days, limit)
        else:
            logger.warning("未知公告 vendor: %s", v)
            continue
        if items:
            return items
    return []


# --------------------------------------------------------------------------- #
# News
# --------------------------------------------------------------------------- #


def fetch_news_eastmoney(
    instrument: str,
    asof: str,
    lookback_days: int = 7,
    limit: int = 15,
) -> list[dict[str, Any]]:
    """东方财富个股新闻（akshare stock_news_em）。"""
    try:
        import akshare as ak
    except ImportError:
        return []

    code = to_pure_code(instrument)
    try:
        df = ak.stock_news_em(symbol=code)
    except Exception as exc:  # noqa: BLE001
        logger.info("eastmoney 新闻失败 %s: %s", instrument, exc)
        return []
    if df is None or df.empty:
        return []

    title_col = "新闻标题" if "新闻标题" in df.columns else df.columns[1]
    content_col = "新闻内容" if "新闻内容" in df.columns else None
    date_col = "发布时间" if "发布时间" in df.columns else None
    src_col = "文章来源" if "文章来源" in df.columns else None
    if date_col is None:
        return []

    start = pd.Timestamp(asof) - pd.Timedelta(days=lookback_days)
    items = []
    for _, row in df.iterrows():
        ts = _parse_dt(row[date_col])
        if ts is None or ts < start:
            continue
        content = str(row[content_col]) if content_col else ""
        if len(content) > 180:
            content = content[:180] + "…"
        items.append({
            "date": ts.strftime("%Y-%m-%d %H:%M:%S"),
            "title": str(row[title_col]),
            "summary": content,
            "media": str(row[src_col]) if src_col else "",
            "source": "eastmoney",
        })
    items = _filter_asof(items, asof)
    items.sort(key=lambda x: str(x.get("date", "")), reverse=True)
    return items[:limit]


def fetch_news(
    instrument: str,
    asof: str,
    vendors: list[str] | None = None,
    lookback_days: int = 7,
    limit: int = 15,
) -> list[dict[str, Any]]:
    vendors = vendors or DEFAULT_VENDORS["news"]
    for v in vendors:
        if v.lower() in {"eastmoney", "em"}:
            items = fetch_news_eastmoney(instrument, asof, lookback_days, limit)
            if items:
                return items
    return []


# --------------------------------------------------------------------------- #
# Fundamentals (baostock)
# --------------------------------------------------------------------------- #


def _baostock_latest_quarter(asof: str) -> tuple[int, int]:
    """取 asof 前最近已结束报告期（粗略：不假设当季已披露）。"""
    ts = pd.Timestamp(asof)
    # 用上一完整季度，降低未披露季报前视风险
    q = ((ts.month - 1) // 3)  # 0..3；0 表示仍在 Q1，应取上年 Q4
    if q == 0:
        return ts.year - 1, 4
    return ts.year, q


def fetch_fundamentals_baostock(instrument: str, asof: str) -> dict[str, Any]:
    """baostock 盈利+成长指标（最近已结束季度）。"""
    import baostock as bs

    code = to_baostock_code(instrument)
    year, quarter = _baostock_latest_quarter(asof)
    lg = bs.login()
    if lg.error_code != "0":
        return {"source": "baostock", "error": lg.error_msg}

    out: dict[str, Any] = {
        "source": "baostock",
        "code": code,
        "asof": asof,
        "year": year,
        "quarter": quarter,
    }
    try:
        for label, fn in (
            ("profit", bs.query_profit_data),
            ("growth", bs.query_growth_data),
            ("operation", bs.query_operation_data),
            ("balance", bs.query_balance_data),
        ):
            rs = fn(code=code, year=year, quarter=quarter)
            if rs.error_code != "0":
                out[f"{label}_error"] = rs.error_msg
                continue
            rows = []
            while rs.error_code == "0" and rs.next():
                rows.append(dict(zip(rs.fields, rs.get_row_data())))
            if not rows:
                continue
            row = rows[0]
            # 公告日若晚于 asof，丢弃防前视
            pub = _parse_dt(row.get("pubDate"))
            if pub is not None and pub > pd.Timestamp(asof) + pd.Timedelta(days=1):
                out[f"{label}_skipped"] = f"pubDate {pub.date()} > asof"
                continue
            out[label] = {k: row.get(k) for k in row if k not in {"code"}}
    finally:
        bs.logout()

    # 若当季空，尝试再往前一季
    if "profit" not in out and "growth" not in out:
        y, q = year, quarter - 1
        if q <= 0:
            y, q = year - 1, 4
        lg = bs.login()
        if lg.error_code == "0":
            try:
                rs = bs.query_profit_data(code=code, year=y, quarter=q)
                rows = []
                while rs.error_code == "0" and rs.next():
                    rows.append(dict(zip(rs.fields, rs.get_row_data())))
                if rows:
                    row = rows[0]
                    pub = _parse_dt(row.get("pubDate"))
                    if pub is None or pub <= pd.Timestamp(asof) + pd.Timedelta(days=1):
                        out["year"], out["quarter"] = y, q
                        out["profit"] = {k: row.get(k) for k in row if k not in {"code"}}
            finally:
                bs.logout()
    return out


def fetch_fundamentals(
    instrument: str,
    asof: str,
    vendors: list[str] | None = None,
) -> dict[str, Any]:
    vendors = vendors or DEFAULT_VENDORS["fundamentals"]
    for v in vendors:
        if v.lower() == "baostock":
            try:
                return fetch_fundamentals_baostock(instrument, asof)
            except Exception as exc:  # noqa: BLE001
                logger.info("baostock 基本面失败 %s: %s", instrument, exc)
                return {"source": "baostock", "error": str(exc)}
    return {"source": "none", "error": "no vendor"}


# --------------------------------------------------------------------------- #
# Bundle for one instrument
# --------------------------------------------------------------------------- #


def fetch_research_bundle(
    instrument: str,
    asof: str,
    vendors: dict[str, list[str]] | None = None,
    lookback: dict[str, int] | None = None,
    limits: dict[str, int] | None = None,
) -> dict[str, Any]:
    """拉取单票公告+新闻+基本面（舆情默认空）。"""
    vendors = {**DEFAULT_VENDORS, **(vendors or {})}
    lookback = lookback or {}
    limits = limits or {}
    t0 = time.time()

    anns = fetch_announcements(
        instrument,
        asof,
        vendors=vendors.get("announcements"),
        lookback_days=int(lookback.get("announcements", 30)),
        limit=int(limits.get("announcements", 10)),
    )
    news = fetch_news(
        instrument,
        asof,
        vendors=vendors.get("news"),
        lookback_days=int(lookback.get("news", 7)),
        limit=int(limits.get("news", 15)),
    )
    fund = fetch_fundamentals(
        instrument,
        asof,
        vendors=vendors.get("fundamentals"),
    )
    return {
        "instrument": instrument,
        "asof": asof,
        "announcements": anns,
        "news": news,
        "fundamentals": fund,
        "sentiment": [],  # 首期关闭
        "meta": {
            "vendors": vendors,
            "elapsed_sec": round(time.time() - t0, 3),
            "n_announcements": len(anns),
            "n_news": len(news),
            "has_fundamentals": bool(fund.get("profit") or fund.get("growth")),
        },
    }


def format_bundle_for_brief(bundle: dict[str, Any]) -> str:
    """把三源数据格式化为中文简报段落。"""
    lines: list[str] = []
    fund = bundle.get("fundamentals") or {}
    if fund.get("error") and not fund.get("profit"):
        lines.append(f"基本面: 获取失败（{fund.get('error')}）")
    elif fund.get("profit") or fund.get("growth"):
        lines.append(
            f"基本面(baostock {fund.get('year')}Q{fund.get('quarter')}):"
        )
        p = fund.get("profit") or {}
        g = fund.get("growth") or {}
        bits = []
        for k, label in (
            ("roeAvg", "ROE"),
            ("npMargin", "净利率"),
            ("gpMargin", "毛利率"),
            ("epsTTM", "EPS_TTM"),
            ("netProfit", "净利润"),
        ):
            if p.get(k) not in (None, ""):
                bits.append(f"{label}={p.get(k)}")
        for k, label in (("YOYNI", "净利同比"), ("YOYEPSBasic", "EPS同比"), ("YOYAsset", "资产同比")):
            if g.get(k) not in (None, ""):
                bits.append(f"{label}={g.get(k)}")
        if p.get("pubDate"):
            bits.append(f"公告日={p.get('pubDate')}")
        lines.append("  " + "；".join(bits) if bits else "  （无有效字段）")
    else:
        lines.append("基本面: 无数据")

    anns = bundle.get("announcements") or []
    if anns:
        src = anns[0].get("source", "")
        lines.append(f"近期公告({len(anns)}条, source={src}):")
        for a in anns[:8]:
            lines.append(
                f"  - [{a.get('date','')[:10]}] {a.get('title','')}"
                + (f" ({a.get('category')})" if a.get("category") else "")
            )
    else:
        lines.append("近期公告: 无（或源不可用）")

    news = bundle.get("news") or []
    if news:
        lines.append(f"近期新闻({len(news)}条, source=eastmoney):")
        for n in news[:8]:
            lines.append(f"  - [{n.get('date','')[:10]}] {n.get('title','')}")
            if n.get("summary"):
                lines.append(f"    摘要: {n['summary']}")
    else:
        lines.append("近期新闻: 无")

    lines.append("舆情/社交: 本阶段关闭（零Key方案首期仅公告+新闻+基本面）")
    return "\n".join(lines)
