"""资讯快讯流：复用 sentiment_memory 采集产物，按条目结构分两条流。

raw 条目结构（实测）：
  - 全局电报（sina / eastmoney_global / policy_*，无 instrument 字段）
    → 快讯主流；池内命中用「代码带边界 + 标题名称」标注
  - 个股新闻/公告（eastmoney / ann_*，自带 instrument 字段）
    → 「池内动态」流：instrument 直接关联，不做文本匹配

无额外 LLM 调用、无外网依赖；raw 为空时 available=False 并提示采集入口。
"""

from __future__ import annotations

import logging
import re
from typing import Any

log = logging.getLogger(__name__)

GLOBAL_SOURCES = ("sina", "eastmoney_global", "policy_", "cls")


def load_feed(pool: list[dict], lookback_days: int = 3, limit: int = 80,
              pool_news_limit: int = 40) -> dict[str, Any]:
    """返回 {global_feed, pool_feed, ...} 双流。"""
    out: dict[str, Any] = {"available": False, "global_feed": [], "pool_feed": []}
    try:
        from overlays.sentiment_memory import store as sm_store
        raw = sm_store.load_raw(lookback_days=lookback_days)
    except Exception as e:  # noqa: BLE001
        out["error"] = f"舆情 raw 读取失败: {e}"
        return out
    if not raw:
        out["error"] = "舆情 raw 为空（evening 管道会自动采集；或在「舆情跟踪」页手动跑）"
        return out

    pool_map = {p["instrument"]: p for p in pool}
    code2meta = {p["instrument"][2:]: p for p in pool}
    code_pats = {c: re.compile(rf"(?<!\d){c}(?!\d)") for c in code2meta}
    name2meta = {p["name"]: p for p in pool if p.get("name") and len(p["name"]) >= 3}

    global_feed: list[dict[str, Any]] = []
    pool_feed: list[dict[str, Any]] = []

    for it in raw:
        title = str(it.get("title") or "")
        published = it.get("published")
        source = str(it.get("source") or "")
        inst = (it.get("instrument") or "").upper() or None

        base = {
            "published": published, "source": source, "kind": it.get("kind"),
            "title": title, "url": it.get("url"),
            "is_policy": bool(it.get("kind") == "政策宏观" or source.startswith("policy_")),
        }

        if inst:
            # 个股新闻/公告：instrument 直接关联
            meta = pool_map.get(inst)
            if meta:
                pool_feed.append({**base, "instrument": inst,
                                  "name": meta.get("name", ""),
                                  "industry": meta.get("industry", "")})
            continue

        # 全局电报：池内命中标注
        hits: list[dict[str, str]] = []
        text = title + " " + str(it.get("summary") or it.get("content") or "")[:200]
        for code, meta in code2meta.items():
            if code_pats[code].search(text):
                hits.append({"instrument": meta["instrument"],
                             "name": meta.get("name", ""), "via": "code"})
        for name, meta in name2meta.items():
            if name in title and not any(h["instrument"] == meta["instrument"] for h in hits):
                hits.append({"instrument": meta["instrument"], "name": name, "via": "name"})
        global_feed.append({**base, "pool_hits": hits[:5],
                            "hits_code": any(h.get("via") == "code" for h in hits)})

    # 全局流：时间倒序（稳定）→ 命中/政策分组提升
    global_feed.sort(key=lambda x: str(x.get("published") or ""), reverse=True)
    global_feed.sort(key=lambda x: 0 if x.get("pool_hits") else (1 if x.get("is_policy") else 2))
    # 池内动态：时间倒序
    pool_feed.sort(key=lambda x: str(x.get("published") or ""), reverse=True)

    out.update({
        "available": True,
        "global_feed": global_feed[:limit],
        "pool_feed": pool_feed[:pool_news_limit],
        "stats": {
            "raw_total": len(raw),
            "global_n": len(global_feed),
            "pool_news_n": len(pool_feed),
            "global_hit_n": sum(1 for x in global_feed if x.get("pool_hits")),
        },
        "lookback_days": lookback_days,
    })
    return out
