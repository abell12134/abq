"""阶段4 日报（收盘后）：净值、当日盈亏、持仓、换手、与回测预期的跟踪偏差、告警摘要。

跟踪偏差口径：实盘累计年化超额 vs 回测预期年化超额（qlib 净超额约 +9.3%、
backtrader 复演约 +5.7~7%）。验收门槛"年化跟踪偏差 ≤2pct"需累计足够交易日
（≥20）后才有统计意义；样本不足时如实标注。

用法：
    python daily_report.py --day 2026-06-11
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd

QUANT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(QUANT / "contracts"))
sys.path.insert(0, str(QUANT / "ops"))
import common as C  # noqa: E402
import schemas as S  # noqa: E402

ANN = 244  # 年化交易日数
BACKTEST_ANN_EXCESS = 0.093  # 阶段1 qlib 净超额年化（跟踪对照基准）


def paths(account: str | None) -> dict[str, Path]:
    d = C.ensure_account_dirs(account)
    return {
        "daily": d["nav"] / "daily.csv",
        "holdings": d["nav"] / "holdings.csv",
        "reports": d["reports"],
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--day", default=None)
    p.add_argument("--account", default=None)
    args = p.parse_args()
    day = args.day or C.latest_trading_day()
    pth = paths(args.account)

    acc = C.load_account(args.account)
    if acc is None or not pth["daily"].exists():
        C.alert("WARN", "无账户或净值数据，无法出日报", day)
        return 1
    d = S.read_csv("daily", pth["daily"]).sort_values("date").reset_index(drop=True)
    if day not in set(d["date"]):
        C.alert("WARN", f"daily.csv 无 {day} 记录，请先 compute_nav", day)
        return 1
    cur = d[d["date"] == day].iloc[0]
    start_cap = float(acc["start_capital"])

    n = len(d)
    cum_ret = float(cur["nav"]) / start_cap - 1
    cum_excess = float(np.prod(1 + d["excess_ret"].values) - 1)
    ann_factor = ANN / n if n else 0
    ann_excess = (1 + cum_excess) ** ann_factor - 1 if n else float("nan")
    te = float(d["excess_ret"].std() * np.sqrt(ANN)) if n > 1 else float("nan")
    gap = abs(ann_excess - BACKTEST_ANN_EXCESS) if ann_excess == ann_excess else float("nan")
    enough = n >= 20

    hold = S.read_csv("holdings", pth["holdings"]) if pth["holdings"].exists() else pd.DataFrame()
    top = ""
    if len(hold):
        hv = hold.assign(mv=hold["shares"] * hold["last_price"]).sort_values(
            "mv", ascending=False).head(10)
        tot = hv["mv"].sum() or 1
        top = "\n".join(f"| {r.instrument} | {int(r.shares)} | {r.last_price:.2f} | "
                        f"{r.mv/float(cur['nav'])*100:.1f}% |" for r in hv.itertuples())

    alog = C.ALERT_LOG
    today_alerts = []
    if alog.exists():
        for ln in alog.read_text().splitlines():
            if day not in ln or " INFO " in ln:
                continue
            if "净值未计算" in ln:
                continue
            if args.account and f"[{args.account}]" not in ln:
                continue
            today_alerts.append(ln)
    today_alerts = today_alerts[-10:]

    L = [
        f"# 每日运营日报 {day}" + (f"（{args.account}）" if args.account else ""),
        "",
        "> ## ⚠ 风险声明（必读）",
        ">",
        "> **本日报为量化研究 / 学习用途，不构成任何投资建议。**  ",
        "> **股市有风险，谨慎操作；据此交易的一切后果由使用者自行承担。**  ",
        "> 历史回测、模拟与纸面表现均 **不代表** 未来收益。",
        "",
        f"- 生成: {dt.datetime.now():%Y-%m-%d %H:%M}　运行天数: {n}",
        "",
        "## 净值与盈亏",
        f"- 组合净值: **{float(cur['nav']):,.2f}**（现金 {float(cur['cash']):,.2f} + "
        f"持仓 {float(cur['position_value']):,.2f}，{int(cur['n_pos'])} 只）",
        f"- 当日收益 {float(cur['daily_ret']):+.2%}　基准 {float(cur['bench_ret']):+.2%}　"
        f"超额 {float(cur['excess_ret']):+.2%}　换手 {float(cur['turnover']):.1%}",
        f"- 累计收益 {cum_ret:+.2%}　累计超额 {cum_excess:+.2%}",
        "",
        "## 与回测预期的跟踪偏差（验收门槛：年化偏差 ≤ 2pct）",
        f"- 实盘年化超额(累计折算) {ann_excess:+.2%}　回测预期 +{BACKTEST_ANN_EXCESS:.1%}　"
        f"偏差 {gap:.2%}" if ann_excess == ann_excess else "- 样本不足，暂不折算年化",
        f"- 超额跟踪误差(年化) {te:.2%}",
        f"- 验收判定: {'达标' if (enough and gap == gap and gap <= 0.02) else ('样本不足(需≥20日)' if not enough else '偏差超 2pct，需复核')}",
        "",
        "## 前十大持仓",
    ]
    if top:
        L += ["| 标的 | 股数 | 收盘价 | 占净值 |", "|---|---|---|---|", top]
    else:
        L.append("（空仓）")
    L += ["", "## 当日告警"]
    L += (["```"] + today_alerts + ["```"]) if today_alerts else ["无"]

    pth["reports"].mkdir(parents=True, exist_ok=True)
    out = pth["reports"] / f"daily_{day}.md"
    out.write_text("\n".join(L))
    print("\n".join(L))
    print(f"\n[OK] 日报 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
