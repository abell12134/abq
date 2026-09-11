"""持仓追踪快照构建：聚合 fills + 拉取行情 + 计算涨跌区间 + 附舆情摘要。

可被 webapp 后台线程调用，也可 CLI 单独运行：
    python -m overlays.tracking.build            # 默认全部账户
    python -m overlays.tracking.build --refresh  # 强制刷新行情
"""

from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

from zoneinfo import ZoneInfo

QUANT = Path(__file__).resolve().parents[2]
for sub in ("ops", "webapp"):
    sys.path.insert(0, str(QUANT / sub))
import common as C  # noqa: E402
import review_accounts as RA  # noqa: E402
import quotes as Q   # noqa: E402

from . import store
from .fundflow import fetch_fundflow

TZ = ZoneInfo("Asia/Shanghai")

# 看板纳管的账户（与 webapp.server.ACCOUNTS 对齐）
ACCOUNTS = [
    "research_sim_100k",
    "live_manual_10k",
    "shadow_ctrl_sim",
    "shadow_ta_sim",
]

# 行情拉取并发：腾讯/东财有节流，过大会被限流。
QUOTE_WORKERS = 6
# 单票最大 K 线根数（覆盖约一年交易日）
MAX_LMT = 250


def _load_all_fills() -> dict[str, list[dict]]:
    """instrument -> [fill row, ...]（含 account 字段）。"""
    by_inst: dict[str, list[dict]] = {}
    for acc in ACCOUNTS:
        try:
            df = RA.load_fills(acc)
        except Exception:
            continue
        if df.empty:
            continue
        for r in df.itertuples():
            inst = str(r.instrument).upper()
            by_inst.setdefault(inst, []).append({
                "date": str(r.date),
                "instrument": inst,
                "side": str(r.side).upper(),
                "shares": int(r.shares),
                "price": float(r.price),
                "amount": float(r.amount) if "amount" in df.columns else float(r.shares) * float(r.price),
                "account": acc,
            })
    return by_inst


def _load_sentiment_catalog() -> dict[str, dict]:
    try:
        sys.path.insert(0, str(QUANT))
        from overlays.sentiment_memory import store as SM  # noqa: WPS433
        cat = SM.load_catalog()
        return {x["instrument"]: x for x in (cat.get("instruments") or {}).values()}
    except Exception:
        return {}


def _summarize_swing(rec: dict) -> dict:
    """tracker 最新一条：去逐日明细，保留催化/理由/结果。"""
    last_delta = None
    deltas = rec.get("deltas") or []
    if deltas and isinstance(deltas[-1], dict):
        d = deltas[-1]
        last_delta = {
            "date": d.get("date"),
            "stance": d.get("stance"),
            "headline": d.get("headline"),
        }
    mfe, mae = rec.get("mfe"), rec.get("mae")
    ret = rec.get("result_return")
    return {
        "pred_date": rec.get("pred_date"),
        "state": rec.get("state"),
        "confidence": rec.get("confidence"),
        "swing_score": rec.get("swing_score"),
        "catalysts": rec.get("catalysts") or [],
        "reasons": rec.get("reasons") or [],
        "entry_date": rec.get("entry_date"),
        "entry_price": rec.get("entry_price"),
        "days_held": rec.get("days_held"),
        "mfe": round(float(mfe) * 100, 2) if mfe is not None else None,
        "mae": round(float(mae) * 100, 2) if mae is not None else None,
        "hit_tier": rec.get("hit_tier") or 0,
        "result": rec.get("result"),
        "result_date": rec.get("result_date"),
        "result_return": round(float(ret) * 100, 2) if ret is not None else None,
        "delta": last_delta,
    }


def _load_swing_index() -> dict[str, dict]:
    """instrument → 最新一条短线猎手跟踪摘要。"""
    try:
        sys.path.insert(0, str(QUANT))
        from overlays.swing_hunter import store as SW  # noqa: WPS433
        from overlays.swing_hunter.schema import ROOT as SW_ROOT  # noqa: WPS433
    except Exception:
        return {}
    d = SW_ROOT / "tracker"
    if not d.exists():
        return {}
    out: dict[str, dict] = {}
    for f in d.glob("*.json"):
        try:
            data = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        recs = data.get("records") or []
        if not recs:
            continue
        rec = recs[0] if isinstance(recs[0], dict) else None
        if rec:
            out[str(data.get("instrument") or f.stem).upper()] = _summarize_swing(rec)
    return out


def _summarize_research(rep: dict) -> dict:
    vc = rep.get("verdict_cn") if isinstance(rep.get("verdict_cn"), dict) else {}
    ve = rep.get("verdict_en") if isinstance(rep.get("verdict_en"), dict) else {}
    analysts = []
    for a in rep.get("analysts") or []:
        if not isinstance(a, dict):
            continue
        content = str(a.get("content") or "").strip()
        analysts.append({
            "kind": a.get("kind"),
            "lang": a.get("lang"),
            "brief": content[:280],
        })
    return {
        "date": rep.get("date"),
        "merged_direction": rep.get("merged_direction"),
        "merged_confidence": rep.get("merged_confidence"),
        "consensus": rep.get("consensus"),
        "action_cn": vc.get("action"),
        "summary_cn": str(vc.get("summary") or "")[:400],
        "reasons_cn": [str(x) for x in (vc.get("reasons") or [])][:4],
        "action_en": ve.get("action"),
        "summary_en": str(ve.get("summary") or "")[:280],
        "analysts": analysts,
    }


def _load_research_index() -> dict[str, dict]:
    """instrument → 最新研究报告摘要。"""
    try:
        sys.path.insert(0, str(QUANT))
        from overlays.research import store as RS  # noqa: WPS433
    except Exception:
        return {}
    out: dict[str, dict] = {}
    root = RS.ROOT / "reports"
    if not root.exists():
        return {}
    for inst_dir in root.iterdir():
        if not inst_dir.is_dir():
            continue
        files = sorted(inst_dir.glob("????-??-??.json"), reverse=True)
        if not files:
            continue
        try:
            rep = json.loads(files[0].read_text())
        except (json.JSONDecodeError, OSError):
            continue
        out[inst_dir.name.upper()] = _summarize_research(rep)
    return out


def _lmt_for(first_buy_date: str) -> int:
    """从首次买入日到今天大致需要的 K 线根数（按 1.4 日历日/交易日估）。"""
    try:
        d0 = datetime.fromisoformat(first_buy_date).replace(tzinfo=TZ)
    except Exception:
        return MAX_LMT
    days = (datetime.now(TZ) - d0).days
    return max(20, min(MAX_LMT, int(days / 1.4) + 10))


def _compute_metrics(klines: list[dict], first_buy_date: str, first_buy_price: float) -> dict:
    """从 K 线（含 date/open/close）计算窗口指标。K 线已按日期升序。"""
    # 仅保留 >= first_buy_date 的 K 线
    win = [k for k in klines if str(k.get("date", "")) >= first_buy_date]
    if not win:
        return {
            "current_price": None, "current_date": None,
            "cum_ret": None, "min_close": None, "min_date": None,
            "max_close": None, "max_date": None, "max_dd": None,
            "series": [],
        }
    closes = [float(k["close"]) for k in win]
    dates = [str(k["date"]) for k in win]
    base = first_buy_price if first_buy_price else closes[0]
    series = []
    peak = closes[0]
    max_dd = 0.0
    min_c, min_d = closes[0], dates[0]
    max_c, max_d = closes[0], dates[0]
    for c, d in zip(closes, dates):
        cum = (c / base - 1) * 100 if base else 0.0
        series.append({"date": d, "close": round(c, 3), "cum": round(cum, 3)})
        if c < min_c:
            min_c, min_d = c, d
        if c > max_c:
            max_c, max_d = c, d
        if c > peak:
            peak = c
        dd = (c / peak - 1) * 100 if peak else 0.0
        if dd < max_dd:
            max_dd = dd
    last_c = closes[-1]
    cum_ret = (last_c / base - 1) * 100 if base else 0.0
    return {
        "current_price": round(last_c, 3),
        "current_date": dates[-1],
        "cum_ret": round(cum_ret, 3),
        "min_close": round(min_c, 3),
        "min_date": min_d,
        "max_close": round(max_c, 3),
        "max_date": max_d,
        "max_dd": round(max_dd, 3),
        "series": series,
    }


def _build_one(inst: str, fills: list[dict], sent_cat: dict[str, dict],
               swing_idx: dict[str, dict], research_idx: dict[str, dict]) -> dict | None:
    """构建单票追踪条目。返回 None 表示行情拉取失败。"""
    buys = [f for f in fills if f["side"] == "BUY"]
    sells = [f for f in fills if f["side"] == "SELL"]
    if not buys:
        return None
    buys_sorted = sorted(buys, key=lambda x: x["date"])
    first_buy = buys_sorted[0]
    first_buy_date = first_buy["date"]
    first_buy_price = first_buy["price"]
    net_shares = sum(f["shares"] for f in buys) - sum(f["shares"] for f in sells)
    try:
        fundflow = fetch_fundflow(inst, history_days=5)
    except Exception:  # noqa: BLE001
        fundflow = {"ok": False}
    extra = {
        "sentiment": sent_cat.get(inst),
        "swing": swing_idx.get(inst),
        "research": research_idx.get(inst),
        "fundflow": fundflow if fundflow.get("ok") else None,
    }

    lmt = _lmt_for(first_buy_date)
    try:
        q = Q.quote(inst, klt=101, lmt=lmt, fqt=1)
    except Exception:
        q = None
    if not q or not q.get("ok") or not q.get("klines"):
        # 行情失败：仍返回基础信息（无走势），便于排序展示
        name = (sent_cat.get(inst, {}) or {}).get("name") or inst
        return {
            "instrument": inst,
            "name": name,
            "accounts": sorted({f["account"] for f in fills}),
            "first_buy_date": first_buy_date,
            "first_buy_price": round(first_buy_price, 3),
            "current_price": None,
            "current_date": None,
            "cum_ret": None,
            "min_close": None, "min_date": None,
            "max_close": None, "max_date": None,
            "max_dd": None,
            "series": [],
            "buy_nodes": buys_sorted,
            "sell_nodes": sorted(sells, key=lambda x: x["date"]),
            "net_shares": int(net_shares),
            "still_held": net_shares > 0,
            "quote_ok": False,
            **extra,
        }

    klines = q["klines"]
    m = _compute_metrics(klines, first_buy_date, first_buy_price)
    name = q.get("name") or (sent_cat.get(inst, {}) or {}).get("name") or inst
    return {
        "instrument": inst,
        "name": name,
        "accounts": sorted({f["account"] for f in fills}),
        "first_buy_date": first_buy_date,
        "first_buy_price": round(first_buy_price, 3),
        "current_price": m["current_price"],
        "current_date": m["current_date"],
        "cum_ret": m["cum_ret"],
        "min_close": m["min_close"], "min_date": m["min_date"],
        "max_close": m["max_close"], "max_date": m["max_date"],
        "max_dd": m["max_dd"],
        "series": m["series"],
        "buy_nodes": buys_sorted,
        "sell_nodes": sorted(sells, key=lambda x: x["date"]),
        "net_shares": int(net_shares),
        "still_held": net_shares > 0,
        "quote_ok": True,
        **extra,
    }


def build_snapshot(*, progress: bool = False) -> dict[str, Any]:
    """聚合 fills + 拉行情 + 计算 + 附舆情/短线/研究，返回快照 dict（不落盘）。"""
    by_inst = _load_all_fills()
    sent_cat = _load_sentiment_catalog()
    swing_idx = _load_swing_index()
    research_idx = _load_research_index()
    items = list(by_inst.items())
    total = len(items)
    if progress:
        store.tick(0, total, instrument="—", message=f"开始追踪 {total} 只")
    instruments: list[dict] = []
    n_ok = 0
    with ThreadPoolExecutor(max_workers=QUOTE_WORKERS) as ex:
        futs = {
            ex.submit(_build_one, inst, fills, sent_cat, swing_idx, research_idx): inst
            for inst, fills in items
        }
        done = 0
        for fut in as_completed(futs):
            inst = futs[fut]
            done += 1
            try:
                row = fut.result()
            except Exception:
                row = None
            if row:
                instruments.append(row)
                if row.get("quote_ok"):
                    n_ok += 1
                if progress:
                    store.tick(done, total, instrument=inst,
                               name=row.get("name", ""), message="行情已取" if row.get("quote_ok") else "行情失败")
    # 排序：跌→涨（cum_ret 升序）；无行情的排最后
    def _sort_key(x: dict):
        r = x.get("cum_ret")
        return (1 if r is None else 0, r if r is not None else 0)
    instruments.sort(key=_sort_key)
    try:
        data_day = C.latest_trading_day()
    except Exception:
        data_day = None
    _attach_sector_fields(instruments)
    n_sent = sum(1 for x in instruments if x.get("sentiment"))
    n_swing = sum(1 for x in instruments if x.get("swing"))
    n_rs = sum(1 for x in instruments if x.get("research"))
    n_ff = sum(1 for x in instruments if x.get("fundflow"))
    n_sec = sum(1 for x in instruments if x.get("sector_forecast"))
    return {
        "data_day": data_day,
        "accounts": ACCOUNTS,
        "total": len(instruments),
        "quote_ok_count": n_ok,
        "quote_fail_count": len(instruments) - n_ok,
        "coverage": {
            "sentiment": n_sent,
            "swing": n_swing,
            "research": n_rs,
            "fundflow": n_ff,
            "sector": n_sec,
        },
        "instruments": instruments,
    }


def _load_industry_map() -> dict[str, str]:
    try:
        sys.path.insert(0, str(QUANT))
        from execution.industry import load_industry_map  # noqa: WPS433
        return load_industry_map()
    except Exception:
        return {}


def _attach_sector_fields(instruments: list[dict]) -> None:
    """挂申万一级 + 当日板块预测切片（只读）。"""
    try:
        from overlays.sector_forecast import store as SF  # noqa: WPS433
        from overlays.sector_forecast.schema import (  # noqa: WPS433
            align_stock_sector, industry_bias, stock_bias,
        )
        _day, pred = SF.load_latest()
    except Exception:
        pred = None
        align_stock_sector = industry_bias = stock_bias = None  # type: ignore
    ind_map = _load_industry_map()
    for x in instruments:
        inst = str(x.get("instrument") or "").upper()
        industry = ind_map.get(inst) or "未知"
        x["industry"] = industry
        if not pred or industry == "未知" or align_stock_sector is None:
            x["sector_forecast"] = None
            x["sector_align"] = None
            continue
        rec = (pred.get("by_industry") or {}).get(industry) or {}
        h10, h20 = rec.get("h10"), rec.get("h20")
        ib = industry_bias(h10, h20)
        sb = stock_bias(x.get("swing"), x.get("research"))
        x["sector_forecast"] = {
            "day": pred.get("day"),
            "shadow": bool(pred.get("shadow")),
            "h10": h10,
            "h20": h20,
            "related_concepts": rec.get("related_concepts") or [],
            "news_hits": rec.get("news_hits") or 0,
            "align": align_stock_sector(sb, ib),
            "disclaimer": pred.get("disclaimer"),
        }
        x["sector_align"] = x["sector_forecast"]["align"]


def refresh_agent_fields() -> dict[str, Any]:
    """用最新舆情/短线/研究/板块预测覆盖快照字段，不重拉行情。"""
    snap = store.load_snapshot()
    if not snap:
        return {"ok": False, "coverage": {}, "total": 0}
    sent = _load_sentiment_catalog()
    swing_idx = _load_swing_index()
    research_idx = _load_research_index()
    n_s = n_w = n_r = 0
    for x in snap.get("instruments") or []:
        inst = x.get("instrument")
        if inst in sent:
            x["sentiment"] = sent[inst]
            n_s += 1
        x["swing"] = swing_idx.get(inst)
        x["research"] = research_idx.get(inst)
        if x.get("swing"):
            n_w += 1
        if x.get("research"):
            n_r += 1
    _attach_sector_fields(snap.get("instruments") or [])
    n_sec = sum(1 for x in (snap.get("instruments") or []) if x.get("sector_forecast"))
    cov = dict(snap.get("coverage") or {})
    cov.update({"sentiment": n_s, "swing": n_w, "research": n_r, "sector": n_sec})
    snap["coverage"] = cov
    store.save_snapshot(snap)
    return {"ok": True, "coverage": cov, "total": snap.get("total") or 0}


def refresh_fundflow_fields(*, workers: int = 4) -> dict[str, Any]:
    """仅刷新快照内各票当日资金流向，不重拉行情与其它 Agent 字段。"""
    snap = store.load_snapshot()
    if not snap:
        return {"ok": False, "coverage": {}, "total": 0}
    rows = snap.get("instruments") or []
    n_ff = 0

    def _one(x: dict) -> tuple[str, dict | None]:
        inst = x.get("instrument")
        if not inst:
            return "", None
        try:
            ff = fetch_fundflow(inst, history_days=5)
        except Exception:  # noqa: BLE001
            return inst, None
        return inst, ff if ff.get("ok") else None

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_one, x): x for x in rows}
        for fut in as_completed(futs):
            inst, ff = fut.result()
            if not inst:
                continue
            x = futs[fut]
            x["fundflow"] = ff
            if ff:
                n_ff += 1
    cov = dict(snap.get("coverage") or {})
    cov["fundflow"] = n_ff
    snap["coverage"] = cov
    store.save_snapshot(snap)
    return {"ok": True, "coverage": cov, "total": snap.get("total") or 0}


def build_and_save(*, progress: bool = False) -> dict[str, Any]:
    if progress:
        store.start_job()
    try:
        snap = build_snapshot(progress=progress)
        store.save_snapshot(snap)
        if progress:
            store.finish_job(ok=True, message=f"追踪快照完成 · {snap['total']} 只 · 行情 {snap['quote_ok_count']} 成功")
        return snap
    except Exception as e:  # noqa: BLE001
        if progress:
            store.finish_job(ok=False, message=f"追踪快照失败: {e}"[:200])
        raise


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="构建持仓追踪快照")
    ap.add_argument("--progress", action="store_true", help="写入 job.json 供看板进度条")
    args = ap.parse_args()
    snap = build_and_save(progress=args.progress)
    print(f"snapshot saved: {snap['total']} instruments, "
          f"{snap['quote_ok_count']} quote ok, {snap['quote_fail_count']} failed")


if __name__ == "__main__":
    main()
