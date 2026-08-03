"""个股/指数行情数据接入。

数据源优先级：
  1) 腾讯 TXApi（gtimg fqkline / mkline，免费无 token，日/周/分钟 K）；
  2) 东方财富 push2his K线（含当日近实时）；
  3) 长历史请求失败时：qlib 历史 + 远端近期 K 线合并；
  4) 仍失败时：刷新 qlib 后纯本地日线（仅 EOD，可能缺当日）。

只读、轻量：标准库 urllib + 进程内短缓存（缓存腾讯/东财成功结果）。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

QUANT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(QUANT / "ops"))
import common as C  # noqa: E402

log = logging.getLogger(__name__)

EM_PATH = "/api/qt/stock/kline/get?secid={secid}&fields1=f1,f2,f3,f4,f5,f6" \
          "&fields2=f51,f52,f53,f54,f55,f56,f57&klt={klt}&fqt={fqt}&end={end}&lmt={lmt}"
EM_HOSTS = ("https://push2his.eastmoney.com", "http://push2his.eastmoney.com")
TX_FQ_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={param}"
TX_MK_URL = "https://ifzq.gtimg.cn/appstock/app/kline/mkline?param={param}"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 quant-dashboard"
_EM_RETRY = 4
_EM_TIMEOUT = 12.0
_TX_RETRY = 3
_TX_TIMEOUT = 10.0
_REMOTE_SOURCES = frozenset({"tencent", "eastmoney"})

INDICES = [
    {"instrument": "SH000001", "name": "上证指数"},
    {"instrument": "SH000985", "name": "中证全指(基准)"},
    {"instrument": "SH000905", "name": "中证500"},
    {"instrument": "SZ399006", "name": "创业板指"},
]

_cache: dict[str, tuple[float, dict]] = {}
_TTL = 60.0


def secid(instrument: str) -> str:
    mkt = instrument[:2].upper()
    code = instrument[2:]
    market = "1" if mkt == "SH" else "0"
    return f"{market}.{code}"


def _tx_symbol(instrument: str) -> str:
    """SH600000 -> sh600000"""
    return instrument[:2].lower() + instrument[2:]


def _tx_period(klt: int) -> str | None:
    """东财 klt → 腾讯 period；None 表示腾讯不支持、应走东财。"""
    return {101: "day", 102: "week", 15: "m15", 60: "m60"}.get(klt)


def _tx_adjust(fqt: int) -> str:
    return {0: "", 1: "qfq", 2: "hfq"}.get(fqt, "qfq")


def _tx_kline_key(period: str, adjust: str) -> str:
    if period.startswith("m"):
        return period
    if adjust:
        return f"{adjust}{period}"
    return period


def _tx_fmt_dt(raw: str) -> str:
    """202608031130 → 2026-08-03 11:30；日线保持 YYYY-MM-DD。"""
    if len(raw) == 10 and raw[4] == "-":
        return raw
    if len(raw) == 12 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]} {raw[8:10]}:{raw[10:12]}"
    return raw


def _http_json(url: str, referer: str, retries: int, timeout: float) -> dict | None:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Referer": referer,
        "Connection": "close",
        "Accept": "application/json",
    })
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            log.debug("HTTP JSON 失败 attempt=%s url=%s err=%s", attempt + 1, url[:96], e)
            time.sleep(0.3 * (attempt + 1))
    return None


def _em_request(url: str) -> dict | None:
    return _http_json(url, "https://quote.eastmoney.com/", _EM_RETRY, _EM_TIMEOUT)


def _tx_name(block: dict, symbol: str, instrument: str) -> str:
    qt = block.get("qt") or {}
    if isinstance(qt, dict):
        row = qt.get(symbol)
        if isinstance(row, list) and len(row) > 1 and row[1]:
            return str(row[1])
    return instrument


def _parse_tx_rows(rows: list) -> list[dict]:
    out = []
    for row in rows:
        if not row or len(row) < 6:
            continue
        out.append({
            "date": _tx_fmt_dt(str(row[0])),
            "open": float(row[1]),
            "close": float(row[2]),
            "high": float(row[3]),
            "low": float(row[4]),
            "volume": float(row[5]),
            "amount": float(row[6]) if len(row) > 6 and row[6] not in (None, "", {}) else 0.0,
        })
    return out


def _parse_tx_fqkline(j: dict, instrument: str, klt: int, fqt: int) -> dict | None:
    data = j.get("data") or {}
    symbol = _tx_symbol(instrument)
    block = data.get(symbol)
    if not block:
        return None
    period = _tx_period(klt)
    if not period or period.startswith("m"):
        return None
    key = _tx_kline_key(period, _tx_adjust(fqt))
    rows = block.get(key) or block.get(period) or block.get(f"qfq{period}")
    if not rows:
        return None
    klines = _parse_tx_rows(rows)
    if not klines:
        return None
    return {"name": _tx_name(block, symbol, instrument), "source": "tencent", "klines": klines}


def _parse_tx_mkline(j: dict, instrument: str, klt: int) -> dict | None:
    data = j.get("data") or {}
    symbol = _tx_symbol(instrument)
    block = data.get(symbol)
    if not block:
        return None
    period = _tx_period(klt)
    if not period or not period.startswith("m"):
        return None
    rows = block.get(period)
    if not rows:
        return None
    klines = _parse_tx_rows(rows)
    if not klines:
        return None
    return {"name": _tx_name(block, symbol, instrument), "source": "tencent", "klines": klines}


def _fetch_tx_once(instrument: str, klt: int, lmt: int, fqt: int) -> dict | None:
    period = _tx_period(klt)
    if not period:
        return None
    symbol = _tx_symbol(instrument)
    if period.startswith("m"):
        param = f"{symbol},{period},,{lmt}"
        url = TX_MK_URL.format(param=param)
        j = _http_json(url, "https://gu.qq.com/", _TX_RETRY, _TX_TIMEOUT)
        return _parse_tx_mkline(j, instrument, klt) if j else None
    adjust = _tx_adjust(fqt)
    param = f"{symbol},{period},,,{lmt},{adjust}"
    j = _http_json(TX_FQ_URL.format(param=param), "https://gu.qq.com/", _TX_RETRY, _TX_TIMEOUT)
    return _parse_tx_fqkline(j, instrument, klt, fqt) if j else None


def _fetch_tx(instrument: str, klt: int, lmt: int, fqt: int) -> dict | None:
    """腾讯 K 线：先全量，失败则缩小窗口；日线仍失败则 qlib 历史 + 腾讯近期合并。"""
    res = _fetch_tx_once(instrument, klt, lmt, fqt)
    if res:
        return res

    for smaller in (min(lmt, 60), min(lmt, 15), 5):
        if smaller >= lmt:
            continue
        res = _fetch_tx_once(instrument, klt, smaller, fqt)
        if res and len(res["klines"]) >= min(smaller, 5):
            log.info("%s 腾讯全量 lmt=%s 失败，降级 lmt=%s 成功", instrument, lmt, smaller)
            return res

    if klt != 101:
        return None

    recent = _fetch_tx_once(instrument, klt, 10, fqt)
    if not recent:
        return None

    C.reset_qlib()
    ql = _fetch_qlib(instrument, lmt)
    if ql and ql["klines"]:
        merged = _merge_klines(ql["klines"], recent["klines"])
        log.info("%s 腾讯全量失败，已用 qlib 历史 + 腾讯近期(%d根) 合并",
                 instrument, len(recent["klines"]))
        return {"name": recent["name"], "source": "tencent", "klines": merged[-lmt:]}

    log.info("%s 腾讯全量失败，仅返回近期 %d 根 K 线", instrument, len(recent["klines"]))
    return recent


def _parse_em_klines(j: dict, instrument: str) -> dict | None:
    data = j.get("data")
    if not data or not data.get("klines"):
        return None
    rows = []
    for ln in data["klines"]:
        p = ln.split(",")
        rows.append({"date": p[0], "open": float(p[1]), "close": float(p[2]),
                     "high": float(p[3]), "low": float(p[4]),
                     "volume": float(p[5]), "amount": float(p[6])})
    return {"name": data.get("name", instrument), "source": "eastmoney", "klines": rows}


def _fetch_em_once(instrument: str, klt: int, lmt: int, fqt: int) -> dict | None:
    end = dt.date.today().strftime("%Y%m%d")
    sid = secid(instrument)
    path = EM_PATH.format(secid=sid, klt=klt, lmt=lmt, fqt=fqt, end=end)
    for host in EM_HOSTS:
        j = _em_request(host + path)
        if j is None:
            continue
        res = _parse_em_klines(j, instrument)
        if res:
            return res
    return None


def _merge_klines(hist: list[dict], recent: list[dict]) -> list[dict]:
    """历史 + 近期合并，重叠日期以 recent（远端）为准。"""
    if not hist:
        return recent
    if not recent:
        return hist
    cut = recent[0]["date"]
    base = [k for k in hist if k["date"] < cut]
    seen = {k["date"] for k in base}
    for k in recent:
        if k["date"] in seen:
            base = [x for x in base if x["date"] != k["date"]]
        base.append(k)
        seen.add(k["date"])
    return sorted(base, key=lambda x: x["date"])


def _fetch_em(instrument: str, klt: int, lmt: int, fqt: int) -> dict | None:
    """东财 K 线：先全量，失败则缩小窗口，再失败则 qlib 历史 + 东财近期合并。"""
    res = _fetch_em_once(instrument, klt, lmt, fqt)
    if res:
        return res

    for smaller in (min(lmt, 60), min(lmt, 15), 5):
        if smaller >= lmt:
            continue
        res = _fetch_em_once(instrument, klt, smaller, fqt)
        if res and len(res["klines"]) >= min(smaller, 5):
            log.info("%s 东财全量 lmt=%s 失败，降级 lmt=%s 成功", instrument, lmt, smaller)
            return res

    recent = _fetch_em_once(instrument, klt, 10, fqt)
    if not recent:
        return None

    C.reset_qlib()
    ql = _fetch_qlib(instrument, lmt)
    if ql and ql["klines"]:
        merged = _merge_klines(ql["klines"], recent["klines"])
        log.info("%s 东财全量失败，已用 qlib 历史 + 东财近期(%d根) 合并", instrument, len(recent["klines"]))
        return {"name": recent["name"], "source": "eastmoney", "klines": merged[-lmt:]}

    log.info("%s 东财全量失败，仅返回近期 %d 根 K 线", instrument, len(recent["klines"]))
    return recent


def _fetch_qlib(instrument: str, lmt: int) -> dict | None:
    try:
        C.reset_qlib()
        from qlib.data import D
        cal = C.calendar()
        end = cal[-1]
        start = cal[max(0, len(cal) - lmt - 1)]
        df = D.features([instrument],
                        ["$open/$factor", "$close/$factor", "$high/$factor",
                         "$low/$factor", "$volume"],
                        start_time=str(start), end_time=str(end))
    except Exception as e:
        log.warning("qlib 行情读取失败 %s: %s", instrument, e)
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
    cached = _cache.get(key)
    if cached and now - cached[0] < _TTL and cached[1].get("source") in _REMOTE_SOURCES:
        return cached[1]

    res = (_fetch_tx(instrument, klt, lmt, fqt)
           or _fetch_em(instrument, klt, lmt, fqt)
           or _fetch_qlib(instrument, lmt))
    if not res:
        out = {"instrument": instrument, "ok": False, "klines": []}
        return out

    if res["source"] not in _REMOTE_SOURCES:
        log.warning("%s 行情回退至 %s（数据可能不是最新）", instrument, res["source"])

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
    if out["source"] in _REMOTE_SOURCES:
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
