"""个股/指数行情数据接入。

数据源优先级：
  1) 东方财富 push2his K线接口（免费、无 key、含当日近实时真实价）；
  2) 连不通时回退本地 qlib 日线（$close/$factor 还原真实价，仅到最近 release）。

只读、轻量：标准库 urllib + 进程内短缓存，避免频繁外呼。
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import time
import urllib.request
from pathlib import Path

QUANT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(QUANT / "ops"))
import common as C  # noqa: E402

EM_KLINE = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
            "?secid={secid}&fields1=f1,f2,f3,f4,f5,f6"
            "&fields2=f51,f52,f53,f54,f55,f56,f57"
            "&klt={klt}&fqt={fqt}&end={end}&lmt={lmt}")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) quant-dashboard"
_EM_RETRY = 2

# 常用指数（“大盘”）
INDICES = [
    {"instrument": "SH000001", "name": "上证指数"},
    {"instrument": "SH000905", "name": "中证500(基准)"},
    {"instrument": "SZ399006", "name": "创业板指"},
]

_cache: dict[str, tuple[float, dict]] = {}
_TTL = 60.0  # 秒


def secid(instrument: str) -> str:
    """SH600000→1.600000；SZ000001/BJ→0.xxxxxx；指数同前缀规则。"""
    mkt = instrument[:2].upper()
    code = instrument[2:]
    market = "1" if mkt == "SH" else "0"  # SZ/BJ 均为 0
    return f"{market}.{code}"


def _fetch_em(instrument: str, klt: int, lmt: int, fqt: int) -> dict | None:
    end = dt.date.today().strftime("%Y%m%d")
    url = EM_KLINE.format(secid=secid(instrument), klt=klt, lmt=lmt, fqt=fqt, end=end)
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Referer": "https://quote.eastmoney.com/",
        "Connection": "close", "Accept": "application/json"})
    j = None
    for attempt in range(_EM_RETRY):  # 东财偶发断连，短重试显著提升命中率
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                j = json.load(r)
            break
        except Exception:
            time.sleep(0.4 * (attempt + 1))
    if j is None:
        return None
    data = j.get("data")
    if not data or not data.get("klines"):
        return None
    rows = []
    for ln in data["klines"]:
        p = ln.split(",")
        # f51..f57 = date,open,close,high,low,volume,amount
        rows.append({"date": p[0], "open": float(p[1]), "close": float(p[2]),
                     "high": float(p[3]), "low": float(p[4]),
                     "volume": float(p[5]), "amount": float(p[6])})
    return {"name": data.get("name", instrument), "source": "eastmoney", "klines": rows}


def _fetch_qlib(instrument: str, lmt: int) -> dict | None:
    try:
        C.init_qlib()
        from qlib.data import D
        cal = C.calendar()
        end = cal[-1]
        start = cal[max(0, len(cal) - lmt - 1)]
        df = D.features([instrument],
                        ["$open/$factor", "$close/$factor", "$high/$factor",
                         "$low/$factor", "$volume"],
                        start_time=str(start), end_time=str(end))
    except Exception:
        return None
    if df is None or df.empty:
        return None
    df = df.droplevel("instrument")
    rows = []
    for ts, r in df.iterrows():
        rows.append({"date": str(ts)[:10],
                     "open": round(float(r.iloc[0]), 3), "close": round(float(r.iloc[1]), 3),
                     "high": round(float(r.iloc[2]), 3), "low": round(float(r.iloc[3]), 3),
                     "volume": float(r.iloc[4]), "amount": 0.0})
    return {"name": instrument, "source": "qlib(本地,日线)", "klines": rows}


def quote(instrument: str, klt: int = 101, lmt: int = 120, fqt: int = 1) -> dict:
    key = f"{instrument}:{klt}:{lmt}:{fqt}"
    now = time.time()
    if key in _cache and now - _cache[key][0] < _TTL:
        return _cache[key][1]

    res = _fetch_em(instrument, klt, lmt, fqt) or _fetch_qlib(instrument, lmt)
    if not res:
        out = {"instrument": instrument, "ok": False, "klines": []}
        _cache[key] = (now, out)
        return out

    kl = res["klines"]
    last = kl[-1]
    prev = kl[-2] if len(kl) > 1 else last
    chg = (last["close"] / prev["close"] - 1) * 100 if prev["close"] else 0.0
    out = {
        "instrument": instrument, "ok": True, "name": res["name"], "source": res["source"],
        "klt": klt, "latest": round(last["close"], 3), "date": last["date"],
        "open": round(last["open"], 3), "high": round(last["high"], 3),
        "low": round(last["low"], 3), "chg_pct": round(chg, 2),
        "klines": kl,
    }
    _cache[key] = (now, out)
    return out


def indices() -> list[dict]:
    out = []
    for ix in INDICES:
        q = quote(ix["instrument"], klt=101, lmt=60, fqt=0)
        if q.get("ok"):
            q["display_name"] = ix["name"]
            out.append(q)
    return out
