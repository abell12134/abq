"""纸面跟踪：对 paper_tracking 因子滚最近窗口 Rank IC，达标可晋升 live。

方案口径（设计实现方案 §3.3 关卡5）：
  - 入库后先跟踪 1~3 个月，线上 IC 达标才参与实盘模型；
  - 本脚本默认只出报告；加 --promote 才写 factors.yaml status=live。

晋升门禁（可调）：
  1) 进入 paper_tracking 已满 min_calendar_days（默认 40 日 ≈ 2 个月）；
  2) 最近 lookback 交易日 Rank IC 与入库时 oos_rank_ic 同号；
  3) |recent_rank_ic| ≥ min_abs_ic（默认 0.01）。

用法：
    python track_paper.py                  # 只评估
    python track_paper.py --promote        # 达标者晋升 live
    python track_paper.py --min-days 60 --lookback 40
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
QUANT = HERE.parent
sys.path.insert(0, str(HERE))
import factor_lib as FL  # noqa: E402
from evaluate import Evaluator  # noqa: E402

REPORTS = QUANT / "data" / "reports"


def _parse_ts(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(str(s)[:16], fmt)
        except ValueError:
            continue
    return None


def evaluate_one(ev: Evaluator, name: str, fac: dict, lookback_days: int,
                 as_of: str) -> dict:
    expr = fac["expr"]
    f = ev.factor(expr)
    # 最近 lookback 个交易日窗口（按因子索引上的 datetime）
    dates = sorted(pd.Timestamp(x).strftime("%Y-%m-%d")
                   for x in f.index.get_level_values("datetime").unique())
    dates = [d for d in dates if d <= as_of]
    if len(dates) < max(10, lookback_days // 2):
        return {"name": name, "ok": False, "reason": "近期样本不足",
                "recent_rank_ic": float("nan"), "recent_icir": float("nan")}
    win = dates[-lookback_days:]
    period = (win[0], win[-1])
    ric, icir = ev.rank_ic(f, period)
    oos = float((fac.get("metrics") or {}).get("oos_rank_ic") or float("nan"))
    entered = _parse_ts(fac.get("updated_at") or fac.get("paper_since"))
    cal_days = (dt.datetime.strptime(as_of, "%Y-%m-%d") - entered).days if entered else 0
    return {
        "name": name,
        "expr": expr,
        "category": fac.get("category", ""),
        "paper_since": fac.get("updated_at", ""),
        "calendar_days": cal_days,
        "window": f"{period[0]}~{period[1]}",
        "recent_rank_ic": ric,
        "recent_icir": icir,
        "oos_rank_ic": oos,
        "ok": True,
        "reason": "",
    }


def pass_gates(row: dict, min_days: int, min_abs_ic: float) -> tuple[bool, str]:
    if not row.get("ok"):
        return False, row.get("reason") or "评估失败"
    if row["calendar_days"] < min_days:
        return False, f"纸面期仅 {row['calendar_days']} 日 < {min_days}"
    ric, oos = row["recent_rank_ic"], row["oos_rank_ic"]
    if ric != ric or oos != oos:
        return False, "IC 缺失"
    if ric * oos <= 0:
        return False, f"近期 IC 变号（recent={ric:+.4f}, oos={oos:+.4f}）"
    if abs(ric) < min_abs_ic:
        return False, f"|recent IC|={abs(ric):.4f} < {min_abs_ic}"
    return True, "达标"


def render_report(rows: list[dict], promoted: list[str]) -> str:
    lines = [
        f"# 纸面跟踪报告 {dt.date.today():%Y-%m-%d}",
        "",
        "| 因子 | 纸面天数 | 窗口 | 近期 RankIC | 近期 ICIR | 入库 OOS IC | 门禁 |",
        "|---|---:|---|---:|---:|---:|---|",
    ]
    for r in rows:
        gate = r.get("gate_reason", "")
        mark = "PASS" if r.get("gate_pass") else "HOLD"
        lines.append(
            f"| {r['name']} | {r.get('calendar_days', 0)} | {r.get('window', '-')} | "
            f"{r.get('recent_rank_ic', float('nan')):+.4f} | "
            f"{r.get('recent_icir', float('nan')):+.3f} | "
            f"{r.get('oos_rank_ic', float('nan')):+.4f} | {mark}: {gate} |"
        )
    lines += ["", f"本次晋升 live: {', '.join(promoted) if promoted else '（无）'}",
              "",
              "> 晋升后须跑 research/rolling_retrain.py 或 run_baseline.py，"
              "用 Alpha158PlusLab 重训并过 IR≥0.8 才切换线上模型。"]
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--promote", action="store_true", help="达标者写入 status=live")
    p.add_argument("--min-days", type=int, default=40, help="最短纸面日历天数")
    p.add_argument("--lookback", type=int, default=40, help="近期 Rank IC 交易日窗口")
    p.add_argument("--min-abs-ic", type=float, default=0.01)
    p.add_argument("--as-of", default=None, help="IC 评估截止日，默认 Qlib 最新交易日")
    args = p.parse_args()

    lib = FL.load_lib()
    paper = FL.by_status(lib, "paper_tracking")
    if not paper:
        print("[OK] 无 paper_tracking 因子")
        return 0

    # IC 窗口跟数据最新日；纸面天数跟「今天」——避免 Evaluator 旧 oos_end 把天数算成负数
    import qlib
    from qlib.data import D
    sys.path.insert(0, str(QUANT / "ops"))
    from ensure_qlib_data import resolve_provider_uri
    qlib.init(provider_uri=resolve_provider_uri("datasets/qlib_data/cn_data"), region="cn")
    cal_last = pd.Timestamp(list(D.calendar(freq="day"))[-1]).strftime("%Y-%m-%d")
    as_of = args.as_of or cal_last
    today = dt.date.today().strftime("%Y-%m-%d")
    print(f"[OK] IC as_of={as_of} 纸面计日截至 today={today}")

    ev = Evaluator(oos_end=as_of)

    rows, promoted = [], []
    for name, fac in paper.items():
        row = evaluate_one(ev, name, fac, args.lookback, as_of)
        # 纸面天数：入库日 → 今天（跟踪时长），不跟数据截止日绑死
        entered = _parse_ts(fac.get("updated_at") or fac.get("paper_since"))
        row["calendar_days"] = (
            (dt.datetime.strptime(today, "%Y-%m-%d") - entered).days if entered else 0
        )
        ok, reason = pass_gates(row, args.min_days, args.min_abs_ic)
        row["gate_pass"], row["gate_reason"] = ok, reason
        rows.append(row)
        print(f"[{'PASS' if ok else 'HOLD'}] {name}: {reason} "
              f"(recent IC={row.get('recent_rank_ic', float('nan')):+.4f}, "
              f"days={row.get('calendar_days', 0)})")
        if ok and args.promote:
            fac["status"] = "live"
            fac["reject_reason"] = "纸面跟踪达标，晋升 live"
            fac["paper_metrics"] = {
                "recent_rank_ic": float(row["recent_rank_ic"]),
                "recent_icir": float(row["recent_icir"]) if row["recent_icir"] == row["recent_icir"] else None,
                "window": row["window"],
                "promoted_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
            }
            fac["updated_at"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
            lib["discovered"][name] = fac
            promoted.append(name)

    if promoted:
        FL.save_lib(lib)
        print(f"[OK] 已晋升 live: {', '.join(promoted)}")

    REPORTS.mkdir(parents=True, exist_ok=True)
    path = REPORTS / f"paper_track_{dt.date.today():%Y%m%d}.md"
    path.write_text(render_report(rows, promoted))
    print(f"[OK] 报告 {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
