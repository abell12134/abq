"""swing_hunter 候选池：三路汇流 + 硬伤过滤 + 规则预打分。

三路来源：
  1. 量化强势池：LGBM signals 当日 rank 前 SIGNAL_TOP_N；
  2. 事件催化池：近 EVENT_LOOKBACK_DAYS 日舆情库中带 instrument 的公告/新闻命中
     （业绩/中标/增持/回购等催化关键词加分）；
  3. 跟踪延伸池：tracker 活跃票 + 账户持仓/次日订单（复用 sentiment_memory.resolve_universe）。

硬伤过滤（规则，LLM 无权豁免）：
  停牌 / 当日涨停（买不进）/ ST（名称含 ST）/ 账户无权限板块（exclude_boards）。

规则分 rule_score ∈ [0,1]：lgbm 名次 + 动量 + 距 60 日高点 + 量比 + 事件 + 延伸，
仅用于决定「今天把哪 ≤MAX_LLM_CALLS 只交给 LLM 深析」，不是预测结论。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from .schema import (
    EVENT_LOOKBACK_DAYS,
    QUANT,
    SIGNAL_TOP_N,
)

sys.path.insert(0, str(QUANT))
sys.path.insert(0, str(QUANT / "ops"))

SIGNALS_DIR = QUANT / "data" / "signals"

# 催化关键词（标题命中即加分；与 CATALYST_TYPES 对应）
CATALYST_KEYWORDS = {
    "业绩超预期": ("预增", "业绩预增", "扭亏", "业绩快报", "超预期", "大幅增长"),
    "重大合同订单": ("中标", "重大合同", "签订", "订单", "框架协议"),
    "股东增持回购": ("增持", "回购", "举牌"),
    "政策行业利好": ("获批", "补贴", "纳入", "试点", "政策"),
}


def load_signals(day: str) -> pd.DataFrame:
    """读取当日信号（instrument,score,rank）；缺当日则回退 latest_pred。"""
    path = SIGNALS_DIR / f"{day}.csv"
    if not path.exists():
        path = SIGNALS_DIR / "latest_pred.csv"
    if not path.exists():
        return pd.DataFrame(columns=["instrument", "score", "rank"])
    df = pd.read_csv(path)
    df["instrument"] = df["instrument"].astype(str).str.upper()
    return df


def account_config(account: str | None) -> dict[str, Any]:
    if not account:
        return {}
    f = QUANT / "configs" / "accounts" / f"{account}.yaml"
    return yaml.safe_load(f.read_text()) if f.exists() else {}


def _board_excluded(inst: str, boards: list[str]) -> bool:
    prefixes = {
        "chinext": ("SZ300", "SZ301"),
        "star": ("SH688",),
        "bse": ("BJ", "SH8", "SH4"),
    }
    return any(inst.startswith(p) for b in boards
               for p in prefixes.get(str(b).lower(), ()))


def load_price_features(instruments: list[str], day: str) -> dict[str, dict[str, Any]]:
    """候选票的量化特征（截至 day，无前视）：动量/位置/量比/波动。"""
    if not instruments:
        return {}
    import common as C  # noqa: WPS433
    C.init_qlib()
    from qlib.data import D

    start = (pd.Timestamp(day) - pd.Timedelta(days=200)).strftime("%Y-%m-%d")
    fields = [
        "$close/$factor", "$high/$factor", "$low/$factor", "$volume", "$amount",
    ]
    df = D.features(instruments, fields, start_time=start, end_time=day)
    if df is None or df.empty:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for inst in instruments:
        try:
            sub = df.xs(inst, level="instrument").sort_index()
        except KeyError:
            continue
        px = sub["$close/$factor"].dropna()
        vol = sub["$volume"].dropna()
        if len(px) < 30:
            continue
        rets = px.pct_change().dropna()
        high60 = float(sub["$high/$factor"].dropna().tail(60).max())
        last = float(px.iloc[-1])
        feat = {
            "last_close": round(last, 4),
            "ret_5d": round(float(px.iloc[-1] / px.iloc[-6] - 1), 4) if len(px) >= 6 else None,
            "ret_20d": round(float(px.iloc[-1] / px.iloc[-21] - 1), 4) if len(px) >= 21 else None,
            "ret_60d": round(float(px.iloc[-1] / px.iloc[-61] - 1), 4) if len(px) >= 61 else None,
            "near_60d_high": round(last / high60 - 1, 4) if high60 else None,
            "vol_20d": round(float(rets.tail(20).std()), 4) if len(rets) >= 5 else None,
            "vol_ratio": round(float(vol.tail(5).mean() / (vol.tail(20).mean() + 1e-9)), 2)
            if len(vol) >= 20 else None,
        }
        out[inst] = feat
    return out


def recent_events(instruments: set[str], day: str | None = None) -> dict[str, list[dict[str, Any]]]:
    """近 EVENT_LOOKBACK_DAYS 日舆情库中带 instrument 的条目（含公告），按票分组。"""
    from overlays.sentiment_memory import store as sm_store  # noqa: WPS433

    raw = sm_store.load_raw(lookback_days=EVENT_LOOKBACK_DAYS)
    out: dict[str, list[dict[str, Any]]] = {i: [] for i in instruments}
    for it in raw:
        inst = str(it.get("instrument") or "").upper()
        if inst in out:
            out[inst].append(it)
    for inst in out:
        out[inst].sort(key=lambda x: str(x.get("published", "")), reverse=True)
    return out


def event_boost(items: list[dict[str, Any]]) -> tuple[float, list[str]]:
    """由近期条目算事件加分（0~0.45）与命中的催化类型（去重，≤3）。"""
    boost, hits = 0.0, []
    for it in items:
        title = str(it.get("title") or "")
        src = str(it.get("source") or "")
        kind = str(it.get("kind") or "")
        is_ann = src.startswith("ann_") or kind in {"公司公告", "财报公告"}
        for cat, kws in CATALYST_KEYWORDS.items():
            if any(k in title for k in kws):
                if cat not in hits:
                    hits.append(cat)
                boost += 0.2 if is_ann else 0.08
        else:
            if is_ann:
                boost += 0.05  # 有公告但未命中关键词，略加分（信息密度高）
    return min(boost, 0.45), hits[:3]


def rule_score(feat: dict[str, Any] | None, rank: float | None,
               evt_boost: float, is_extension: bool) -> float:
    """规则预打分（决定 LLM 深析顺位，非预测结论）。"""
    score = evt_boost + (0.10 if is_extension else 0.0)
    if rank is not None and not pd.isna(rank):
        score += 0.25 * max(0.0, 1.0 - float(rank) / (SIGNAL_TOP_N * 3))
    if feat:
        r20 = feat.get("ret_20d")
        if r20 is not None:
            score += 0.15 * min(max(r20, 0.0), 0.30) / 0.30
        nh = feat.get("near_60d_high")
        if nh is not None:
            score += 0.10 * min(max((nh + 0.10) / 0.10, 0.0), 1.0)
        vr = feat.get("vol_ratio")
        if vr is not None:
            score += 0.10 * min(max((vr - 0.5) / 2.5, 0.0), 1.0)
    return round(min(score, 1.0), 4)


def _sig_val(sig: pd.DataFrame, inst: str, col: str) -> float:
    if sig.empty or inst not in sig.index or col not in sig.columns:
        return float("nan")
    v = sig.loc[inst, col]
    if isinstance(v, pd.Series):
        v = v.iloc[0]
    try:
        return float(v)
    except (TypeError, ValueError):
        return float("nan")


def build_candidates(
    day: str,
    account: str | None = "live_manual_10k",
) -> dict[str, Any]:
    """返回 {candidates: [ {instrument, rank, score_lgbm, feat, events, rule_score,
    catalyst_hints, is_extension, filtered, filter_reason} ], market_notes: [...]}。

    filtered=True 的票保留在列表里供复盘展示，但不进入 LLM 深析。
    """
    import common as C  # noqa: WPS433
    from overlays.swing_hunter import store as sw_store  # noqa: WPS433

    sig = load_signals(day)
    sig = sig.set_index("instrument") if not sig.empty else sig

    # --- 路 1：量化强势池 ---
    pool: dict[str, dict[str, Any]] = {}
    if not sig.empty:
        top = sig.nsmallest(SIGNAL_TOP_N, "rank") if "rank" in sig.columns else sig.head(SIGNAL_TOP_N)
        for inst, row in top.iterrows():
            pool[str(inst)] = {
                "instrument": str(inst),
                "rank": float(row.get("rank", float("nan"))),
                "score_lgbm": float(row.get("score", float("nan"))),
                "from_signal": True,
                "is_extension": False,
            }

    # --- 路 3：跟踪延伸池（先并入，事件池需要全集） ---
    active = {r.instrument for r in sw_store.all_active_records()}
    held = set()
    if account:
        try:
            from overlays.sentiment_memory.run_memory import resolve_universe  # noqa: WPS433
            held = set(resolve_universe(account))
        except Exception:  # noqa: BLE001
            held = set()
    extension = active | held
    for inst in sorted(extension):
        if inst not in pool:
            pool[inst] = {
                "instrument": inst,
                "rank": _sig_val(sig, inst, "rank"),
                "score_lgbm": _sig_val(sig, inst, "score"),
                "from_signal": False,
                "is_extension": True,
            }
        else:
            pool[inst]["is_extension"] = True

    # --- 路 2：事件催化池 ---
    events = recent_events(set(pool.keys()))
    for inst, items in events.items():
        if items and inst not in pool:
            pool[inst] = {
                "instrument": inst,
                "rank": float("nan"),
                "score_lgbm": float("nan"),
                "from_signal": False,
                "is_extension": False,
            }

    # --- 量化特征 + 事件加分 + 规则分 ---
    feats = load_price_features(sorted(pool.keys()), day)
    cfg = account_config(account)
    exclude_boards = (cfg.get("execution", {}) or {}).get("exclude_boards", []) or []

    trade = pd.DataFrame()
    try:
        trade = C.trade_status(sorted(pool.keys()), day)
    except Exception:  # noqa: BLE001
        pass

    candidates: list[dict[str, Any]] = []
    for inst, info in pool.items():
        items = events.get(inst, [])
        boost, cat_hits = event_boost(items)
        feat = feats.get(inst)
        rs = rule_score(feat, info.get("rank"), boost, bool(info.get("is_extension")))
        filtered, reason = False, ""
        if not trade.empty and inst in trade.index:
            row = trade.loc[inst]
            if bool(row.get("suspended")):
                filtered, reason = True, "停牌"
            elif bool(row.get("limit_up")):
                filtered, reason = True, "涨停不可买"
        if _board_excluded(inst, exclude_boards):
            filtered, reason = True, "账户无权限板块"
        candidates.append({
            **info,
            "feat": feat or {},
            "events": [
                {"source": it.get("source"), "kind": it.get("kind"),
                 "published": it.get("published"), "title": it.get("title"),
                 "url": it.get("url")}
                for it in items[:10]
            ],
            "catalyst_hints": cat_hits,
            "rule_score": rs,
            "filtered": filtered,
            "filter_reason": reason,
        })

    candidates.sort(key=lambda x: (-x["rule_score"], x["instrument"]))
    return {
        "candidates": candidates,
        "universe_size": len(sig),
        "n_signal_pool": sum(1 for c in candidates if c.get("from_signal")),
        "n_extension": sum(1 for c in candidates if c.get("is_extension")),
        "n_event_hit": sum(1 for c in candidates if c.get("events")),
        "n_filtered": sum(1 for c in candidates if c.get("filtered")),
    }
