"""双线复盘：研究模拟线 vs 实盘线。

输出：
  - 两条线的净值、累计收益/超额、持仓数、现金闲置、换手、费用；
  - 共同交易日的收益差；
  - 可匹配成交的价格差（实盘相对研究模拟线）。

用法：
    python ops/review_accounts.py
    python ops/review_accounts.py --research research_sim_100k --live live_manual_10k
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

REPORTS = QUANT / "data" / "reports"


def account_files(account: str) -> dict[str, Path]:
    d = C.ensure_account_dirs(account)
    return {
        "daily": d["nav"] / "daily.csv",
        "fills": d["fills"],
        "reports": d["reports"],
    }


def load_daily(account: str) -> pd.DataFrame:
    f = account_files(account)["daily"]
    if not f.exists():
        return pd.DataFrame(columns=list(S.SCHEMAS["daily"]))
    return S.read_csv("daily", f).sort_values("date").reset_index(drop=True)


def load_fills(account: str) -> pd.DataFrame:
    fills_dir = account_files(account)["fills"]
    rows = []
    for f in sorted(fills_dir.glob("????-??-??.csv")):
        if not f.with_suffix(".done").exists():
            continue
        try:
            df = S.read_csv("fills", f)
        except Exception:
            continue
        if df.empty:
            continue
        df["date"] = f.stem
        rows.append(df)
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(
        columns=["date", "instrument", "side", "shares", "price", "amount", "fee"])


def summary(account: str) -> dict:
    d = load_daily(account)
    fills = load_fills(account)
    acc = C.load_account(account) or {}
    if d.empty:
        return {"account": account, "days": 0}
    start = float(acc.get("start_capital", d.iloc[0]["nav"]))
    last = d.iloc[-1]
    cum_ret = C.twr_cum_return(d["daily_ret"])
    cum_excess = C.twr_cum_return(d["excess_ret"])
    fee = float(fills["fee"].sum()) if len(fills) else 0.0
    traded = float(fills["amount"].sum()) if len(fills) else 0.0
    return {
        "account": account,
        "days": len(d),
        "start": start,
        "nav": float(last["nav"]),
        "cum_ret": cum_ret,
        "cum_excess": cum_excess,
        "avg_pos": float(d["n_pos"].mean()),
        "cash_ratio": float((d["cash"] / d["nav"]).mean()),
        "turnover": float(d["turnover"].mean()),
        "fee": fee,
        "traded": traded,
        "fee_ratio": fee / traded if traded else 0.0,
    }


def fill_diff(research: str, live: str) -> pd.DataFrame:
    r, l = load_fills(research), load_fills(live)
    if r.empty or l.empty:
        return pd.DataFrame()
    key = ["date", "instrument", "side"]
    rr = r.groupby(key).agg(research_price=("price", "mean"),
                            research_shares=("shares", "sum")).reset_index()
    ll = l.groupby(key).agg(live_price=("price", "mean"),
                            live_shares=("shares", "sum")).reset_index()
    m = rr.merge(ll, on=key, how="inner")
    if m.empty:
        return m
    sign = m["side"].str.upper().map({"BUY": 1, "SELL": -1}).fillna(1)
    # 正数表示实盘更差：买得更贵或卖得更便宜。
    m["adverse_slip_pct"] = sign * (m["live_price"] / m["research_price"] - 1) * 100
    return m


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--research", default="research_sim_100k")
    p.add_argument("--live", default="live_manual_10k")
    p.add_argument("--ta", default=None,
                   help="若指定，额外输出 TA 影子线摘要（默认 shadow_ta_sim 可手填）")
    p.add_argument("--ta-control", default="shadow_ctrl_sim")
    args = p.parse_args()

    sr, sl = summary(args.research), summary(args.live)
    dr, dl = load_daily(args.research), load_daily(args.live)
    fd = fill_diff(args.research, args.live)

    accounts = [sr, sl]
    if args.ta:
        accounts.append(summary(args.ta))
        if args.ta_control:
            accounts.append(summary(args.ta_control))

    common = pd.DataFrame()
    if not dr.empty and not dl.empty:
        common = dr[["date", "daily_ret", "excess_ret", "nav"]].merge(
            dl[["date", "daily_ret", "excess_ret", "nav"]],
            on="date", how="inner", suffixes=("_research", "_live"))
        if not common.empty:
            common["daily_ret_gap"] = common["daily_ret_live"] - common["daily_ret_research"]
            common["excess_gap"] = common["excess_ret_live"] - common["excess_ret_research"]

    lines = [
        f"# 双线复盘 {dt.date.today():%Y-%m-%d}",
        "",
        f"- 研究模拟线: `{args.research}`",
        f"- 实盘线: `{args.live}`",
    ]
    if args.ta:
        lines.append(f"- TA 影子线: `{args.ta}`（对照 `{args.ta_control}`；详见 `ops/review_ta_overlay.py`）")
    lines += [
        "",
        "## 总览",
        "| 账户 | 天数 | 初始资金 | 最新净值 | 累计收益 | 累计超额 | 平均持仓 | 平均现金 | 平均换手 | 费用 | 费用/成交额 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for s in accounts:
        if not s.get("days"):
            lines.append(f"| {s['account']} | 0 | - | - | - | - | - | - | - | - | - |")
            continue
        lines.append(
            f"| {s['account']} | {s['days']} | {s['start']:,.0f} | {s['nav']:,.2f} | "
            f"{s['cum_ret']:+.2%} | {s['cum_excess']:+.2%} | {s['avg_pos']:.1f} | "
            f"{s['cash_ratio']:.1%} | {s['turnover']:.1%} | {s['fee']:,.2f} | "
            f"{s['fee_ratio']:.2%} |"
        )

    lines += ["", "## 共同交易日表现差异"]
    if common.empty:
        lines.append("暂无共同交易日。")
    else:
        lines.append(
            f"- 共同交易日 {len(common)} 天；实盘-研究 日收益均值差 "
            f"{common['daily_ret_gap'].mean():+.2%}，超额均值差 {common['excess_gap'].mean():+.2%}"
        )
        lines += ["| 日期 | 研究收益 | 实盘收益 | 收益差 | 研究超额 | 实盘超额 | 超额差 |",
                  "|---|---:|---:|---:|---:|---:|---:|"]
        for r in common.tail(10).itertuples():
            lines.append(
                f"| {r.date} | {r.daily_ret_research:+.2%} | {r.daily_ret_live:+.2%} | "
                f"{r.daily_ret_gap:+.2%} | {r.excess_ret_research:+.2%} | "
                f"{r.excess_ret_live:+.2%} | {r.excess_gap:+.2%} |"
            )

    lines += ["", "## 成交偏差（实盘相对研究线）"]
    if fd.empty:
        lines.append("暂无可匹配成交。")
    else:
        lines.append(
            f"- 可匹配成交 {len(fd)} 笔；平均不利滑点 {fd['adverse_slip_pct'].mean():+.3f}%"
        )
        lines += ["| 日期 | 标的 | 方向 | 研究价 | 实盘价 | 不利滑点% |",
                  "|---|---|---|---:|---:|---:|"]
        for r in fd.sort_values("adverse_slip_pct", ascending=False).head(15).itertuples():
            lines.append(
                f"| {r.date} | {r.instrument} | {r.side} | {r.research_price:.2f} | "
                f"{r.live_price:.2f} | {r.adverse_slip_pct:+.3f} |"
            )

    REPORTS.mkdir(parents=True, exist_ok=True)
    out = REPORTS / f"dual_review_{dt.date.today():%Y-%m-%d}.md"
    out.write_text("\n".join(lines))
    print("\n".join(lines))
    print(f"\n[OK] 双线复盘报告 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
