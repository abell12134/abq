"""短线猎手：候选 → LLM 预测 → 持续跟踪（纯看板建议，不改订单/不开账户）。

用法：
  python overlays/swing_hunter/run_swing.py                      # 完整跑：跟踪+新预测
  python overlays/swing_hunter/run_swing.py --dry-run            # 不调 LLM（管线联调）
  python overlays/swing_hunter/run_swing.py --track-only         # 只更新旧预测跟踪
  python overlays/swing_hunter/run_swing.py --max-llm 3 --account live_manual_10k

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
from overlays.swing_hunter import report as RPT  # noqa: E402
from overlays.swing_hunter import sentiment_prep as SP  # noqa: E402
from overlays.swing_hunter import store, tracker  # noqa: E402
from overlays.swing_hunter.schema import (  # noqa: E402
    MAX_LLM_CALLS,
    Prediction,
    PredictionFile,
    TrackRecord,
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
) -> int:
    day = day or datetime.now(TZ).strftime("%Y-%m-%d")
    store.ensure_dirs()
    print(f"[swing_hunter] day={day} account={account} dry_run={dry_run}")

    tsum = tracker.run_tracking(day)
    print(f"[OK] 跟踪更新 tracked={tsum['tracked']} entered={tsum['entered']} "
          f"hit={tsum['hit']} stopped={tsum['stopped']} expired={tsum['expired']}")
    for line in tsum["details"]:
        print(f"    · {line}")

    # 活跃票 delta（仅今日新增舆情，轻量 LLM）
    delta_summary: dict[str, Any] = {}
    if not skip_delta and store.all_active_records():
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
        return 0

    built = CD.build_candidates(day, account=account)
    cands: list[dict[str, Any]] = built["candidates"]
    print(f"[OK] 候选池 {len(cands)} 只（强势池 {built['n_signal_pool']} / "
          f"延伸 {built['n_extension']} / 事件命中 {built['n_event_hit']} / "
          f"硬伤过滤 {built['n_filtered']}）")
    if not cands:
        pf = PredictionFile(date=day, status="fail_open", universe_size=built["universe_size"],
                            candidates=[], predictions=[], fail_reason="候选池为空")
        write_predictions(pf)
        RPT.write_daily_report(day, pf)
        return 0

    active_insts = {r.instrument for r in store.all_active_records()}
    actionable = [
        c for c in cands
        if not c["filtered"] and c["instrument"] not in active_insts
    ][: max(1, int(max_llm))]

    names = _lookup_names([c["instrument"] for c in actionable])
    # 无舆情则采集后再刷新 events
    prep_stats = SP.ensure_for_candidates(actionable, names)
    print(f"[OK] 舆情预采集 collected={prep_stats['collected']} "
          f"skipped={prep_stats['skipped']}")

    market_notes = _market_notes()
    predictions: list[Prediction] = []
    llm_traces: list[dict[str, Any]] = []
    n_llm_ok = 0
    for c in actionable:
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
        print(f"  · {inst} {name}: action={pred.action} conf={pred.confidence} "
              f"swing={pred.swing_score} gate={pred.meta.get('gate_tier', '—')} "
              f"| {'; '.join(pred.reasons[:1])}")

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

    status = "dry_run" if dry_run else "ok"
    pf = PredictionFile(
        date=day, status=status, universe_size=built["universe_size"],
        candidates=[c["instrument"] for c in cands], predictions=predictions,
        meta={
            "account": account, "max_llm": max_llm, "n_llm_ok": n_llm_ok,
            "market_notes": market_notes,
            "sentiment_prep": prep_stats,
            "delta_summary": delta_summary,
            "gate": gate_info,
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
    print(f"[DONE] predictions={len(predictions)} predict={n_predict} "
          f"新入跟踪={n_new} llm_ok={n_llm_ok}")
    print(f"[OK] 报告 → {report_path}")
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
    p.add_argument("--max-llm", type=int, default=MAX_LLM_CALLS)
    args = p.parse_args()
    account = (args.account or "").strip() or None
    return run(
        day=args.date, account=account, dry_run=args.dry_run,
        track_only=args.track_only, max_llm=args.max_llm,
        skip_delta=args.skip_delta,
    )


if __name__ == "__main__":
    raise SystemExit(main())
