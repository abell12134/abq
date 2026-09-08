"""单票资金流向：东财 push2 实时 + 近 N 日日线资金流。

字段（stock/get f135–f148，单位元）：
  超大 f135 流入 / f136 流出 / f137 净
  大单 f138 流入 / f139 流出 / f140 净
  中单 f141 流入 / f142 流出 / f143 净
  小单 f144 流入 / f145 流出 / f146 净
  主力 f147 流入 / f148 流出；净 = f147 - f148；占比 f184
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
from typing import Any

log = logging.getLogger(__name__)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 quant-tracking"
HOSTS = (
    "https://push2delay.eastmoney.com",
    "https://push2.eastmoney.com",
    "https://push2his.eastmoney.com",
)
GET_FIELDS = "f135,f136,f137,f138,f139,f140,f141,f142,f143,f144,f145,f146,f147,f148,f184"
KLINE_FIELDS2 = "f51,f52,f53,f54,f55,f56"


def _secid(instrument: str) -> str:
    mkt = instrument[:2].upper()
    return f"{'1' if mkt == 'SH' else '0'}.{instrument[2:]}"


def _yi(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return round(float(v) / 1e8, 3)
    except (TypeError, ValueError):
        return None


def _fetch_json(path: str) -> dict | None:
    for host in HOSTS:
        req = urllib.request.Request(
            host + path,
            headers={"User-Agent": UA, "Referer": "https://data.eastmoney.com/zjlx/detail.html",
                     "Connection": "close"},
        )
        for attempt in range(2):
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    j = json.load(r)
                if j.get("rc") == 0 and j.get("data"):
                    return j
            except Exception as e:  # noqa: BLE001
                log.debug("fundflow %s attempt %s: %s", host, attempt + 1, e)
                time.sleep(0.25)
    return None


def _tier(data: dict, pin: str, pout: str, pnet: str) -> dict[str, float | None]:
    return {
        "in_yi": _yi(data.get(pin)),
        "out_yi": _yi(data.get(pout)),
        "net_yi": _yi(data.get(pnet)),
    }


def _parse_kline_row(line: str) -> dict[str, Any] | None:
    """日线 kline：date, 超大净, 主力净, 小单净, 中单净, 大单净（与实时 f137/f53/f146/f143/f140 对齐）。"""
    parts = str(line).split(",")
    if len(parts) < 6:
        return None
    try:
        super_net = float(parts[1])
        main_net = float(parts[2])
        big_net = float(parts[5])
    except (TypeError, ValueError):
        return None
    return {
        "date": parts[0],
        "main_net_yi": round(main_net / 1e8, 3),
        "super_net_yi": round(super_net / 1e8, 3),
        "big_net_yi": round(big_net / 1e8, 3),
        "large_net_yi": round((super_net + big_net) / 1e8, 3),
    }


def fetch_fundflow(instrument: str, *, history_days: int = 5) -> dict[str, Any]:
    """拉单票当日资金分解 + 近 history_days 日主力/大单趋势。"""
    inst = instrument.upper()
    sec = _secid(inst)
    out: dict[str, Any] = {"instrument": inst, "ok": False}

    j = _fetch_json(f"/api/qt/stock/get?secid={sec}&fields={GET_FIELDS}")
    if not j:
        out["message"] = "东财接口不可用"
        return out

    d = j["data"]
    main_in = float(d.get("f147") or 0)
    main_out = float(d.get("f148") or 0)
    super_t = _tier(d, "f135", "f136", "f137")
    big_t = _tier(d, "f138", "f139", "f140")
    large_in = (d.get("f135") or 0) + (d.get("f138") or 0)
    large_out = (d.get("f136") or 0) + (d.get("f139") or 0)
    large_net = (d.get("f137") or 0) + (d.get("f140") or 0)

    out.update({
        "ok": True,
        "main_in_yi": _yi(main_in),
        "main_out_yi": _yi(main_out),
        "main_net_yi": _yi(main_in - main_out),
        "main_net_pct": round(float(d.get("f184") or 0), 2) if d.get("f184") is not None else None,
        "super": super_t,
        "big": big_t,
        "large": {
            "in_yi": _yi(large_in),
            "out_yi": _yi(large_out),
            "net_yi": _yi(large_net),
        },
        "medium": _tier(d, "f141", "f142", "f143"),
        "small": _tier(d, "f144", "f145", "f146"),
    })

    if history_days > 0:
        path = (
            f"/api/qt/stock/fflow/kline/get?lmt={int(history_days)}&klt=101&secid={sec}"
            f"&fields1=f1,f2,f3,f7&fields2={KLINE_FIELDS2}"
        )
        jk = _fetch_json(path)
        hist: list[dict] = []
        if jk:
            for line in (jk.get("data") or {}).get("klines") or []:
                row = _parse_kline_row(line)
                if row:
                    hist.append(row)
        out["history"] = hist
        if hist:
            out["date"] = hist[-1]["date"]

    return out
