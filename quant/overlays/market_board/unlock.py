"""解禁雷区：东财全市场未来解禁明细（一次拉取），池内过滤。

数据源：akshare `stock_restricted_release_detail_em(start, end)`——
东财数据中心全市场解禁预告（含限售股类型/解禁市值/占解禁前流通市值比例）。
（注意：`stock_restricted_release_queue_em` 个股接口仅含历史已解禁记录，
  无未来预告，勿用于雷区。）

标注口径：未来 UNLOCK_WINDOW_DAYS 日内、占流通市值比 ≥ ALERT_PCT% 记雷区，
≥ HIGH_RISK_PCT% 记高危。本地缓存当天有效（盘后跑一次即可）。
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

QUANT = Path(__file__).resolve().parents[2]
CACHE_PATH = QUANT / "data" / "overlays" / "market_board" / "unlock_cache.json"
TZ = ZoneInfo("Asia/Shanghai")

UNLOCK_WINDOW_DAYS = 30
ALERT_PCT = 1.0            # 占解禁前流通市值 ≥1%
HIGH_RISK_PCT = 10.0


def _load_cache() -> dict[str, Any]:
    if not CACHE_PATH.exists():
        return {"generated": None, "items": {}}
    try:
        return json.loads(CACHE_PATH.read_text())
    except json.JSONDecodeError:
        return {"generated": None, "items": {}}


def _save_cache(cache: dict[str, Any]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=1) + "\n")


def _to_instrument(code: str) -> str | None:
    code = str(code).zfill(6)
    if code.startswith(("60", "68", "90")):
        return "SH" + code
    if code.startswith(("00", "30", "20")):
        return "SZ" + code
    if code.startswith(("4", "8")):
        return "BJ" + code
    return None


def refresh_all(force: bool = False) -> dict[str, list[dict[str, Any]]]:
    """拉全市场未来 N 日解禁明细 → 按 instrument 建索引缓存。当天缓存有效。"""
    cache = _load_cache()
    today = date.today().isoformat()
    if not force and str(cache.get("generated") or "")[:10] == today and cache.get("items"):
        return cache["items"]

    import akshare as ak
    start = date.today().strftime("%Y%m%d")
    end = (date.today() + timedelta(days=UNLOCK_WINDOW_DAYS)).strftime("%Y%m%d")
    df = ak.stock_restricted_release_detail_em(start_date=start, end_date=end)
    items: dict[str, list[dict[str, Any]]] = {}
    if df is not None and not df.empty:
        today_d = date.today()
        for _, r in df.iterrows():
            inst = _to_instrument(str(r.get("股票代码", "")))
            if not inst:
                continue
            d_raw = str(r.get("解禁时间", ""))[:10]
            try:
                d = date.fromisoformat(d_raw)
            except ValueError:
                continue
            ratio = r.get("占解禁前流通市值比例")
            try:
                ratio_pct = round(float(ratio) * 100, 2)   # 接口为小数 → %
            except (TypeError, ValueError):
                continue
            mv = r.get("实际解禁市值")
            try:
                mv_yi = round(float(mv) / 1e8, 2)
            except (TypeError, ValueError):
                mv_yi = None
            items.setdefault(inst, []).append({
                "date": d.isoformat(),
                "days_left": (d - today_d).days,
                "ratio_pct": ratio_pct,
                "market_value_yi": mv_yi,
                "share_type": str(r.get("限售股类型", "")),
                "high_risk": ratio_pct >= HIGH_RISK_PCT,
                "alert": ratio_pct >= ALERT_PCT,
            })
    for v in items.values():
        v.sort(key=lambda x: x["date"])
    cache = {"generated": datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"),
             "window_days": UNLOCK_WINDOW_DAYS, "items": items}
    _save_cache(cache)
    log.info("解禁缓存刷新: 全市场未来%d日 → %d 只有解禁", UNLOCK_WINDOW_DAYS, len(items))
    return items


def refresh_for(instruments: list[str], force: bool = False) -> dict[str, list[dict]]:
    """兼容旧签名：全量刷新后按列表过滤返回。"""
    items = refresh_all(force=force)
    return {i: items.get(i, []) for i in instruments}


def alerts_for(instruments: list[str]) -> dict[str, dict[str, Any]]:
    """读缓存，返回 {instrument: 最近的雷区事件}（仅 alert=True）。"""
    items = _load_cache().get("items") or {}
    out: dict[str, dict[str, Any]] = {}
    for inst in instruments:
        events = items.get(inst) or []
        if isinstance(events, dict):          # 旧缓存格式兼容（{fetched, events}）
            events = events.get("events") or []
        alerts = [e for e in events if isinstance(e, dict) and e.get("alert")]
        if alerts:
            nearest = alerts[0]
            out[inst] = {
                "date": nearest["date"], "days_left": nearest["days_left"],
                "ratio_pct": nearest["ratio_pct"], "high_risk": nearest["high_risk"],
                "n_events": len(alerts),
                "market_value_yi": nearest.get("market_value_yi"),
                "share_type": nearest.get("share_type"),
            }
    return out


def all_alerts() -> list[dict[str, Any]]:
    """全缓存雷区清单（看板展示用），按 days_left 升序。"""
    items = _load_cache().get("items") or {}
    out = []
    for inst, events in items.items():
        for e in events:
            if e.get("alert"):
                out.append({"instrument": inst, **e})
    out.sort(key=lambda x: (x["days_left"], -(x["ratio_pct"] or 0)))
    return out
