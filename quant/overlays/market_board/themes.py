"""题材热度：东财 push2 板块行情（概念+行业）+ 板块成分 × 池内涨停交集。

数据链路：
  概念/行业板块涨幅榜（手写 push2 clist，akshare 封装对 push2 的调用在本机被断连）
  → TOP N 板块拉成分股 → 与池内涨停/强势股交集 → 每板块标注"池内命中数"

同时产出 sector_pulse 兼容字段（industries_top / rising 等），修复看板
「市场热度」生成端缺失问题（research/sector_pulse.py 不在仓库中）。

盘后调用，全程单外网依赖（东财），失败 fail-open 返回 available=False。
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

TOP_BOARDS = 25          # 拉涨幅榜前 N 个板块做成分匹配
CONS_LIMIT = 300         # 每板块成分股上限


def _em_clist(fs: str, fields: str, fid: str = "f3", pz: int = 50,
              referer: str = "https://quote.eastmoney.com/") -> list[dict]:
    path = (f"/api/qt/clist/get?pn=1&pz={pz}&po=1&np=1&fltt=2&invt=2"
            f"&fid={fid}&fs={fs}&fields={fields}")
    for host in HOSTS:
        url = host + path
        req = urllib.request.Request(url, headers={
            "User-Agent": UA, "Referer": referer, "Connection": "close"})
        for attempt in range(2):
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    j = json.load(r)
                rows = (j.get("data") or {}).get("diff") or []
                if rows:
                    return rows
            except Exception as e:  # noqa: BLE001
                log.debug("东财 clist 失败 host=%s attempt=%s err=%s", host, attempt + 1, e)
                time.sleep(0.4)
    return []


def board_rank(board_type: str = "concept", limit: int = 40) -> list[dict[str, Any]]:
    """板块涨幅榜：concept=m:90+t:2，industry=m:90+t:1。"""
    fs = "m:90+t:2" if board_type == "concept" else "m:90+t:1"
    rows = _em_clist(fs, "f12,f14,f3,f8,f62,f104,f105,f128,f136,f140", pz=limit)
    out = []
    for r in rows:
        out.append({
            "code": r.get("f12"), "name": r.get("f14"),
            "chg_pct": r.get("f3"), "turnover_pct": r.get("f8"),
            "main_net_inflow": r.get("f62"),
            "up_count": r.get("f104"), "down_count": r.get("f105"),
            "leader_code": r.get("f128"), "leader_chg": r.get("f136"),
        })
    return out


def board_cons(board_code: str, limit: int = CONS_LIMIT) -> list[dict[str, Any]]:
    """板块成分股（按涨幅降序）。"""
    rows = _em_clist(f"b:{board_code}", "f12,f14,f3", pz=limit)
    return [{"code": r.get("f12"), "name": r.get("f14"), "chg_pct": r.get("f3")}
            for r in rows]


def compute_themes(pool: list[dict], limit_up_codes: set[str],
                   strong_codes: set[str] | None = None) -> dict[str, Any]:
    """题材热度主入口。limit_up_codes: 池内当日涨停（6 位代码集合）。"""
    strong_codes = strong_codes or set()
    out: dict[str, Any] = {"available": False, "concepts": [], "industries": []}

    concepts = board_rank("concept", TOP_BOARDS)
    industries = board_rank("industry", 40)
    if not concepts and not industries:
        return out
    out["available"] = True

    # 概念榜 + 池内命中
    theme_rows = []
    for b in concepts[:TOP_BOARDS]:
        cons = board_cons(b["code"])
        time.sleep(0.1)
        cons_codes = {c["code"] for c in cons}
        hit_limit = sorted(limit_up_codes & cons_codes)
        hit_strong = sorted(strong_codes & cons_codes)
        theme_rows.append({
            **b,
            "n_cons": len(cons_codes),
            "pool_limit_hits": len(hit_limit),
            "pool_limit_list": hit_limit[:8],
            "pool_strong_hits": len(hit_strong),
            # 热度分：板块涨幅 + 上涨家数占比 + 池内涨停命中加成
            "heat": round((b.get("chg_pct") or 0) * 2
                          + min((b.get("up_count") or 0) / 10, 10)
                          + len(hit_limit) * 8, 1),
        })
    theme_rows.sort(key=lambda x: -x["heat"])
    out["concepts"] = theme_rows

    # 行业榜直接展示（池内行业分布在 run_board 已算，此处为全市场视角）
    out["industries"] = industries
    return out


def sector_pulse_compat(themes: dict[str, Any], pool_limit_ups: list[dict],
                        indices: list[dict] | None = None) -> dict[str, Any]:
    """兼容旧「市场热度」契约（/api/market/pulse 读取的字段）。"""
    return {
        "generated": None,
        "signal_day": None,
        "disclaimer": "由 market_board/themes.py 生成（sector_pulse 补全兼容）。"
                      "研究参考，不改订单，不构成投资建议。",
        "indices": indices or [],
        "industries_top": [
            {"name": b.get("name"), "chg_pct": b.get("chg_pct"),
             "up_count": b.get("up_count")}
            for b in (themes.get("industries") or [])[:12]
        ],
        "concepts_top": [
            {"name": t.get("name"), "chg_pct": t.get("chg_pct"), "heat": t.get("heat"),
             "pool_limit_hits": t.get("pool_limit_hits")}
            for t in (themes.get("concepts") or [])[:15]
        ],
        "policy_themes": [],
        "rising": pool_limit_ups[:10],
        "watch": [],
    }
