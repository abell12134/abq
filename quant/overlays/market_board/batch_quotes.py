"""腾讯批量实时报价（qt.gtimg.cn）：池内 500 只分 ~9 批拉取，约 3~5 秒。

关键字段（v_xxx="~" 分隔，GBK 编码）：
  1 名称 | 3 现价 | 4 昨收 | 5 今开 | 6 成交量(手) | 9/10 买一价/量(手)
  19/20 卖一价/量 | 30 时间 | 31 涨跌额 | 32 涨跌幅% | 33/34 最高/最低
  37 成交额(万) | 38 换手率% | 43 振幅% | 44 流通市值(亿) | 45 总市值(亿)
  47 涨停价 | 48 跌停价 | 49 量比 | 51 均价

封单口径：现价=涨停价（卖一价为 0 或无卖盘）时，买一价×买一量×100 即封单额。
仅盘后/手动刷新调用；进程内 60s 缓存，失败 fail-open 返回已成功批次。
"""

from __future__ import annotations

import logging
import time
import urllib.request
from typing import Any

log = logging.getLogger(__name__)

BATCH_SIZE = 60
_TIMEOUT = 10.0
_RETRY = 2
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 quant-board"
REFERER = "https://gu.qq.com/"

_cache: dict[str, tuple[float, dict[str, dict]]] = {}
_CACHE_TTL = 60.0


def _tx_symbol(instrument: str) -> str:
    return instrument[:2].lower() + instrument[2:]


def _f(v: str, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _parse_line(symbol: str, payload: str) -> dict[str, Any] | None:
    f = payload.split("~")
    if len(f) < 50:
        return None
    price = _f(f[3])
    prev_close = _f(f[4])
    if price <= 0 or prev_close <= 0:
        return None
    limit_up_px, limit_down_px = _f(f[47]), _f(f[48])
    bid1_px, bid1_vol = _f(f[9]), _f(f[10])          # 买一价 / 买一量(手)
    ask1_px = _f(f[19])
    chg_pct = _f(f[32])
    float_mv = _f(f[44])                              # 流通市值（亿）

    at_limit_up = limit_up_px > 0 and abs(price - limit_up_px) < 0.001
    at_limit_down = limit_down_px > 0 and abs(price - limit_down_px) < 0.001
    touched = limit_up_px > 0 and abs(_f(f[33]) - limit_up_px) < 0.001

    # 封单额（元）：涨停且卖一空（无卖盘）时买一量即封单
    sealed_amt = 0.0
    if at_limit_up and ask1_px <= 0 and bid1_px > 0:
        sealed_amt = bid1_px * bid1_vol * 100

    return {
        "symbol": symbol,
        "name": f[1],
        "price": price,
        "prev_close": prev_close,
        "open": _f(f[5]),
        "high": _f(f[33]),
        "low": _f(f[34]),
        "chg_pct": chg_pct,
        "ret": chg_pct / 100,
        "volume_hand": _f(f[6]),
        "amount_wan": _f(f[37]),
        "turnover_pct": _f(f[38]),
        "amplitude_pct": _f(f[43]),
        "float_mv_yi": float_mv,
        "total_mv_yi": _f(f[45]),
        "limit_up_px": limit_up_px,
        "limit_down_px": limit_down_px,
        "vol_ratio": _f(f[49]),
        "avg_price": _f(f[51]),
        "at_limit_up": at_limit_up,
        "at_limit_down": at_limit_down,
        "touched_limit": touched,
        "broken": touched and not at_limit_up,       # 盘中炸板状态
        "sealed_amt": sealed_amt,                    # 封单额（元）
        "sealed_ratio": round(sealed_amt / (float_mv * 1e8), 6) if float_mv > 0 and sealed_amt > 0 else 0.0,
        "quote_time": f[30] if len(f) > 30 else "",
    }


def _fetch_batch(symbols: list[str]) -> dict[str, dict]:
    url = "https://qt.gtimg.cn/q=" + ",".join(symbols)
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Referer": REFERER, "Connection": "close"})
    for attempt in range(_RETRY):
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
                text = r.read().decode("gbk", errors="replace")
            break
        except Exception as e:  # noqa: BLE001
            log.debug("腾讯批量报价失败 attempt=%s err=%s", attempt + 1, e)
            time.sleep(0.4 * (attempt + 1))
    else:
        return {}

    out: dict[str, dict] = {}
    for line in text.split(";"):
        line = line.strip()
        if not line.startswith("v_") or "=" not in line:
            continue
        var, _, payload = line.partition("=")
        symbol = var[2:]
        payload = payload.strip().strip('"')
        if not payload:
            continue
        row = _parse_line(symbol, payload)
        if row:
            out[symbol] = row
    return out


def fetch_pool_quotes(instruments: list[str], use_cache: bool = True) -> dict[str, dict]:
    """批量拉取池内实时报价。返回 {instrument(大写): row}；失败批次跳过（fail-open）。"""
    key = ",".join(sorted(instruments)[:8]) + f"#{len(instruments)}"
    now = time.time()
    if use_cache and key in _cache and now - _cache[key][0] < _CACHE_TTL:
        return _cache[key][1]

    symbols = [_tx_symbol(i) for i in instruments]
    sym2inst = dict(zip(symbols, instruments))
    result: dict[str, dict] = {}
    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i:i + BATCH_SIZE]
        rows = _fetch_batch(batch)
        for sym, row in rows.items():
            inst = sym2inst.get(sym)
            if inst:
                row["instrument"] = inst
                result[inst] = row
        if i + BATCH_SIZE < len(symbols):
            time.sleep(0.15)          # 温和限速，避免触发频控

    log.info("批量报价完成: %d/%d 只", len(result), len(instruments))
    if result:
        _cache[key] = (now, result)
    return result
