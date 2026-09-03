"""资金流向榜：东财 push2 个股资金流排名（主力净流入），池内过滤。

手写 clist（akshare 对应封装在本机被断连）。拉全市场主力净流入
降序前 N 页 + 净流出前 1 页，与池内取交集。盘后/手动刷新均可。
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
from typing import Any

log = logging.getLogger(__name__)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 quant-board"
HOSTS = ("https://push2delay.eastmoney.com",   # 延时节点：主节点 502 时最稳
         "https://push2.eastmoney.com",
         "https://push2his.eastmoney.com")
FIELDS = "f12,f14,f2,f3,f62,f184,f66,f69,f72,f75,f84,f87"   # 主力/超大/大/小单
PAGES_IN = 4               # 净流入抓前 4 页（约 400 只，提高池内命中）
PAGES_OUT = 2              # 净流出抓前 2 页


def _fetch_page(pn: int, po: int, pz: int = 100) -> list[dict]:
    path = (f"/api/qt/clist/get?pn={pn}&pz={pz}&po={po}&np=1&fltt=2&invt=2"
            f"&fid=f62&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23&fields={FIELDS}")
    for host in HOSTS:
        req = urllib.request.Request(host + path, headers={
            "User-Agent": UA, "Referer": "https://data.eastmoney.com/zjlx/detail.html",
            "Connection": "close"})
        for attempt in range(2):
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    j = json.load(r)
                return (j.get("data") or {}).get("diff") or []
            except Exception as e:  # noqa: BLE001
                log.debug("资金流拉取失败 host=%s p=%s attempt=%s err=%s",
                          host, pn, attempt + 1, e)
                time.sleep(0.4)
    return []


def _to_inst(code: str) -> str | None:
    if code.startswith(("60", "68", "90")):
        return "SH" + code
    if code.startswith(("00", "30", "20")):
        return "SZ" + code
    if code.startswith(("4", "8")):
        return "BJ" + code
    return None


def compute_fundflow(pool: list[dict], top_in: int = 20, top_out: int = 10) -> dict[str, Any]:
    """返回 {inflow: [...], outflow: [...]}，仅池内。"""
    pool_codes = {p["instrument"][2:]: p for p in pool}
    out: dict[str, Any] = {"available": False, "inflow": [], "outflow": []}

    def collect(pages: int, po: int) -> dict[str, dict]:
        found: dict[str, dict] = {}
        for pn in range(1, pages + 1):
            rows = _fetch_page(pn, po)
            if not rows:
                break
            for r in rows:
                code = str(r.get("f12") or "")
                if code not in pool_codes:
                    continue
                found[code] = {
                    "instrument": _to_inst(code),
                    "name": r.get("f14"),
                    "industry": pool_codes[code].get("industry", "未知"),
                    "price": r.get("f2"), "chg_pct": r.get("f3"),
                    "main_net_yi": round((r.get("f62") or 0) / 1e8, 2),
                    "main_net_pct": r.get("f184"),
                    "super_net_yi": round((r.get("f66") or 0) / 1e8, 2),
                    "big_net_yi": round((r.get("f72") or 0) / 1e8, 2),
                    "small_net_yi": round((r.get("f84") or 0) / 1e8, 2),
                }
            time.sleep(0.15)
        return found

    inflow = collect(PAGES_IN, po=1)     # po=1 降序（净流入最大）
    outflow = collect(PAGES_OUT, po=0)   # po=0 升序（净流出最大）
    if not inflow and not outflow:
        return out

    out["available"] = True
    out["inflow"] = sorted(inflow.values(), key=lambda x: -x["main_net_yi"])[:top_in]
    out["outflow"] = sorted(outflow.values(), key=lambda x: x["main_net_yi"])[:top_out]
    return out
