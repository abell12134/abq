"""研究分析跑批入口：建宇宙 → 逐只研究 → 落盘 → 进账本。

用法：
  python overlays/research/run_research.py --dry-run --instruments SH600282   # 管线联调
  python overlays/research/run_research.py --account live_manual_10k          # 全量三源宇宙
  python overlays/research/run_research.py --instruments SH600282 --force-llm peak
  python overlays/research/run_research.py --date 2026-08-18 --max-llm 5

单只票约 10 次 LLM 调用（4 分析师 + CN 多空判 + EN 多空判）。
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

QUANT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(QUANT))
sys.path.insert(0, str(QUANT / "ops"))

import common as C  # noqa: E402

from overlays.research import analyze as A  # noqa: E402
from overlays.research import job as JOB  # noqa: E402
from overlays.research import ledger_emit as LEDGER  # noqa: E402
from overlays.research import store  # noqa: E402
from overlays.research.universe import build_research_universe  # noqa: E402

TZ = ZoneInfo("Asia/Shanghai")
log = logging.getLogger("research")


def _lookup_names(instruments: list[str]) -> dict[str, str]:
    try:
        from overlays.sentiment_memory.run_memory import _lookup_names as _lk
        return _lk(instruments)
    except Exception:  # noqa: BLE001
        return {i: "" for i in instruments}


def _resolve_day(day: str | None) -> str:
    if day:
        return day
    try:
        return C.latest_trading_day()
    except Exception:  # noqa: BLE001
        return datetime.now(TZ).strftime("%Y-%m-%d")


def run(
    day: str | None = None,
    account: str | None = "live_manual_10k",
    instruments: list[str] | None = None,
    dry_run: bool = False,
    force: bool = False,
    force_llm: str | None = None,
    max_llm: int = 0,
) -> int:
    day = _resolve_day(day)
    force_llm = force_llm or "peak"
    store.ensure_dirs()

    cur = JOB.read_job()
    if cur.get("status") != "running":
        JOB.start_job(account=account, dry_run=dry_run, day=day)
    else:
        JOB.write_job({"day": day, "account": account, "dry_run": dry_run,
                       "force_llm": force_llm})

    print(f"[research] day={day} account={account} dry_run={dry_run} "
          f"force={force} force_llm={force_llm or 'auto'}")

    try:
        return _run_impl(day=day, account=account, instruments=instruments,
                         dry_run=dry_run, force=force, force_llm=force_llm,
                         max_llm=max_llm)
    except Exception as e:  # noqa: BLE001
        JOB.finish_job(ok=False, message=str(e)[:200])
        print(f"[FAIL] {e}", flush=True)
        raise


def _run_impl(
    day: str,
    account: str | None,
    instruments: list[str] | None,
    dry_run: bool,
    force: bool,
    force_llm: str | None,
    max_llm: int,
) -> int:
    # 同日幂等（除非 --force / --dry-run / 显式指定票）
    if not force and not dry_run and not instruments:
        if store.is_done(day):
            print(f"[SKIP] {day} 已跑过研究分析（{day}.done 哨兵存在），加 --force 重跑")
            JOB.finish_job(ok=True, message=f"{day} 已跑过，跳过（--force 可重跑）")
            return 0

    # 构建宇宙
    JOB.set_phase("universe", "构建研究宇宙…", pct=8)
    if instruments:
        from overlays.sentiment_memory.run_memory import normalize_instrument
        uni = []
        for raw in instruments:
            inst = normalize_instrument(raw) or raw.upper()
            uni.append({"instrument": inst, "sources": ["manual"],
                        "swing_action": None, "swing_score": None,
                        "order_side": None, "sentiment_score": None})
    else:
        uni = build_research_universe(day, account=account)
    print(f"[OK] 研究宇宙 {len(uni)} 只", flush=True)
    for e in uni:
        print(f"    · {e['instrument']} sources={','.join(e['sources'])}", flush=True)

    if not uni:
        JOB.finish_job(ok=False, message="研究宇宙为空")
        print("[WARN] 研究宇宙为空")
        return 1

    if int(max_llm) > 0 and not dry_run:
        uni = uni[: int(max_llm)]
        print(f"[OK] 待研究分析 {len(uni)} 只（上限 max_llm={max_llm}）", flush=True)
    else:
        print(f"[OK] 待研究分析 {len(uni)} 只", flush=True)

    JOB.set_llm_total(len(uni))
    if not dry_run:
        try:
            LEDGER.register_strategy()
        except Exception as e:  # noqa: BLE001
            log.info("注册策略失败（账本可能未就绪）: %s", e)

    names = _lookup_names([e["instrument"] for e in uni])
    n_buy = n_sell = n_hold = n_emit = n_fail = 0
    global_cache: dict[str, Any] = {}

    for i, e in enumerate(uni, 1):
        inst = e["instrument"]
        name = names.get(inst, "") or ""
        try:
            report = A.analyze_instrument(
                inst, name, day,
                force_llm=None if dry_run else force_llm,
                dry_run=dry_run, sources=e["sources"], global_cache=global_cache,
            )
        except Exception as ex:  # noqa: BLE001
            report = A.analyze_instrument(
                inst, name, day, dry_run=True, sources=e["sources"])
            report.meta["llm_error"] = True
            report.meta["error"] = str(ex)[:200]
            report.status = "fail_open"
            print(f"  · [{i}/{len(uni)}] {inst} {name}: LLM 异常降级 dry-run | {ex}",
                  flush=True)

        report_dict = report.to_dict()
        path = store.save_report(report_dict)

        action = report.merged_direction
        reason = (report.verdict_cn.summary or report.verdict_en.summary or "")[:80]
        if action == "buy":
            n_buy += 1
        elif action == "sell":
            n_sell += 1
        else:
            n_hold += 1

        # 进账本（hold 不发）
        pred_info: dict[str, Any] = {"emitted": False}
        if not dry_run and action in ("buy", "sell"):
            try:
                pred_info = LEDGER.emit_research_prediction(report_dict)
                if pred_info.get("ok"):
                    n_emit += 1
                    report_dict["pred_id"] = pred_info["pred_id"]
                    store.save_report(report_dict)  # 回填 pred_id
            except Exception as ex:  # noqa: BLE001
                pred_info = {"emitted": False, "error": str(ex)[:120]}
                n_fail += 1
                log.info("账本写入失败 %s: %s", inst, ex)

        pred_tag = pred_info.get("pred_id") or ("—" if dry_run else "skip")
        print(f"  · [{i}/{len(uni)}] {inst} {name}: action={action} "
              f"conf={report.merged_confidence} consensus={report.consensus} "
              f"| pred={pred_tag} | {reason}", flush=True)
        try:
            JOB.tick_llm(i, len(uni), instrument=inst, name=name,
                         action=action, reason=reason)
        except Exception:  # noqa: BLE001
            pass

    if not dry_run:
        store.mark_done(day)

    msg = (f"完成：buy {n_buy} / sell {n_sell} / hold {n_hold}"
           f"（入账 {n_emit}，失败 {n_fail}）")
    JOB.write_job({"n_buy": n_buy, "n_sell": n_sell, "n_hold": n_hold,
                   "done_count": len(uni), "total": len(uni)})
    print(f"[DONE] {msg}", flush=True)
    JOB.finish_job(ok=True, message=msg)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="研究分析：4 分析师 + 中英双辩论 + 评判 → 可结算预测")
    p.add_argument("--date", default=None)
    p.add_argument("--account", default="live_manual_10k")
    p.add_argument("--instruments", nargs="*", default=None,
                   help="显式指定标的（跳过宇宙构建）；多个空格分隔")
    p.add_argument("--dry-run", action="store_true", help="不调 LLM，规则兜底")
    p.add_argument("--force", action="store_true", help="忽略同日哨兵，强制重跑")
    p.add_argument("--force-llm", default="peak", choices=["peak", "offpeak"],
                   help="LLM 路由：peak=本地自部署；offpeak=DeepSeek")
    p.add_argument("--max-llm", type=int, default=0,
                   help="研究上限；0=全部宇宙（默认）")
    args = p.parse_args()
    account = (args.account or "").strip() or None
    return run(day=args.date, account=account, instruments=args.instruments,
               dry_run=args.dry_run, force=args.force,
               force_llm=args.force_llm, max_llm=args.max_llm)


if __name__ == "__main__":
    raise SystemExit(main())
