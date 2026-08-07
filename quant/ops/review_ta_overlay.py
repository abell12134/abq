"""TA 否决影子线 vs 对照线 A/B 复盘 + 门禁判定。

用法：
    python ops/review_ta_overlay.py
    python ops/review_ta_overlay.py --ta shadow_ta_sim --control shadow_ctrl_sim
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

QUANT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(QUANT / "contracts"))
sys.path.insert(0, str(QUANT / "ops"))
sys.path.insert(0, str(QUANT))
import common as C  # noqa: E402
import schemas as S  # noqa: E402
from overlays.ta_veto.schema import read_veto_file, veto_dir  # noqa: E402

REPORTS = QUANT / "data" / "reports"


def load_daily(account: str) -> pd.DataFrame:
    f = C.ensure_account_dirs(account)["nav"] / "daily.csv"
    if not f.exists():
        return pd.DataFrame(columns=list(S.SCHEMAS["daily"]))
    return S.read_csv("daily", f).sort_values("date").reset_index(drop=True)


def summary(account: str) -> dict:
    d = load_daily(account)
    acc = C.load_account(account) or {}
    if d.empty:
        return {"account": account, "days": 0}
    start = float(acc.get("start_capital", d.iloc[0]["nav"]))
    last = d.iloc[-1]
    rets = d["excess_ret"].astype(float)
    ir = float(rets.mean() / rets.std() * np.sqrt(252)) if rets.std() > 1e-12 else 0.0
    nav = d["nav"].astype(float)
    peak = nav.cummax()
    max_dd = float(((nav / peak) - 1).min())
    return {
        "account": account,
        "days": len(d),
        "start": start,
        "nav": float(last["nav"]),
        "cum_ret": C.twr_cum_return(d["daily_ret"]),
        "cum_excess": C.twr_cum_return(d["excess_ret"]),
        "excess_ir": ir,
        "max_dd": max_dd,
        "turnover": float(d["turnover"].mean()),
        "avg_pos": float(d["n_pos"].mean()),
        "cash_ratio": float((d["cash"] / d["nav"]).mean()),
    }


def veto_stats() -> dict:
    rows = []
    for f in sorted(veto_dir().glob("????-??-??.json")):
        vf = read_veto_file(f.stem)
        if vf is None:
            continue
        n_cand = len(vf.candidates)
        n_veto = len(vf.vetoed) if vf.status == "ok" else 0
        rows.append({
            "date": vf.date,
            "status": vf.status,
            "n_cand": n_cand,
            "n_veto": n_veto,
            "veto_rate": (n_veto / n_cand) if n_cand else 0.0,
            "fail_open": vf.status != "ok",
        })
    if not rows:
        return {"days": 0, "avg_veto_rate": 0.0, "fail_open_days": 0, "max_veto_rate": 0.0}
    df = pd.DataFrame(rows)
    ok = df[df["status"] == "ok"]
    return {
        "days": int(len(df)),
        "ok_days": int(len(ok)),
        "fail_open_days": int(df["fail_open"].sum()),
        "avg_veto_rate": float(ok["veto_rate"].mean()) if len(ok) else 0.0,
        "max_veto_rate": float(ok["veto_rate"].max()) if len(ok) else 0.0,
        "total_vetoes": int(ok["n_veto"].sum()) if len(ok) else 0,
    }


def gate_check(ta: dict, ctrl: dict, vs: dict, min_days: int = 40) -> dict:
    """门禁：样本天数、IR 或回撤、换手、否决率。"""
    reasons: list[str] = []
    ok = True
    if ta.get("days", 0) < min_days or ctrl.get("days", 0) < min_days:
        ok = False
        reasons.append(
            f"样本不足：ta={ta.get('days', 0)} ctrl={ctrl.get('days', 0)}（需≥{min_days}）"
        )
    else:
        better_ir = ta["excess_ir"] > ctrl["excess_ir"] + 1e-6
        better_dd = ta["max_dd"] > ctrl["max_dd"] + 1e-6  # max_dd 为负，更大=回撤更浅
        not_worse_ret = ta["cum_excess"] + 1e-6 >= ctrl["cum_excess"]
        if not ((better_ir or better_dd) and not_worse_ret):
            ok = False
            reasons.append(
                f"业绩未过门：IR {ta['excess_ir']:.3f} vs {ctrl['excess_ir']:.3f}；"
                f"回撤 {ta['max_dd']:.2%} vs {ctrl['max_dd']:.2%}；"
                f"累计超额 {ta['cum_excess']:.2%} vs {ctrl['cum_excess']:.2%}"
            )
        if ta["turnover"] > ctrl["turnover"] * 1.05 + 1e-9:
            ok = False
            reasons.append(
                f"换手升高：{ta['turnover']:.2%} > 对照 {ctrl['turnover']:.2%}×1.05"
            )
    if vs.get("ok_days", 0) and vs.get("max_veto_rate", 0) > 0.30 + 1e-9:
        ok = False
        reasons.append(f"单日否决率过高：max={vs['max_veto_rate']:.1%}（阈值30%）")
    if not reasons and ok:
        reasons.append("全部主门禁通过（否决事后10日超额需另跑 gate_report 补验）")
    return {"passed": ok, "reasons": reasons}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--ta", default="shadow_ta_sim")
    p.add_argument("--control", default="shadow_ctrl_sim")
    p.add_argument("--min-days", type=int, default=None)
    args = p.parse_args()

    cfg = C.account_config(args.ta)
    min_days = args.min_days or int(
        cfg.get("review", {}).get("gate_min_days")
        or cfg.get("account", {}).get("review_min_days")
        or 40
    )

    st, sc = summary(args.ta), summary(args.control)
    vs = veto_stats()
    gate = gate_check(st, sc, vs, min_days=min_days)

    lines = [
        f"# TA 否决层 A/B 复盘 {dt.date.today():%Y-%m-%d}",
        "",
        "> ## ⚠ 风险声明（必读）",
        ">",
        "> **本报告为量化研究 / 学习用途，不构成任何投资建议。**  ",
        "> **股市有风险，谨慎操作；据此交易的一切后果由使用者自行承担。**  ",
        "> 历史回测、模拟与纸面表现均 **不代表** 未来收益。",
        "",
        f"- TA 影子线: `{args.ta}`",
        f"- 对照线: `{args.control}`",
        f"- 门禁最短样本: {min_days} 交易日",
        "",
        "## 总览",
        "| 账户 | 天数 | 净值 | 累计收益 | 累计超额 | 超额IR | 最大回撤 | 换手 | 持仓 | 现金比 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for s in (st, sc):
        if not s.get("days"):
            lines.append(f"| {s['account']} | 0 | - | - | - | - | - | - | - | - |")
            continue
        lines.append(
            f"| {s['account']} | {s['days']} | {s['nav']:,.2f} | {s['cum_ret']:+.2%} | "
            f"{s['cum_excess']:+.2%} | {s['excess_ir']:.3f} | {s['max_dd']:.2%} | "
            f"{s['turnover']:.1%} | {s['avg_pos']:.1f} | {s['cash_ratio']:.1%} |"
        )

    lines += [
        "",
        "## 否决层运行统计",
        f"- veto 文件天数: {vs.get('days', 0)}（ok={vs.get('ok_days', 0)}，"
        f"fail_open={vs.get('fail_open_days', 0)}）",
        f"- 生效否决总数: {vs.get('total_vetoes', 0)}",
        f"- 平均否决率: {vs.get('avg_veto_rate', 0):.1%}；"
        f"单日最大否决率: {vs.get('max_veto_rate', 0):.1%}",
        "",
        "## 门禁判定",
        f"- **结果: {'PASS' if gate['passed'] else 'FAIL'}**",
    ]
    for r in gate["reasons"]:
        lines.append(f"- {r}")
    lines += [
        "",
        "## 上线纪律",
        "- 未 PASS 前禁止修改 `live_manual_10k` 的 `use_ta_veto`。",
        "- 关闭方式：账户配置 `execution.use_ta_veto: false`，系统回退 LGBM+UMP。",
    ]

    REPORTS.mkdir(parents=True, exist_ok=True)
    out = REPORTS / f"ta_overlay_ab_{dt.date.today():%Y-%m-%d}.md"
    out.write_text("\n".join(lines) + "\n")
    # 机器可读门禁
    gate_json = REPORTS / f"ta_overlay_gate_{dt.date.today():%Y-%m-%d}.json"
    gate_json.write_text(json.dumps({
        "date": dt.date.today().isoformat(),
        "ta": args.ta,
        "control": args.control,
        "min_days": min_days,
        "ta_summary": st,
        "control_summary": sc,
        "veto_stats": vs,
        "gate": gate,
    }, ensure_ascii=False, indent=2) + "\n")

    print("\n".join(lines))
    print(f"\n[OK] 报告 {out}")
    print(f"[OK] 门禁 JSON {gate_json}")
    return 0 if gate["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
