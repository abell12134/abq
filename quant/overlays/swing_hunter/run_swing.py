"""短线猎手：候选 → LLM 预测 → 持续跟踪（纯看板建议，不改订单/不开账户）。

用法：
  python overlays/swing_hunter/run_swing.py                      # 完整跑：跟踪+新预测
  python overlays/swing_hunter/run_swing.py --dry-run            # 不调 LLM（管线联调）
  python overlays/swing_hunter/run_swing.py --track-only         # 只更新旧预测跟踪
  python overlays/swing_hunter/run_swing.py --account live_manual_10k          # 默认全候选 LLM
  python overlays/swing_hunter/run_swing.py --max-llm 5 --account live_manual_10k  # 可选截断

接入：ops/run_daily.py evening 在舆情记忆之后调用（账户 use_swing_hunter 开启时）。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

QUANT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(QUANT))
sys.path.insert(0, str(QUANT / "ops"))

import common as C  # noqa: E402

from overlays.swing_hunter import analyze as A  # noqa: E402
from overlays.swing_hunter import candidates as CD  # noqa: E402
from overlays.swing_hunter import delta_track as DT  # noqa: E402
from overlays.swing_hunter import job as JOB  # noqa: E402
from overlays.swing_hunter import report as RPT  # noqa: E402
from overlays.swing_hunter import sentiment_prep as SP  # noqa: E402
from overlays.swing_hunter import store, tracker  # noqa: E402
from overlays.swing_hunter.schema import (  # noqa: E402
    MAX_LLM_CALLS,
    Prediction,
    PredictionFile,
    TrackRecord,
    already_predicted_today,
    mark_skip_meta,
    normalize_prediction,
    read_predictions,
    write_predictions,
)

TZ = ZoneInfo("Asia/Shanghai")


def _market_notes(limit: int = 5) -> list[str]:
    try:
        from overlays.sentiment_memory import store as sm_store
        raw = sm_store.load_raw(lookback_days=3)
    except Exception:  # noqa: BLE001
        return []
    return [
        f"{it.get('published')} | {it.get('title')}"
        for it in raw
        if str(it.get("source", "")).startswith("policy_") or it.get("kind") == "政策宏观"
    ][:limit]


def _lookup_names(instruments: list[str]) -> dict[str, str]:
    try:
        from overlays.sentiment_memory.run_memory import _lookup_names
        return _lookup_names(instruments)
    except Exception:  # noqa: BLE001
        return {i: "" for i in instruments}


def run(
    day: str | None = None,
    account: str | None = "live_manual_10k",
    dry_run: bool = False,
    track_only: bool = False,
    max_llm: int = MAX_LLM_CALLS,
    skip_delta: bool = False,
    force: bool = False,
) -> int:
    day = day or datetime.now(TZ).strftime("%Y-%m-%d")
    store.ensure_dirs()
    cur = JOB.read_job()
    if cur.get("status") != "running":
        JOB.start_job(account=account, dry_run=dry_run, track_only=track_only, day=day)
    else:
        JOB.write_job({"day": day, "account": account, "dry_run": dry_run,
                       "track_only": track_only})
    print(f"[swing_hunter] day={day} account={account} dry_run={dry_run} force={force}")

    try:
        rc = _run_impl(
            day=day, account=account, dry_run=dry_run, track_only=track_only,
            max_llm=max_llm, skip_delta=skip_delta, force=force,
        )
        return rc
    except Exception as e:  # noqa: BLE001
        JOB.finish_job(ok=False, message=str(e)[:200])
        raise


def _run_impl(
    day: str,
    account: str | None,
    dry_run: bool,
    track_only: bool,
    max_llm: int,
    skip_delta: bool,
    force: bool,
) -> int:
    JOB.set_phase("track", "跟踪更新中…", pct=6)
    tsum = tracker.run_tracking(day)
    print(f"[OK] 跟踪更新 tracked={tsum['tracked']} entered={tsum['entered']} "
          f"hit={tsum['hit']} stopped={tsum['stopped']} expired={tsum['expired']}")
    for line in tsum["details"]:
        print(f"    · {line}")

    # 同日幂等：已有 status=ok 预测则跳过新预测/贵价 LLM（跟踪已更新；delta 仍可跑）
    existing = None if force or dry_run or track_only else already_predicted_today(day)
    if existing is not None:
        print(f"[SKIP] 同日已有成功预测（first_runner="
              f"{(existing.meta or {}).get('first_runner') or (existing.meta or {}).get('account')}），"
              f"跳过新预测 LLM；账户={account}")
        JOB.set_phase("skip", "同日已有预测，跳过 LLM…", pct=90)
        delta_summary: dict[str, Any] = {}
        if not skip_delta and store.all_active_records():
            names_active = _lookup_names([r.instrument for r in store.all_active_records()])
            delta_summary = DT.run_delta_updates(
                day, names_active, dry_run=False, force_llm="offpeak",
            )
            print(f"[OK] delta 更新 {delta_summary.get('updated')} 只，"
                  f"跳过 {delta_summary.get('skipped')}")
        pf = mark_skip_meta(existing, account)
        if delta_summary:
            pf.meta["delta_summary"] = delta_summary
        write_predictions(pf)
        store.update_catalog(day)
        RPT.write_daily_report(day, pf)
        JOB.finish_job(ok=True, message="同日已有预测，已跳过 LLM")
        return 0

    # 活跃票 delta（仅今日新增舆情，轻量 LLM）
    delta_summary = {}
    if not skip_delta and store.all_active_records():
        JOB.set_phase("delta", "活跃票 delta 更新中…", pct=10)
        names_active = _lookup_names([r.instrument for r in store.all_active_records()])
        delta_summary = DT.run_delta_updates(
            day, names_active, dry_run=dry_run,
            force_llm=None if dry_run else "offpeak",
        )
        print(f"[OK] delta 更新 {delta_summary.get('updated')} 只，"
              f"跳过 {delta_summary.get('skipped')}")
        for line in delta_summary.get("details", [])[:8]:
            print(f"    · {line}")

    if track_only:
        if delta_summary:
            pf_old = read_predictions(day)
            if pf_old:
                pf_old.meta["delta_summary"] = delta_summary
                write_predictions(pf_old)
        RPT.write_daily_report(day)
        JOB.finish_job(ok=True, message="仅跟踪完成")
        return 0

    JOB.set_phase("candidates", "构建候选池…", pct=12)
    built = CD.build_candidates(day, account=account)
    cands: list[dict[str, Any]] = built["candidates"]
    print(f"[OK] 候选池 {len(cands)} 只（强势池 {built['n_signal_pool']} / "
          f"动量 {built.get('n_momentum', 0)} / "
          f"延伸 {built['n_extension']} / 事件命中 {built['n_event_hit']} / "
          f"模式命中 {built.get('n_pattern_hit', 0)} / "
          f"硬伤过滤 {built['n_filtered']}）")
    if not cands:
        pf = PredictionFile(date=day, status="fail_open", universe_size=built["universe_size"],
                            candidates=[], predictions=[], fail_reason="候选池为空")
        write_predictions(pf)
        RPT.write_daily_report(day, pf)
        JOB.finish_job(ok=True, message="候选池为空")
        return 0

    active_insts = {r.instrument for r in store.all_active_records()}
    actionable = [
        c for c in cands
        if not c["filtered"] and c["instrument"] not in active_insts
    ]
    # max_llm<=0：不截断，全部未过滤候选都跑 LLM（默认，避免漏票）
    if int(max_llm) > 0:
        actionable = actionable[: int(max_llm)]
    print(f"[OK] 待 LLM 深析 {len(actionable)} 只"
          f"{'' if int(max_llm) <= 0 else f'（上限 max_llm={max_llm}）'}")
    JOB.set_llm_total(len(actionable))

    names = _lookup_names([c["instrument"] for c in actionable])
    # 无舆情则采集后再刷新 events
    prep_stats = SP.ensure_for_candidates(actionable, names)
    print(f"[OK] 舆情预采集 collected={prep_stats['collected']} "
          f"skipped={prep_stats['skipped']}")

    market_notes = _market_notes()
    predictions: list[Prediction] = []
    llm_traces: list[dict[str, Any]] = []
    n_llm_ok = 0
    n_actionable = len(actionable)
    for i, c in enumerate(actionable, 1):
        inst = c["instrument"]
        name = names.get(inst, "") or ""
        if "ST" in name.upper():
            c["filtered"], c["filter_reason"] = True, "ST 名称"
        if dry_run or c["filtered"]:
            pred = A.dry_run_prediction(c, name)
            if c["filtered"]:
                pred.action = "reject"
                pred.reasons = [f"硬伤过滤：{c['filter_reason']}"]
                pred.meta["rule_filtered"] = True
            llm_traces.append({})
        else:
            try:
                pred, trace = analyze_with_alert(c, name, market_notes, day)
                n_llm_ok += 1
                llm_traces.append(trace)
            except Exception:  # noqa: BLE001
                pred = A.dry_run_prediction(c, name)
                pred.reasons = ["LLM 异常，降级为观察（fail-open）"]
                pred.meta["llm_error"] = True
                llm_traces.append({})
        predictions.append(pred)
        reason = "; ".join(pred.reasons[:1])
        print(f"  · [{i}/{n_actionable}] {inst} {name}: action={pred.action} "
              f"conf={pred.confidence} swing={pred.swing_score} "
              f"gate={pred.meta.get('gate_tier', '—')} "
              f"| {reason}", flush=True)
        try:
            JOB.tick_llm(
                i, n_actionable, instrument=inst, name=name,
                action=pred.action, reason=reason,
            )
        except Exception:  # noqa: BLE001
            pass

    gate_info: dict[str, Any] = {}
    if not dry_run and llm_traces and any(llm_traces):
        stub_rows = [
            {
                "instrument": p.instrument,
                "prediction": p.to_dict(),
                "elapsed_sec": 0,
                "usage": {},
            }
            for p in predictions
        ]
        try:
            JOB.set_phase("gate", "门槛降档裁判中…", pct=94)
            new_rows, new_traces, gate_info = A.apply_gate_fallback(
                actionable, names, stub_rows, llm_traces,
            )
            if gate_info.get("fallback_used"):
                print(f"[gate] 严格档 predict=0 → 降档 standard，"
                      f"最终 predict={gate_info.get('n_predict_final')}")
                for i, row in enumerate(new_rows):
                    predictions[i] = Prediction.from_dict(row["prediction"])
                    if row.get("gate_fallback"):
                        llm_traces[i] = new_traces[i]
        except Exception as e:  # noqa: BLE001
            gate_info = {"fallback_used": False, "error": str(e)}
            print(f"[WARN] gate 降档失败，保留 strict 结果: {e}", flush=True)
            C.alert("WARN", f"短线猎手 gate 降档失败（已保留 strict）: {e}", day)

    llm_set = {c["instrument"] for c in actionable}
    for c in cands:
        if c["instrument"] in llm_set or c["instrument"] in active_insts:
            continue
        pred = A.dry_run_prediction(c)
        if c["filtered"]:
            pred.action = "reject"
            pred.reasons = [f"硬伤过滤：{c['filter_reason']}"]
            pred.meta["rule_filtered"] = True
        predictions.append(normalize_prediction(pred))

    JOB.set_phase("finishing", "写入报告…", pct=96)
    status = "dry_run" if dry_run else "ok"
    pf = PredictionFile(
        date=day, status=status, universe_size=built["universe_size"],
        candidates=[c["instrument"] for c in cands], predictions=predictions,
        meta={
            "account": account,
            "first_runner": account,
            "max_llm": max_llm, "n_llm_ok": n_llm_ok,
            "market_notes": market_notes,
            "sentiment_prep": prep_stats,
            "delta_summary": delta_summary,
            "gate": gate_info,
            "pool_stats": {
                "n_signal_pool": built.get("n_signal_pool"),
                "n_momentum": built.get("n_momentum"),
                "n_extension": built.get("n_extension"),
                "n_pattern_hit": built.get("n_pattern_hit"),
            },
        },
    )
    path = write_predictions(pf)
    report_path = RPT.write_daily_report(day, pf)

    n_new = 0
    if not dry_run:
        for pred in predictions:
            if pred.action != "predict":
                continue
            rec = TrackRecord(
                pred_date=day, instrument=pred.instrument, name=pred.name,
                state="triggered", confidence=pred.confidence,
                swing_score=pred.swing_score, catalysts=pred.catalysts,
                reasons=pred.reasons,
            )
            store.upsert_record(rec)
            n_new += 1
    store.update_catalog(day)

    n_predict = sum(1 for p in predictions if p.action == "predict")
    n_watch = sum(1 for p in predictions if p.action == "watch")
    n_reject = sum(1 for p in predictions if p.action == "reject")
    print(f"[DONE] predictions={len(predictions)} predict={n_predict} "
          f"新入跟踪={n_new} llm_ok={n_llm_ok} → {path}")
    print(f"[OK] 报告 → {report_path}")
    JOB.write_job({
        "n_predict": n_predict, "n_watch": n_watch, "n_reject": n_reject,
        "done_count": n_llm_ok or n_actionable, "total": n_actionable,
    })
    JOB.finish_job(
        ok=True,
        message=(f"完成：predict {n_predict} / watch {n_watch} / reject {n_reject}"
                 f"（LLM {n_llm_ok}/{n_actionable}）"),
    )
    return 0


def analyze_with_alert(cand: dict[str, Any], name: str,
                       market_notes: list[str], day: str):
    try:
        return A.analyze_candidate(cand, name=name, market_notes=market_notes)
    except Exception as e:  # noqa: BLE001
        C.alert("WARN", f"短线猎手 LLM 分析失败 {cand['instrument']}: {e}", day)
        raise


def main() -> int:
    p = argparse.ArgumentParser(description="短线猎手：5~15 日 10%~20% 预测与跟踪（纯建议）")
    p.add_argument("--date", default=None)
    p.add_argument("--account", default="live_manual_10k")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--track-only", action="store_true")
    p.add_argument("--skip-delta", action="store_true", help="跳过活跃票 delta LLM")
    p.add_argument("--force", action="store_true",
                   help="忽略同日已有预测，强制重跑新预测 LLM")
    p.add_argument("--max-llm", type=int, default=MAX_LLM_CALLS,
                   help="LLM 深析上限；0=全部未过滤候选（默认）")
    args = p.parse_args()
    account = (args.account or "").strip() or None
    return run(
        day=args.date, account=account, dry_run=args.dry_run,
        track_only=args.track_only, max_llm=args.max_llm,
        skip_delta=args.skip_delta, force=args.force,
    )


if __name__ == "__main__":
    raise SystemExit(main())
