"""对指定账户当前持仓跑三路分析：舆情 → 研究（4 分析师）→ 短线猎手。

默认宇宙：实盘线 live_manual_10k + TA影子线 shadow_ta_sim 的 holdings（shares>0）。
--full：持仓追踪快照全部标的（四账户曾买卖，含已清仓）。

    python -m overlays.tracking.run_analyze
    python -m overlays.tracking.run_analyze --dry-run
    python -m overlays.tracking.run_analyze --full
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

QUANT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(QUANT))
sys.path.insert(0, str(QUANT / "ops"))

import common as C  # noqa: E402

from . import store

# 先跑这两条线的当前持仓；后续可扩账户
ANALYZE_ACCOUNTS = ["live_manual_10k", "shadow_ta_sim"]
ACCOUNT_CN = {
    "research_sim_100k": "研究模拟线",
    "live_manual_10k": "实盘线",
    "shadow_ctrl_sim": "对照影子线",
    "shadow_ta_sim": "TA影子线",
}


def _pack_universe(items: list[dict[str, Any]], accounts: list[str], *,
                   mode: str) -> dict[str, Any]:
    instruments = [it["instrument"] for it in items]
    held = sum(1 for it in items if it.get("still_held") or (it.get("shares") or 0) > 0)
    return {
        "accounts": accounts,
        "account_labels": [ACCOUNT_CN.get(a, a) for a in accounts],
        "instruments": instruments,
        "items": items,
        "total": len(instruments),
        "held_count": held,
        "mode": mode,
    }


def load_holdings_universe(accounts: list[str] | None = None) -> dict[str, Any]:
    """返回 {instruments, items: [{accounts, shares, name}], ...}。"""
    import pandas as pd

    accs = accounts or ANALYZE_ACCOUNTS
    by_inst: dict[str, dict[str, Any]] = {}
    snap = store.load_snapshot() or {}
    names = {x.get("instrument"): x.get("name") or ""
             for x in (snap.get("instruments") or [])}

    for acc in accs:
        try:
            hf = C.account_subdirs(acc)["nav"] / "holdings.csv"
        except Exception:  # noqa: BLE001
            continue
        if not hf.exists():
            continue
        try:
            df = pd.read_csv(hf)
        except Exception:  # noqa: BLE001
            continue
        if df.empty or "instrument" not in df.columns:
            continue
        for r in df.itertuples():
            inst = str(r.instrument).upper()
            shares = int(getattr(r, "shares", 0) or 0)
            if shares <= 0 or len(inst) < 8:
                continue
            e = by_inst.setdefault(inst, {
                "instrument": inst,
                "name": names.get(inst, ""),
                "accounts": [],
                "shares": 0,
            })
            if acc not in e["accounts"]:
                e["accounts"].append(acc)
            e["shares"] += shares
            e["still_held"] = True
            if not e["name"] and names.get(inst):
                e["name"] = names[inst]

    items = [by_inst[i] for i in sorted(by_inst.keys())]
    return _pack_universe(items, accs, mode="holdings")


def load_full_universe() -> dict[str, Any]:
    """追踪页全部标的：快照优先，无快照则聚合四账户 fills（含已清仓）。"""
    from .build import ACCOUNTS, _load_all_fills

    snap = store.load_snapshot() or {}
    accs = [a for a in (snap.get("accounts") or ACCOUNTS) if a]
    if not accs:
        accs = list(ACCOUNTS)
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for x in snap.get("instruments") or []:
        inst = str(x.get("instrument") or "").upper()
        if len(inst) < 8 or inst[:2] not in {"SH", "SZ"} or inst in seen:
            continue
        seen.add(inst)
        net = int(x.get("net_shares") or 0)
        items.append({
            "instrument": inst,
            "name": x.get("name") or "",
            "accounts": list(x.get("accounts") or []),
            "shares": net,
            "still_held": bool(x.get("still_held") if x.get("still_held") is not None else net > 0),
        })
    if not items:
        by_fills = _load_all_fills()
        for inst in sorted(by_fills.keys()):
            inst = str(inst).upper()
            if len(inst) < 8 or inst[:2] not in {"SH", "SZ"}:
                continue
            fills = by_fills[inst]
            net = (sum(f["shares"] for f in fills if f["side"] == "BUY")
                   - sum(f["shares"] for f in fills if f["side"] == "SELL"))
            items.append({
                "instrument": inst,
                "name": "",
                "accounts": sorted({f["account"] for f in fills}),
                "shares": int(net),
                "still_held": net > 0,
            })
    items.sort(key=lambda it: it["instrument"])
    return _pack_universe(items, accs, mode="full")


def load_analyze_universe(*, full: bool = False,
                          accounts: list[str] | None = None) -> dict[str, Any]:
    return load_full_universe() if full else load_holdings_universe(accounts)


def _llm_busy_reason() -> str | None:
    if store.read_analyze_job().get("status") == "running":
        return "持仓分析正在跑"
    try:
        from overlays.sentiment_memory import job as SJ
        if SJ.read_job().get("status") == "running":
            return "舆情分析正在跑"
    except Exception:  # noqa: BLE001
        pass
    try:
        from overlays.research import job as RJ
        if RJ.read_job().get("status") == "running":
            return "研究分析正在跑"
    except Exception:  # noqa: BLE001
        pass
    try:
        from overlays.swing_hunter import job as WJ
        if WJ.read_job().get("status") == "running":
            return "短线猎手正在跑"
    except Exception:  # noqa: BLE001
        pass
    return None


def _lookup_names(instruments: list[str]) -> dict[str, str]:
    snap = store.load_snapshot() or {}
    names = {x.get("instrument"): x.get("name") or ""
             for x in (snap.get("instruments") or [])}
    missing = [i for i in instruments if not names.get(i)]
    if missing:
        try:
            from overlays.sentiment_memory.run_memory import _lookup_names as _lk
            names.update(_lk(missing))
        except Exception:  # noqa: BLE001
            pass
    return {i: names.get(i, "") or "" for i in instruments}


def _run_sentiment(instruments: list[str], names: dict[str, str], *,
                   force_llm: str, dry_run: bool) -> int:
    from overlays.sentiment_memory.run_memory import run
    n = len(instruments)
    store.tick_analyze("sentiment", 0, n, message="开始舆情")
    rc = run(instruments=instruments, force_llm=force_llm, account=None, dry_run=dry_run)
    store.tick_analyze("sentiment", n, n, message="舆情完成")
    return 0 if rc is None else int(rc)


def _run_research(instruments: list[str], names: dict[str, str], *,
                  force_llm: str, dry_run: bool) -> int:
    from overlays.research import analyze as A
    from overlays.research import store as RS

    n = len(instruments)
    try:
        day = C.latest_trading_day()
    except Exception:  # noqa: BLE001
        from datetime import datetime
        from zoneinfo import ZoneInfo
        day = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
    store.tick_analyze("research", 0, n, message="开始研究")
    cache: dict[str, Any] = {}
    n_ok = 0
    for i, inst in enumerate(instruments, 1):
        name = names.get(inst, "") or ""
        store.tick_analyze("research", i - 1, n, instrument=inst, name=name, message="研究中")
        try:
            report = A.analyze_instrument(
                inst, name, day,
                force_llm=None if dry_run else force_llm,
                dry_run=dry_run, sources=["manual"], global_cache=cache,
            )
            RS.save_report(report.to_dict())
            n_ok += 1
            print(f"  · [research {i}/{n}] {inst} {name}: "
                  f"{report.merged_direction} conf={report.merged_confidence}", flush=True)
        except Exception as ex:  # noqa: BLE001
            print(f"  [FAIL research] {inst}: {ex}", flush=True)
        store.tick_analyze("research", i, n, instrument=inst, name=name,
                           message="ok" if n_ok == i else "fail", n_rs_ok=n_ok)
    if not dry_run:
        try:
            RS.mark_done(day)
        except Exception:  # noqa: BLE001
            pass
    return n_ok


def _run_swing(instruments: list[str], names: dict[str, str], *,
               force_llm: str, dry_run: bool) -> int:
    from overlays.swing_hunter import analyze as A
    from overlays.swing_hunter import candidates as CD
    from overlays.swing_hunter import sentiment_prep as SP
    from overlays.swing_hunter import store as SW
    from overlays.swing_hunter.schema import TrackRecord, normalize_prediction

    n = len(instruments)
    store.tick_analyze("swing", 0, n, message="开始短线")
    try:
        day = C.latest_trading_day()
    except Exception:  # noqa: BLE001
        from datetime import datetime
        from zoneinfo import ZoneInfo
        day = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")

    built = CD.build_candidates(day, account="research_sim_100k")
    by_inst = {c["instrument"]: c for c in built["candidates"]}
    missing = [i for i in instruments if i not in by_inst]
    if missing:
        feats = CD.load_price_features(missing, day)
        events = CD.recent_events(set(missing))
        for inst in missing:
            items = events.get(inst, [])
            boost, cat_hits = CD.event_boost(items)
            feat = feats.get(inst)
            rs = CD.rule_score(feat, None, boost, True)
            by_inst[inst] = {
                "instrument": inst, "rank": float("nan"), "score_lgbm": float("nan"),
                "from_signal": False, "from_momentum": False, "is_extension": True,
                "feat": feat or {}, "events": items[:10],
                "catalyst_hints": cat_hits, "pattern_hits": [],
                "rule_score": rs, "filtered": False, "filter_reason": "",
            }
    cands = [by_inst[i] for i in instruments]
    if not dry_run:
        SP.ensure_for_candidates(cands, names)

    n_ok = n_predict = n_watch = n_reject = 0
    for i, c in enumerate(cands, 1):
        inst = c["instrument"]
        name = names.get(inst, "") or ""
        if dry_run or c.get("filtered"):
            pred = A.dry_run_prediction(c, name)
            if c.get("filtered"):
                pred.action = "reject"
                pred.reasons = [f"硬伤过滤：{c.get('filter_reason')}"]
        else:
            try:
                pred, _ = A.analyze_candidate(c, name=name, force_llm=force_llm)
                pred = normalize_prediction(pred)
                n_ok += 1
            except Exception as ex:  # noqa: BLE001
                pred = A.dry_run_prediction(c, name)
                pred.reasons = [f"LLM 异常：{ex}"[:120]]
                pred.meta["llm_error"] = True
        action = pred.action
        if action == "predict":
            n_predict += 1
            state = "triggered"
        elif action == "watch":
            n_watch += 1
            state = "watch"
        else:
            n_reject += 1
            state = "reject"
        if not dry_run and action in ("predict", "watch"):
            rec = TrackRecord(
                pred_date=day, instrument=pred.instrument, name=pred.name or name,
                state=state, confidence=pred.confidence,
                swing_score=pred.swing_score, catalysts=pred.catalysts,
                reasons=pred.reasons,
            )
            SW.upsert_record(rec)
        print(f"  · [swing {i}/{n}] {inst} {name}: {action}", flush=True)
        store.tick_analyze("swing", i, n, instrument=inst, name=name,
                           message=action, n_swing_ok=n_ok)

    if not dry_run:
        SW.update_catalog(day)
    return n_ok


def run(*, accounts: list[str] | None = None, dry_run: bool = False,
        force_llm: str = "peak", progress: bool = True,
        start_job: bool = True, full: bool = False) -> dict[str, Any]:
    uni = load_analyze_universe(full=full, accounts=accounts)
    instruments = uni["instruments"]
    names = _lookup_names(instruments)
    for it in uni["items"]:
        if not it.get("name"):
            it["name"] = names.get(it["instrument"], "")

    if not instruments:
        msg = ("追踪快照为空，请先重新构建" if full
               else "实盘线 + TA线当前无持仓")
        if progress and start_job:
            store.start_analyze_job(
                accounts=uni["accounts"], instruments=[], names={}, mode=uni["mode"])
            store.finish_analyze_job(ok=False, message=msg)
        elif progress:
            store.finish_analyze_job(ok=False, message=msg)
        return {"ok": False, "total": 0, "message": msg, "mode": uni["mode"]}

    if progress and start_job:
        store.start_analyze_job(
            accounts=uni["accounts"], instruments=instruments, names=names,
            mode=uni["mode"])

    scope = "全量 · 追踪快照" if full else ", ".join(uni["account_labels"])
    print(f"[analyze] {uni['total']} 只 · {scope}", flush=True)
    for it in uni["items"]:
        print(f"    · {it['instrument']} {it.get('name') or ''} "
              f"acc={'+'.join(it['accounts'])} shares={it['shares']}", flush=True)

    try:
        _run_sentiment(instruments, names, force_llm=force_llm, dry_run=dry_run)
        _run_research(instruments, names, force_llm=force_llm, dry_run=dry_run)
        n_sw = _run_swing(instruments, names, force_llm=force_llm, dry_run=dry_run)
        store.tick_analyze("attach", 1, 1, message="回写追踪快照")
        from .build import refresh_agent_fields
        cov = refresh_agent_fields()
        scope_done = "全量（追踪快照）" if full else "实盘+TA 持仓"
        msg = (f"完成：{len(instruments)} 只 · {scope_done}"
               f"{'（dry-run）' if dry_run else ''}")
        print(f"[DONE] {msg} coverage={cov.get('coverage')}", flush=True)
        if progress:
            store.finish_analyze_job(ok=True, message=msg, coverage=cov.get("coverage"),
                                     n_swing_ok=n_sw, mode=uni["mode"])
        return {"ok": True, "total": len(instruments), "message": msg,
                "mode": uni["mode"], "coverage": cov.get("coverage")}
    except Exception as e:  # noqa: BLE001
        if progress:
            store.finish_analyze_job(ok=False, message=f"持仓分析失败: {e}"[:200])
        raise


def main() -> int:
    import argparse
    p = argparse.ArgumentParser(description="持仓追踪：舆情/研究/短线三路分析")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force-llm", default="peak", choices=["peak", "offpeak"])
    p.add_argument("--full", action="store_true",
                   help="全量：追踪快照全部标的（四账户曾买卖，含已清仓）")
    args = p.parse_args()
    out = run(dry_run=args.dry_run, force_llm=args.force_llm, progress=True,
              full=args.full)
    print(out.get("message", ""))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
