"""板块预测编排：特征面板 → 训练/推理 → 发 claim → 结算到期单。

用法：
  python -m overlays.sector_forecast.run_forecast
  python -m overlays.sector_forecast.run_forecast --day 2026-09-07 --force --retrain

接入 evening：run_board 之后 fail-open。不改 orders/。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

QUANT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(QUANT))
sys.path.insert(0, str(QUANT / "ops"))

import common as C  # noqa: E402

from overlays.sector_forecast import emit as E  # noqa: E402
from overlays.sector_forecast import features as F  # noqa: E402
from overlays.sector_forecast import job  # noqa: E402
from overlays.sector_forecast import settle as ST  # noqa: E402
from overlays.sector_forecast import store  # noqa: E402
from overlays.sector_forecast.schema import LOOKBACK_DAYS  # noqa: E402


def run(day: str | None = None, *, force: bool = False, retrain: bool = False,
        progress: bool = False, skip_llm: bool = False,
        force_llm: str | None = None, brief_only: bool = False) -> dict:
    day = day or C.latest_trading_day()
    if progress:
        job.start(f"{day} 板块预测开始")
    if brief_only:
        from overlays.sector_forecast import brief as B
        pred = store.load_prediction(day)
        if not pred:
            raise FileNotFoundError(f"{day} 尚无预测文件，先跑完整 emit")
        if progress:
            job.tick(40, "对已有候选跑 LLM 简报")
        print(f"[brief] {day} 补 LLM 简报（不改排名）")
        B.attach(pred, day, force_llm=force_llm, skip=skip_llm)
        store.save_prediction(day, pred)
        if progress:
            job.finish(True, f"{day} LLM 简报完成", extra={"day": day})
        print(f"      llm={pred.get('llm')}")
        return pred
    if not force:
        old = store.load_prediction(day)
        if old and old.get("status") in {"ok", "partial"}:
            llm_ok = isinstance(old.get("llm"), dict) and old["llm"].get("ok")
            if not llm_ok and not skip_llm:
                from overlays.sector_forecast import brief as B
                if progress:
                    job.tick(50, "数字预测已在，补 LLM 简报")
                print(f"[brief] {day} 已有预测，补 LLM 简报")
                B.attach(old, day, force_llm=force_llm, skip=False)
                store.save_prediction(day, old)
            if progress:
                job.tick(70, "当日预测已存在，仅结算到期单")
            settled = ST.settle_due(day)
            if progress:
                job.finish(True, f"{day} 已存在，结算 {settled.get('updated_claims', 0)} 条")
            print(f"[OK] {day} 预测已存在（--force 覆盖）")
            return old
    try:
        if progress:
            job.tick(15, "拉取 qlib 行业日面板")
        print(f"[1/4] 特征面板 {day} lookback={LOOKBACK_DAYS}")
        panel = F.build_panel(day, lookback=LOOKBACK_DAYS)
        print(f"      {len(panel)} 行 / {panel['industry'].nunique()} 行业 / {panel['day'].nunique()} 日")
        if progress:
            job.tick(45, "训练或加载模型并打分")
        print("[2/4] 发预测")
        pred = E.emit(day, panel, retrain=retrain, skip_llm=skip_llm,
                      force_llm=force_llm)
        n = len(pred.get("claims") or [])
        shadow = pred.get("shadow")
        print(f"      claims={n} shadow={shadow} {pred.get('shadow_reason', '')}")
        if progress:
            job.tick(80, "结算到期预测")
        print("[3/4] 结算到期")
        settled = ST.settle_due(day)
        print(f"      files={settled.get('files')} updated={settled.get('updated_claims')}")
        if progress:
            job.finish(True, f"{day} 完成 · {n} 条 claim · shadow={shadow}",
                       extra={"day": day, "shadow": shadow, "n_claims": n})
        print("[4/4] 完成")
        return pred
    except Exception as e:  # noqa: BLE001
        if progress:
            job.finish(False, f"失败: {e}"[:200])
        print(f"[FATAL] {e}")
        raise


def main() -> int:
    ap = argparse.ArgumentParser(description="板块行业预测（只读 overlay）")
    ap.add_argument("--day", default=None)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--retrain", action="store_true", help="强制重训截面模型")
    ap.add_argument("--progress", action="store_true")
    ap.add_argument("--skip-llm", action="store_true", help="不跑 LLM 简报")
    ap.add_argument("--brief-only", action="store_true",
                    help="只对已有预测补 LLM，不重训不重打分")
    ap.add_argument("--force-llm", default=None,
                    help="peak | nous | offpeak；默认走 overlays 路由")
    args = ap.parse_args()
    try:
        run(args.day, force=args.force, retrain=args.retrain, progress=args.progress,
            skip_llm=args.skip_llm, force_llm=args.force_llm, brief_only=args.brief_only)
        return 0
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
