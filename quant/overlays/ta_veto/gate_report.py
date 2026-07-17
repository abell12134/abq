"""TA 否决质量验收：被否决买入 vs 通过组的事后持有期超额。

对齐 UMP「否决组应更差」的验收口径。默认 horizon=10（与 hold 标签同量级）。

用法：
    python overlays/ta_veto/gate_report.py
    python overlays/ta_veto/gate_report.py --horizon 10 --min-days 40
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

QUANT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(QUANT))
sys.path.insert(0, str(QUANT / "ops"))

from overlays.ta_veto.schema import read_veto_file, veto_dir  # noqa: E402

REPORTS = QUANT / "data" / "reports"
DEFAULT_CFG = QUANT / "configs" / "global.yaml"


def _init_qlib():
    import sys
    import qlib

    sys.path.insert(0, str(QUANT / "ops"))
    from ensure_qlib_data import resolve_provider_uri

    cfg = yaml.safe_load(DEFAULT_CFG.read_text())
    qlib.init(provider_uri=resolve_provider_uri(cfg["paths"]["qlib_data"]), region="cn")


def _forward_excess(instruments: list[str], day: str, horizon: int) -> pd.Series:
    """信号日收盘 → horizon 个交易日后的超额（相对配置基准）。"""
    if not instruments:
        return pd.Series(dtype=float)
    _init_qlib()
    from qlib.data import D

    cfg = yaml.safe_load(DEFAULT_CFG.read_text())
    bench = cfg.get("universe", {}).get("benchmark", "SH000985")

    cal = list(D.calendar(start_time=day, end_time="2099-12-31", freq="day"))
    cal = [pd.Timestamp(c).strftime("%Y-%m-%d") for c in cal]
    if day not in cal:
        return pd.Series(dtype=float)
    i0 = cal.index(day)
    if i0 + horizon >= len(cal):
        return pd.Series(dtype=float)
    end = cal[i0 + horizon]
    px = D.features(instruments + [bench], ["$close"], start_time=day, end_time=end)
    if px is None or px.empty:
        return pd.Series(dtype=float)
    close = px["$close"].unstack("instrument")
    if day not in close.index or end not in close.index:
        # qlib index may be Timestamp
        close.index = pd.to_datetime(close.index)
        d0, d1 = pd.Timestamp(day), pd.Timestamp(end)
        if d0 not in close.index or d1 not in close.index:
            return pd.Series(dtype=float)
        row0, row1 = close.loc[d0], close.loc[d1]
    else:
        row0, row1 = close.loc[day], close.loc[end]
    if bench not in row0.index or bench not in row1.index:
        return pd.Series(dtype=float)
    bret = float(row1[bench] / row0[bench] - 1)
    out = {}
    for inst in instruments:
        if inst not in row0.index or inst not in row1.index:
            continue
        if pd.isna(row0[inst]) or pd.isna(row1[inst]) or float(row0[inst]) <= 0:
            continue
        out[inst] = float(row1[inst] / row0[inst] - 1) - bret
    return pd.Series(out)


def collect_outcomes(horizon: int) -> pd.DataFrame:
    rows = []
    for f in sorted(veto_dir().glob("????-??-??.json")):
        vf = read_veto_file(f.stem)
        if vf is None or vf.status != "ok" or not vf.candidates:
            continue
        veto_set = set(vf.vetoed)
        passed = [c for c in vf.candidates if c not in veto_set]
        try:
            ex_v = _forward_excess(sorted(veto_set), vf.date, horizon) if veto_set else pd.Series()
            ex_p = _forward_excess(passed, vf.date, horizon) if passed else pd.Series()
        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] {vf.date} forward excess failed: {exc}")
            continue
        for inst, ex in ex_v.items():
            rows.append({"date": vf.date, "instrument": inst, "group": "vetoed", "excess": ex})
        for inst, ex in ex_p.items():
            rows.append({"date": vf.date, "instrument": inst, "group": "passed", "excess": ex})
    return pd.DataFrame(rows)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--horizon", type=int, default=10)
    p.add_argument("--min-days", type=int, default=40)
    args = p.parse_args()

    files = sorted(veto_dir().glob("????-??-??.json"))
    ok_days = 0
    for f in files:
        vf = read_veto_file(f.stem)
        if vf and vf.status == "ok":
            ok_days += 1

    outcomes = collect_outcomes(args.horizon)
    lines = [
        f"# TA 否决门禁报告 {dt.date.today():%Y-%m-%d}",
        "",
        "## 验收标准（来自设计）",
        f"1. 样本 ≥ {args.min_days} 个交易日（当前 ok 日={ok_days}）",
        "2. 相对对照线：超额 IR 提升 **或** 最大回撤下降，且换手不升高（见 review_ta_overlay）",
        f"3. 否决质量：被否决买入的事后 {args.horizon} 日超额分布差于通过组",
        "4. 稳定性：单日否决率 ≤30%；LLM 失败 fail-open",
        "5. 未过门禁前 **不修改** `live_manual_10k.use_ta_veto`",
        "",
    ]

    quality_pass = None
    if outcomes.empty:
        lines += [
            "## 否决事后超额",
            "暂无足够 veto 文件或前向收益尚未到期。请先跑影子线 evening/postclose。",
            "",
            f"## 门禁状态: **PENDING**（样本 {ok_days}/{args.min_days}）",
        ]
        overall = "PENDING"
    else:
        g = outcomes.groupby("group")["excess"]
        stats = g.agg(["count", "mean", "median"]).reindex(["vetoed", "passed"])
        lines += [
            "## 否决事后超额",
            "| group | count | mean | median |",
            "|---|---:|---:|---:|",
        ]
        for idx, row in stats.iterrows():
            if pd.isna(row["count"]) or row["count"] == 0:
                lines.append(f"| {idx} | 0 | - | - |")
            else:
                lines.append(
                    f"| {idx} | {int(row['count'])} | {row['mean']:+.2%} | {row['median']:+.2%} |"
                )
        lines.append("")
        v_mean = float(stats.loc["vetoed", "mean"]) if "vetoed" in stats.index and stats.loc["vetoed", "count"] > 0 else np.nan
        p_mean = float(stats.loc["passed", "mean"]) if "passed" in stats.index and stats.loc["passed", "count"] > 0 else np.nan
        if np.isnan(v_mean) or np.isnan(p_mean):
            quality_pass = False
            lines.append("- 否决/通过组样本不足，质量门禁未通过。")
        else:
            quality_pass = v_mean < p_mean
            lines.append(
                f"- 否决组均值超额 {v_mean:+.2%} vs 通过组 {p_mean:+.2%} → "
                f"{'PASS' if quality_pass else 'FAIL'}（期望否决组更差）"
            )
        if ok_days < args.min_days:
            overall = "PENDING"
            lines.append(f"\n## 门禁状态: **PENDING**（样本 {ok_days}/{args.min_days}）")
        else:
            overall = "PASS" if quality_pass else "FAIL"
            lines.append(f"\n## 门禁状态: **{overall}**")

    lines += [
        "",
        "## 运行提示",
        "```bash",
        "python ops/run_daily.py --stage evening --account shadow_ctrl_sim --skip-data",
        "python ops/run_daily.py --stage evening --account shadow_ta_sim --skip-data",
        "python ops/run_daily.py --stage postclose --account shadow_ctrl_sim",
        "python ops/run_daily.py --stage postclose --account shadow_ta_sim",
        "python ops/review_ta_overlay.py",
        "python overlays/ta_veto/gate_report.py",
        "```",
    ]

    REPORTS.mkdir(parents=True, exist_ok=True)
    out = REPORTS / f"ta_veto_gate_{dt.date.today():%Y-%m-%d}.md"
    out.write_text("\n".join(lines) + "\n")
    meta = {
        "date": dt.date.today().isoformat(),
        "ok_days": ok_days,
        "min_days": args.min_days,
        "horizon": args.horizon,
        "quality_pass": quality_pass,
        "overall": overall,
        "n_outcome_rows": int(len(outcomes)),
    }
    (REPORTS / f"ta_veto_gate_{dt.date.today():%Y-%m-%d}.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n"
    )
    print("\n".join(lines))
    print(f"\n[OK] {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
