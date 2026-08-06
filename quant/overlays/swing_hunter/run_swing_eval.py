"""短线猎手 LLM 评测：本地模型 TopN → DeepSeek 复跑 Top5。

产出（data/overlays/swing_hunter/eval/<day>/）：
  run.log                    完整终端日志
  pass1_local_top15.json     本地模型 15 只全量结果+trace
  pass2_deepseek_top5.json   DeepSeek 复跑 Top5
  comparison.md              对比表（供后续优化）
  traces/{inst}_{pass}.json  单票辩论轨迹

用法：
  python overlays/swing_hunter/run_swing_eval.py --date 2026-08-05
  python overlays/swing_hunter/run_swing_eval.py --date 2026-08-05 --top-n 15 --refine-n 5
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

QUANT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(QUANT))
sys.path.insert(0, str(QUANT / "ops"))

from overlays.swing_hunter import analyze as A  # noqa: E402
from overlays.swing_hunter import candidates as CD  # noqa: E402
from overlays.swing_hunter.run_swing import _lookup_names, _market_notes  # noqa: E402

TZ = ZoneInfo("Asia/Shanghai")
EVAL_ROOT = QUANT / "data" / "overlays" / "swing_hunter" / "eval"
LOG_ROOT = QUANT / "data" / "logs"


class TeeLog:
    """同时写终端与 run.log。"""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = path.open("a", encoding="utf-8")
        self._fh.write(f"\n=== swing_eval {datetime.now(TZ):%F %T} ===\n")

    def line(self, msg: str) -> None:
        print(msg, flush=True)
        self._fh.write(msg + "\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


def _sum_usage(llm_calls: list[dict]) -> dict[str, int]:
    pt, ct = 0, 0
    for c in llm_calls:
        u = c.get("usage") or {}
        pt += int(u.get("prompt_tokens") or 0)
        ct += int(u.get("completion_tokens") or 0)
    return {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct}


def run_one(
    cand: dict[str, Any],
    name: str,
    market_notes: list[str],
    force_llm: str,
    pass_label: str,
    out_dir: Path,
    log: TeeLog,
    gate_tier: str = "strict",
) -> tuple[dict[str, Any], dict[str, Any]]:
    inst = cand["instrument"]
    log.line(f"[{pass_label}] 开始 {inst} {name} force={force_llm} gate={gate_tier} "
             f"rule_score={cand.get('rule_score')} rank={cand.get('rank')}")
    t0 = time.monotonic()
    try:
        pred, trace = A.analyze_candidate(
            cand, name=name, market_notes=market_notes, force_llm=force_llm,
            gate_tier=gate_tier,
        )
        err = None
    except Exception as e:  # noqa: BLE001
        pred = A.dry_run_prediction(cand, name)
        pred.reasons = [f"LLM 异常: {e}"]
        pred.meta["llm_error"] = True
        trace = {"instrument": inst, "error": str(e), "steps": [], "llm_calls": []}
        err = str(e)
        log.line(f"  [FAIL] {inst}: {e}")

    elapsed = round(time.monotonic() - t0, 2)
    usage = _sum_usage(trace.get("llm_calls") or [])
    row = {
        "instrument": inst,
        "name": name,
        "pass": pass_label,
        "force_llm": force_llm,
        "rule_score": cand.get("rule_score"),
        "rank": cand.get("rank"),
        "score_lgbm": cand.get("score_lgbm"),
        "catalyst_hints": cand.get("catalyst_hints"),
        "elapsed_sec": elapsed,
        "usage": usage,
        "gate_tier": gate_tier,
        "prediction": pred.to_dict(),
        "error": err,
    }
    log.line(
        f"  [OK] {inst} action={pred.action} conf={pred.confidence} "
        f"swing={pred.swing_score} elapsed={elapsed}s tokens={usage.get('total_tokens')} "
        f"| {pred.reasons[0] if pred.reasons else ''}"
    )

    trace_path = out_dir / "traces" / f"{inst}_{pass_label}.json"
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    trace_path.write_text(
        json.dumps({**trace, "summary_row": row}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return row, trace


def write_comparison(
    day: str,
    pass1: list[dict],
    pass2: list[dict],
    out: Path,
    gate_info: dict[str, Any] | None = None,
) -> None:
    p1_map = {r["instrument"]: r for r in pass1}
    lines = [
        f"# 短线猎手 LLM 评测对比 · {day}",
        "",
        f"> 生成：{datetime.now(TZ):%F %T}",
        "",
    ]
    if gate_info:
        lines += [
            "## 预测门槛",
            "",
            f"- 初始门槛：**{gate_info.get('initial_tier')}**（{gate_info.get('label_initial')}）",
            f"- 实际采用：**{gate_info.get('applied_tier')}**（{gate_info.get('label_applied')}）",
            f"- 是否降档：{'是' if gate_info.get('fallback_used') else '否'}",
            f"- predict 数：严格档 {gate_info.get('n_predict_initial')} → "
            f"最终 {gate_info.get('n_predict_final')}",
            "",
        ]
    lines += [
        "## Pass1 本地 Top15（按 rule_score 入选，结果按 swing_score 排序）",
        "",
        "| 排名 | 标的 | rule | swing | action | conf | 门槛 | 耗时s | tokens | 理由 |",
        "|------|------|------|-------|--------|------|------|-------|--------|------|",
    ]
    sorted1 = sorted(pass1, key=lambda x: -float(
        (x.get("prediction") or {}).get("swing_score") or 0))
    for i, r in enumerate(sorted1, 1):
        p = r.get("prediction") or {}
        gt = (p.get("meta") or {}).get("gate_tier") or r.get("gate_tier") or "—"
        fb = "↘" if (p.get("meta") or {}).get("gate_fallback") or r.get("gate_fallback") else ""
        lines.append(
            f"| {i} | {r['instrument']} {r.get('name','')} | "
            f"{r.get('rule_score')} | {p.get('swing_score')} | {p.get('action')} | "
            f"{p.get('confidence')} | {gt}{fb} | {r.get('elapsed_sec')} | "
            f"{(r.get('usage') or {}).get('total_tokens', '—')} | "
            f"{(p.get('reasons') or [''])[0][:60]} |"
        )

    lines += [
        "",
        "## Pass2 DeepSeek 复跑 Top5（按 Pass1 swing_score 取前 5）",
        "",
        "| 标的 | P1 action/conf | P2 action/conf | swing Δ | 耗时s | tokens |",
        "|------|----------------|----------------|---------|-------|--------|",
    ]
    for r in pass2:
        inst = r["instrument"]
        p1 = p1_map.get(inst, {})
        p1p = p1.get("prediction") or {}
        p2p = r.get("prediction") or {}
        d_sw = round(float(p2p.get("swing_score") or 0) - float(p1p.get("swing_score") or 0), 4)
        lines.append(
            f"| {inst} | {p1p.get('action')}/{p1p.get('confidence')} | "
            f"{p2p.get('action')}/{p2p.get('confidence')} | {d_sw:+} | "
            f"{r.get('elapsed_sec')} | {(r.get('usage') or {}).get('total_tokens', '—')} |"
        )

    agree = sum(
        1 for r in pass2
        if (r.get("prediction") or {}).get("action")
        == (p1_map.get(r["instrument"], {}).get("prediction") or {}).get("action")
    )
    lines += [
        "",
        "## 汇总",
        "",
        f"- Pass1 本地：{len(pass1)} 只，predict={sum(1 for r in pass1 if (r.get('prediction') or {}).get('action')=='predict')} 只",
        f"- Pass2 DeepSeek：{len(pass2)} 只，动作一致 {agree}/{len(pass2)}",
        f"- Pass1 总 tokens：{sum((r.get('usage') or {}).get('total_tokens', 0) for r in pass1)}",
        f"- Pass2 总 tokens：{sum((r.get('usage') or {}).get('total_tokens', 0) for r in pass2)}",
        "",
        "详细 trace 见 `traces/`；完整 JSON 见 `pass1_*.json` / `pass2_*.json`。",
        "",
        "*研究用途，不构成投资建议。*",
    ]
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")


def publish_eval_predictions(
    day: str,
    eval_dir: Path,
    built: dict[str, Any],
    pass1_rows: list[dict],
    pass2_rows: list[dict],
    gate_info: dict[str, Any],
) -> Path:
    """将评测结果写入 predictions/（看板展示用），Pass2 覆盖 Top5。"""
    from overlays.swing_hunter.schema import Prediction, PredictionFile, write_predictions
    from overlays.swing_hunter import report as RPT

    p2_map = {r["instrument"]: r.get("prediction") for r in pass2_rows}
    predictions: list[Prediction] = []
    for r in pass1_rows:
        inst = r["instrument"]
        raw = p2_map.get(inst) or r.get("prediction") or {}
        pred = Prediction.from_dict(raw)
        if inst in p2_map:
            pred.meta = dict(pred.meta or {})
            pred.meta["eval_pass"] = "pass2_deepseek"
        else:
            pred.meta = dict(pred.meta or {})
            pred.meta["eval_pass"] = "pass1_local"
        predictions.append(pred)

    pf = PredictionFile(
        date=day,
        status="ok",
        universe_size=int(built.get("universe_size") or 0),
        candidates=[c["instrument"] for c in built.get("candidates") or []],
        predictions=predictions,
        meta={
            "source": "eval",
            "gate": gate_info,
            "eval_dir": str(eval_dir),
            "n_pass1": len(pass1_rows),
            "n_pass2": len(pass2_rows),
        },
    )
    path = write_predictions(pf)
    RPT.write_daily_report(day, pf)
    return path


def main() -> int:
    p = argparse.ArgumentParser(description="短线猎手 LLM 双路评测")
    p.add_argument("--date", required=True, help="信号日 YYYY-MM-DD")
    p.add_argument("--account", default="live_manual_10k")
    p.add_argument("--top-n", type=int, default=15, help="Pass1 本地分析只数")
    p.add_argument("--refine-n", type=int, default=5, help="Pass2 DeepSeek 复跑只数")
    p.add_argument("--skip-track", action="store_true", help="跳过当日跟踪更新")
    args = p.parse_args()

    day = args.date
    out_dir = EVAL_ROOT / day
    out_dir.mkdir(parents=True, exist_ok=True)
    log = TeeLog(out_dir / "run.log")

    if not args.skip_track:
        from overlays.swing_hunter import tracker  # noqa: E402
        log.line(f"[track] 更新跟踪至 {day}")
        tsum = tracker.run_tracking(day)
        log.line(f"[track] {json.dumps(tsum, ensure_ascii=False)}")

    log.line(f"[eval] day={day} pass1=local/peak top{args.top_n} "
             f"pass2=deepseek/offpeak top{args.refine_n}")

    built = CD.build_candidates(day, account=args.account)
    actionable = [
        c for c in built["candidates"]
        if not c.get("filtered")
    ][: args.top_n]
    log.line(f"[eval] 候选可交易 {len(actionable)} 只（池内共 {len(built['candidates'])}）")
    if not actionable:
        log.line("[FATAL] 无可交易候选，换 --date 或检查过滤")
        log.close()
        return 1

    names = _lookup_names([c["instrument"] for c in actionable])
    market_notes = _market_notes()
    log.line(f"[eval] 市场背景 {len(market_notes)} 条")

    # Pass1: 本地（强制 peak，严格档）
    pass1_rows: list[dict] = []
    pass1_traces: list[dict] = []
    pass1_cands: list[dict] = []
    for c in actionable:
        name = names.get(c["instrument"], "") or ""
        if "ST" in name.upper():
            log.line(f"  [SKIP] {c['instrument']} ST")
            continue
        row, trace = run_one(
            c, name, market_notes, "peak", "pass1_local", out_dir, log,
            gate_tier="strict",
        )
        pass1_rows.append(row)
        pass1_traces.append(trace)
        pass1_cands.append(c)

    pass1_rows, pass1_traces, gate_info = A.apply_gate_fallback(
        pass1_cands, names, pass1_rows, pass1_traces, force_llm="peak",
    )
    if gate_info.get("fallback_used"):
        log.line(
            f"[gate] 严格档 predict=0，降档至 standard；"
            f"最终 predict={gate_info.get('n_predict_final')}"
        )
        for row, trace in zip(pass1_rows, pass1_traces):
            if row.get("gate_fallback"):
                inst = row["instrument"]
                trace_path = out_dir / "traces" / f"{inst}_pass1_local.json"
                trace_path.write_text(
                    json.dumps({**trace, "summary_row": row}, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                log.line(
                    f"  [gate] {inst} → {row['prediction'].get('action')} "
                    f"conf={row['prediction'].get('confidence')}"
                )

    applied_tier = gate_info.get("applied_tier", "strict")

    pass1_path = out_dir / f"pass1_local_top{args.top_n}.json"
    pass1_path.write_text(
        json.dumps({
            "date": day,
            "pass": "local_peak",
            "top_n": args.top_n,
            "market_notes": market_notes,
            "gate": gate_info,
            "rows": pass1_rows,
        }, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    log.line(f"[OK] Pass1 写入 {pass1_path}")

    # Top5 by swing_score from pass1
    ranked = sorted(
        pass1_rows,
        key=lambda r: -float((r.get("prediction") or {}).get("swing_score") or 0),
    )
    refine_insts = [r["instrument"] for r in ranked[: args.refine_n]]
    cand_map = {c["instrument"]: c for c in actionable}
    log.line(f"[eval] Pass2 复跑: {', '.join(refine_insts)}")

    pass2_rows: list[dict] = []
    for inst in refine_insts:
        c = cand_map.get(inst)
        if not c:
            continue
        name = names.get(inst, "") or ""
        row, _trace = run_one(
            c, name, market_notes, "offpeak", "pass2_deepseek", out_dir, log,
            gate_tier=applied_tier,
        )
        pass2_rows.append(row)

    pass2_path = out_dir / f"pass2_deepseek_top{args.refine_n}.json"
    pass2_path.write_text(
        json.dumps({
            "date": day,
            "pass": "deepseek_offpeak",
            "refine_n": args.refine_n,
            "instruments": refine_insts,
            "gate": gate_info,
            "rows": pass2_rows,
        }, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    log.line(f"[OK] Pass2 写入 {pass2_path}")

    cmp_path = out_dir / "comparison.md"
    write_comparison(day, pass1_rows, pass2_rows, cmp_path, gate_info=gate_info)
    log.line(f"[OK] 对比报告 → {cmp_path}")
    try:
        pub = publish_eval_predictions(day, out_dir, built, pass1_rows, pass2_rows, gate_info)
        log.line(f"[OK] 看板预测已同步 → {pub}")
    except Exception as e:  # noqa: BLE001
        log.line(f"[WARN] 看板预测同步失败: {e}")
    log.line("[DONE] swing_eval 完成")
    log.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
